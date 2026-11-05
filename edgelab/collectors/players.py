"""Jugadores y alineaciones desde Statshub.

- /api/team/{id}/last-games?page=&limit=500 → hasta 500 partidos por equipo con las
  alineaciones jugadas y las stats de cada jugador (xG, xA, regates, entradas,
  duelos, faltas, tarjetas…). Con esto se calcula la fuerza de cada once y los duelos.
- /api/event/{id}/predicted-teams-lineup → alineación prevista o confirmada de un
  partido futuro (Statshub marca predictionType = predicted | confirmed).
"""
from __future__ import annotations

import time

from ..core import db

GENERIC = {"G", "D", "M", "F"}


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_last_games(rows: list[dict]) -> list[dict]:
    """Devuelve partidos con sus dos alineaciones y stats por jugador."""
    out = []
    for g in rows:
        ev = g.get("events") or {}
        mid = ev.get("id")
        if not mid or ev.get("status") != "finished":
            continue
        forms = g.get("formations") or {}
        rec = {"match_id": mid, "kickoff": ev.get("timeStartTimestamp"), "home_id": ev.get("homeTeamId"),
               "away_id": ev.get("awayTeamId"), "players": [],
               "home_name": (g.get("homeTeam") or {}).get("name"), "away_name": (g.get("awayTeam") or {}).get("name"),
               "tournament": ev.get("uniqueTournamentName") or ev.get("tournamentName"),
               "hg": ev.get("homeScoreCurrent"), "ag": ev.get("awayScoreCurrent"),
               "formation_h": (forms.get(str(ev.get("homeTeamId"))) or {}).get("formation"),
               "formation_a": (forms.get(str(ev.get("awayTeamId"))) or {}).get("formation")}
        for side, key, tid in (("H", "homeTeamLineup", ev.get("homeTeamId")), ("A", "awayTeamLineup", ev.get("awayTeamId"))):
            for p in g.get(key) or []:
                pos = p.get("position") or ""
                starter = pos not in GENERIC and not p.get("isSubstitute")
                rec["players"].append({
                    "player_id": p.get("playerId"), "name": p.get("name"), "team_id": tid, "side": side, "position": pos,
                    "starter": int(starter), "jersey": p.get("jerseyNo"), "minutes": _f(p.get("minutesPlayed")) or 0.0,
                    "goals": _i(p.get("goals")), "assists": _i(p.get("assists")), "xg": _f(p.get("expectedGoals")),
                    "xa": _f(p.get("expectedAssists")), "shots": _i(p.get("shots")), "sot": _i(p.get("onTargetScoringAttempt")),
                    "key_pass": _i(p.get("keyPass")), "won_contest": _i(p.get("wonContest")), "tackles": _i(p.get("totalTackle")),
                    "interceptions": _i(p.get("interceptionWon")), "duel_won": _i(p.get("duelWon")), "duel_lost": _i(p.get("duelLost")),
                    "aerial_won": _i(p.get("aerialWon")), "fouls": _i(p.get("fouls")), "was_fouled": _i(p.get("wasFouled")),
                    "yellow": 1 if p.get("yellowCard") else 0, "red": 1 if p.get("redCard") else 0,
                    "crosses": _i(p.get("totalCross")), "saves": _i(p.get("saves")), "rating": _f(p.get("rating")),
                })
        out.append(rec)
    return out


def store_last_games(parsed: list[dict]) -> int:
    n = 0
    now = int(time.time())
    for g in parsed:
        db.execute("UPDATE matches SET formation_h=COALESCE(?,formation_h), formation_a=COALESCE(?,formation_a) WHERE id=?",
                   (g["formation_h"], g["formation_a"], g["match_id"]))
        db.execute("INSERT OR REPLACE INTO game_meta(match_id,kickoff,home_id,away_id,home_name,away_name,tournament,hg,ag) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (g["match_id"], g["kickoff"], g["home_id"], g["away_id"], g.get("home_name"),
                                                g.get("away_name"), g.get("tournament"), g.get("hg"), g.get("ag")))
        rows_ps, rows_lu = [], []
        for p in g["players"]:
            if not p["player_id"]:
                continue
            db.execute("INSERT INTO players(id,name,team_id,position,updated) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "name=excluded.name, team_id=CASE WHEN excluded.updated>=COALESCE(updated,0) THEN excluded.team_id ELSE team_id END, "
                       "position=CASE WHEN excluded.position NOT IN ('G','D','M','F') THEN excluded.position ELSE position END",
                       (p["player_id"], p["name"], p["team_id"], p["position"], g["kickoff"] or now))
            rows_ps.append((p["player_id"], g["match_id"], p["team_id"], p["side"], p["position"], p["starter"], p["minutes"],
                            p["goals"], p["assists"], p["xg"], p["xa"], p["shots"], p["sot"], p["key_pass"], p["won_contest"],
                            p["tackles"], p["interceptions"], p["duel_won"], p["duel_lost"], p["aerial_won"], p["fouls"],
                            p["was_fouled"], p["yellow"], p["red"], p["crosses"], p["saves"], p["rating"]))
            if p["starter"] or p["minutes"] > 0:
                rows_lu.append((g["match_id"], p["side"], p["player_id"], p["name"], p["position"], p["jersey"], p["starter"], "played",
                                g["formation_h"] if p["side"] == "H" else g["formation_a"], now))
        db.executemany("INSERT OR REPLACE INTO player_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_ps)
        db.executemany("INSERT OR REPLACE INTO lineups(match_id,side,player_id,name,position,jersey,starter,source,formation,ts) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", rows_lu)
        n += 1
    return n


def store_predicted(match_id: int, data: dict, formations: dict | None = None) -> int:
    """Guarda la alineación prevista/confirmada de un partido futuro."""
    now = int(time.time())
    n = 0
    db.execute("DELETE FROM lineups WHERE match_id=? AND source IN ('predicted','confirmed')", (match_id,))
    for side, key in (("H", "homeTeam"), ("A", "awayTeam")):
        t = data.get(key) or {}
        for p in t.get("data") or []:
            src = "confirmed" if p.get("predictionType") == "confirmed" else "predicted"
            db.execute("INSERT OR REPLACE INTO lineups(match_id,side,player_id,name,position,jersey,starter,source,formation,ts) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (match_id, side, p.get("playerId"), p.get("name"), p.get("position"), p.get("jerseyNo"),
                        1 if p.get("isStarter", True) else 0, src,
                        ((formations or {}).get(str(t.get("teamId"))) or {}).get("formation"), now))
            n += 1
    return n


# ---------------------------------------------------------------------------
# Remates de cabeza / desde fuera del área / primera parte (Statshub extra-stats, por partido)
# ---------------------------------------------------------------------------
def _nk(s: str) -> tuple[str, str]:
    import re as _re
    import unicodedata as _u
    t = _re.sub(r"[^a-z0-9 ]+", " ", _u.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()).split()
    return (t[-1], t[0][:1]) if t else ("", "")


def store_extra(mid: int, data: dict) -> int:
    """Empareja cada jugador de extra-stats (viene por nombre) con los de ese partido en player_stats."""
    rows = (data or {}).get("data") or []
    if not rows:
        return 0
    have = db.q("SELECT ps.player_id, p.name FROM player_stats ps JOIN players p ON p.id=ps.player_id WHERE ps.match_id=?", (mid,))
    by_last: dict = {}
    for h in have:
        by_last.setdefault(_nk(h["name"])[0], []).append(h)
    out = []
    for r in rows:
        last, ini = _nk(r.get("playerName") or str(r.get("playerSlug") or "").replace("-", " "))
        c = by_last.get(last) or []
        if len(c) > 1:
            c = [h for h in c if _nk(h["name"])[1] == ini] or c[:0]
        if len(c) != 1:
            continue
        out.append((c[0]["player_id"], mid, _i(r.get("headedShot")), _i(r.get("headedShotOnTarget")), _i(r.get("shotsOutsideBox")),
                    _i(r.get("shotsOnTargetOutsideBox")), _i(r.get("firstHalfShots")), _i(r.get("firstHalfShotsOnTarget")),
                    _i(r.get("firstHalfFouls")), _i(r.get("firstHalfFoulsWon"))))
    if out:
        db.executemany("INSERT OR REPLACE INTO player_extra(player_id,match_id,head_shots,head_sot,out_shots,out_sot,fh_shots,fh_sot,"
                       "fh_fouls,fh_fouled) VALUES(?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def backfill_extra(limit: int = 40, log=None) -> int:
    """Primero los partidos de los equipos que juegan en los próximos 7 días (sus últimos ~15), luego el resto."""
    from ..live.engine import shub
    now = int(time.time())
    rows = db.q("""SELECT DISTINCT mt.id FROM matches mt
                   WHERE mt.status='finished' AND mt.kickoff BETWEEN ? AND ?
                     AND EXISTS (SELECT 1 FROM player_stats ps WHERE ps.match_id=mt.id)
                     AND NOT EXISTS (SELECT 1 FROM player_extra pe WHERE pe.match_id=mt.id)
                     AND NOT EXISTS (SELECT 1 FROM kv WHERE kv.key = 'pex_none:' || mt.id)
                   ORDER BY (CASE WHEN mt.home_id IN (SELECT home_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?
                                                      UNION SELECT away_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?)
                               OR mt.away_id IN (SELECT home_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?
                                                      UNION SELECT away_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?)
                             THEN 0 ELSE 1 END), mt.kickoff DESC LIMIT ?""",
                (now - 300 * 86400, now - 3 * 3600, *([now, now + 7 * 86400] * 4), limit))
    n = 0
    for r in rows:
        try:
            d = shub().extra_stats(r["id"])
        except Exception:
            d = None
        k = store_extra(r["id"], d) if d else 0
        if not k:
            db.kv_set(f"pex_none:{r['id']}", 1)
        n += k
    return n
