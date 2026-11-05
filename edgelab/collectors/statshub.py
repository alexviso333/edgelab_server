"""Colector principal: Statshub (API JSON interna, IDs de Sofascore).

Endpoints verificados con el tráfico capturado en Burp (01/11/2026):
  /api/event/by-date?startOfDay=&endOfDay=          partidos de un día (con árbitro, HT, estado)
  /api/event/live-scores?ids=a,b,c                   marcador y estado en directo
  /api/event/lineup-status?ids=                      none | predicted | confirmed
  /api/event/{id}/predicted-teams-lineup             alineación prevista/confirmada
  /api/event/{id}/team-lineup?teamId=&heatmap=false  alineación con minutos jugados
  /api/event/{id}/odds-comparison?homeTeam=&awayTeam=  ~130 mercados, 15 casas (bet365, Pinnacle, Betfair...)
  /api/event/{id}/odds-movements?market=             histórico de cuotas (props de jugador)
  /api/event/{id}/extra-stats                        tiros/faltas por jugador
  /api/event/bulk-team-odds?eventIds=                1X2 de muchos partidos de golpe
  /api/team/{id}/performance?tournamentId=&limit=&location=both   stats por partido (xG, tiros, córners...)
  /api/team/{id}/players/performance?...             stats por jugador y partido
  /api/team/{id}/last-games?page=&limit=             últimos partidos con alineaciones
  /api/team/{id}/tournaments-and-seasons
  /api/tournament/{tid}/seasons, /api/tournament/{tid}/{season}/standing, /api/tournament/{tid}/{season}/events
Cloudflare devuelve 429 (error 1015) si se va rápido: el cliente espera ≥2.5 s entre peticiones.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..config import COMPETITIONS, UNIQUE_TO_KEY, settings
from ..core import db
from .http import PoliteClient

TARGET_UNIQUE_IDS = {COMPETITIONS[k].unique_id for k in settings.competitions if k in COMPETITIONS}

STATUS_MAP = {"notstarted": "notstarted", "inprogress": "inprogress", "in_progress": "inprogress",
              "finished": "finished", "postponed": "postponed", "canceled": "canceled", "cancelled": "canceled",
              "1st half": "inprogress", "2nd half": "inprogress", "halftime": "inprogress", "ht": "inprogress",
              "extra time": "inprogress", "penalties": "inprogress", "ended": "finished", "ft": "finished",
              "aet": "finished", "ap": "finished", "interrupted": "inprogress", "abandoned": "canceled"}


class Statshub:
    def __init__(self, client: PoliteClient | None = None):
        headers = {"Referer": "https://www.statshub.com/", "Origin": "https://www.statshub.com",
                   "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        if settings.statshub_cookie:
            headers["Cookie"] = settings.statshub_cookie
        if settings.statshub_relay and settings.statshub_relay_key:
            headers["x-relay-key"] = settings.statshub_relay_key
        self.c = client or PoliteClient(min_interval=settings.statshub_min_interval, headers=headers)
        self.base = (settings.statshub_relay or settings.statshub_base).rstrip("/")

    def get(self, path: str, params: dict | None = None, ttl: float | None = None) -> Any:
        return self.c.get_json(f"{self.base}{path}", params=params, ttl=ttl)

    # ------------------------------------------------------------------ eventos
    def events_by_date(self, day: datetime, ttl: float | None = None) -> list[dict]:
        """Todos los partidos de un día natural (hora de Madrid ≈ UTC+1)."""
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=1)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        data = self.get("/api/event/by-date", {"startOfDay": int(start.timestamp()), "endOfDay": int(end.timestamp())}, ttl=ttl)
        return data.get("data", []) if isinstance(data, dict) else data

    def live_scores(self, ids: Iterable[int]) -> dict[int, dict]:
        ids = [str(i) for i in ids]
        out: dict[int, dict] = {}
        for k in range(0, len(ids), 150):
            data = self.get("/api/event/live-scores", {"ids": ",".join(ids[k:k + 150])})
            for eid, v in (data.get("data") or {}).items():
                out[int(eid)] = v
        return out

    def lineup_status(self, ids: Iterable[int]) -> dict[int, str]:
        ids = [str(i) for i in ids]
        out: dict[int, str] = {}
        for k in range(0, len(ids), 150):
            data = self.get("/api/event/lineup-status", {"ids": ",".join(ids[k:k + 150])})
            out.update({int(a): b for a, b in (data.get("data") or {}).items()})
        return out

    def predicted_lineup(self, event_id: int) -> dict:
        return self.get(f"/api/event/{event_id}/predicted-teams-lineup", ttl=900)

    def odds_comparison(self, event_id: int, home: str, away: str) -> dict:
        return self.get(f"/api/event/{event_id}/odds-comparison", {"homeTeam": home, "awayTeam": away})

    def bulk_team_odds(self, ids: Iterable[int]) -> dict:
        ids = [str(i) for i in ids]
        out: dict = {}
        for k in range(0, len(ids), 150):
            data = self.get("/api/event/bulk-team-odds", {"eventIds": ",".join(ids[k:k + 150])})
            out.update(data.get("data") or {})
        return out

    def odds_movements(self, event_id: int, market: str) -> dict:
        return self.get(f"/api/event/{event_id}/odds-movements", {"market": market})

    def team_performance(self, team_id: int, tournament_ids: Iterable[int], limit: int = 30, ttl: float | None = 3600) -> list[dict]:
        data = self.get(f"/api/team/{team_id}/performance",
                        {"tournamentId": ",".join(str(t) for t in tournament_ids), "limit": limit, "location": "both"}, ttl=ttl)
        return data.get("data", [])

    def team_tournaments(self, team_id: int) -> dict:
        return self.get(f"/api/team/{team_id}/tournaments-and-seasons", ttl=86400 * 7)

    def team_lineup(self, event_id: int, team_id: int, heatmap: bool = False, ttl: float | None = None) -> list[dict]:
        return self.get(f"/api/event/{event_id}/team-lineup", {"teamId": team_id, "heatmap": "true" if heatmap else "false"},
                        ttl=ttl).get("data", [])

    def team_last_games(self, team_id: int, page: int = 1, limit: int = 100, ttl: float | None = 3600 * 6) -> list[dict]:
        return self.get(f"/api/team/{team_id}/last-games", {"page": page, "limit": limit}, ttl=ttl).get("data", [])

    def formations(self, event_id: int) -> dict:
        return self.get(f"/api/event/{event_id}/formations", ttl=1800).get("data", {})

    def standing(self, tournament_id: int, season_id: int) -> list[dict]:
        return self.get(f"/api/tournament/{tournament_id}/{season_id}/standing", ttl=3600 * 6).get("data", [])

    def extra_stats(self, event_id: int) -> dict:
        return self.get(f"/api/event/{event_id}/extra-stats")


# ---------------------------------------------------------------------------
# Parsers (funciones puras, testeadas con respuestas reales capturadas)
# ---------------------------------------------------------------------------
def season_label(ts: int) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def parse_event(e: dict) -> dict | None:
    """Convierte una entrada de by-date / team events en una fila de `matches`."""
    ev = e.get("events") or e.get("event") or {}
    ut = ev.get("uniqueTournamentId")
    comp = UNIQUE_TO_KEY.get(ut)
    if comp is None:
        return None
    status = STATUS_MAP.get(str(ev.get("status", "")).lower(), str(ev.get("status", "")).lower())
    home, away = e.get("homeTeam") or {}, e.get("awayTeam") or {}
    finished = status == "finished"
    hg = ev.get("homeScoreNormaltime") if finished else ev.get("homeScoreCurrent")
    ag = ev.get("awayScoreNormaltime") if finished else ev.get("awayScoreCurrent")
    p1h, p1a = ev.get("period1Home"), ev.get("period1Away")
    has_ht = finished or status == "inprogress"
    extra = {
        "seasonId": ev.get("seasonId"), "tournamentId": ev.get("tournamentId"), "uniqueTournamentId": ut,
        "venueId": ev.get("venueId"), "injuryTime1": ev.get("injuryTime1"), "injuryTime2": ev.get("injuryTime2"),
        "periodStart": ev.get("timeCurrentPeriodStartTimestamp"), "rawStatus": ev.get("status"),
        "refereeAvgCards": _f(e.get("refereeAvgCards")), "refereeGames": e.get("refereeGames"),
        "refereeYellowCards": e.get("refereeYellowCards"), "refereeRedCards": e.get("refereeRedCards"),
        "winnerCode": ev.get("winnerCode"), "pens_h": ev.get("homeScorePenalties"), "pens_a": ev.get("awayScorePenalties"),
        "homeSlug": home.get("slug"), "awaySlug": away.get("slug"), "slug": ev.get("slug"),
    }
    return {
        "id": ev.get("id"), "comp": comp, "season": season_label(ev.get("timeStartTimestamp", 0)),
        "kickoff": ev.get("timeStartTimestamp"), "home_id": home.get("id") or ev.get("homeTeamId"),
        "away_id": away.get("id") or ev.get("awayTeamId"), "home_name": home.get("name"), "away_name": away.get("name"),
        "status": status, "hg": hg if (finished or status == "inprogress") else None,
        "ag": ag if (finished or status == "inprogress") else None,
        "hg_ht": p1h if has_ht else None, "ag_ht": p1a if has_ht else None,
        "hg_et": ev.get("homeScoreCurrent") if finished and ev.get("homeScoreOvertime") is not None else None,
        "ag_et": ev.get("awayScoreCurrent") if finished and ev.get("awayScoreOvertime") is not None else None,
        "referee": e.get("refereeName"), "referee_id": ev.get("refereeId"),
        "round": str(ev.get("roundInfo") or ev.get("roundSlug") or ""),
        "lineup_status": e.get("lineupType") or ("confirmed" if ev.get("lineupConfirmed") else None),
        "source": "statshub", "extra": extra,
        "_teams": [(home.get("id"), home.get("name"), home.get("shortname"), home.get("slug"), home.get("countrySlug"),
                    home.get("teamcolorsprimary"), home.get("teamcolorssecondary")),
                   (away.get("id"), away.get("name"), away.get("shortname"), away.get("slug"), away.get("countrySlug"),
                    away.get("teamcolorsprimary"), away.get("teamcolorssecondary"))],
    }


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def live_minute(match: dict, now: float | None = None) -> float | None:
    """Minuto aproximado a partir del inicio del periodo actual."""
    now = now or time.time()
    extra = match.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    raw = str(extra.get("rawStatus") or match.get("status") or "").lower()
    start = extra.get("periodStart") or 0
    if not start:
        return None
    el = (now - start) / 60.0
    if "2nd" in raw or "second" in raw:
        return 45 + el
    if "1st" in raw or "first" in raw:
        return el
    if "half" in raw and "time" in raw:
        return 45.0
    if "extra" in raw:
        return 90 + el
    return None


_TEAM_RE = re.compile(r"^(.*)\s\(([+-]?\d+(?:\.\d+)?)\)$")
_OU_RE = re.compile(r"^(Over|Under)\s+(\d+(?:\.\d+)?)$", re.I)


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _side(label: str, home: str, away: str) -> str | None:
    n = _norm(label)
    if n == _norm(home):
        return "H"
    if n == _norm(away):
        return "A"
    if n in ("draw", "tie", "x"):
        return "D"
    if _norm(home) in n or n in _norm(home):
        return "H"
    if _norm(away) in n or n in _norm(away):
        return "A"
    return None


def _period(name: str) -> str:
    if name.endswith(" HT") or "1st Half" in name:
        return "HT"
    if name.endswith(" 2H"):
        return "2H"
    return "FT"


def parse_odds_comparison(data: dict, home: str, away: str, include_raw: bool = True) -> list[dict]:
    """Normaliza la respuesta de odds-comparison a filas (market, selection, line, period, bookmaker, odds)."""
    rows: list[dict] = []
    for m in data.get("markets", []):
        name = m.get("name", "")
        per = _period(name)
        base = name.replace(" HT", "").replace(" 2H", "")
        for o in m.get("outcomes", []):
            label = o.get("label", "")
            parsed = _parse_outcome(base, label, home, away, o)
            for b in o.get("bookmakerOdds", []):
                odds = _f(b.get("odds"))
                if odds is None or odds <= 1.0:
                    continue
                if parsed is None:
                    if include_raw:
                        rows.append({"market": f"RAW:{name}", "selection": label[:120], "line": _f(b.get("line")),
                                     "period": per, "bookmaker": b.get("bookmaker"), "odds": odds})
                    continue
                mk, sel, line = parsed
                rows.append({"market": mk, "selection": sel, "line": line, "period": per,
                             "bookmaker": b.get("bookmaker"), "odds": odds})
    return fix_ah_signs(rows)


def _parse_outcome(base: str, label: str, home: str, away: str, o: dict):
    if base in ("To Win", "ML"):
        s = _side(label, home, away)
        return ("1X2", s, None) if s else None
    if base == "Double Chance":
        parts = [p.strip() for p in label.split(" or ")]
        if len(parts) != 2:
            return None
        sides = {_side(p, home, away) for p in parts}
        code = {frozenset({"H", "D"}): "1X", frozenset({"D", "A"}): "X2", frozenset({"H", "A"}): "12"}.get(frozenset(sides))
        return ("DC", code, None) if code else None
    if base == "Draw No Bet":
        s = _side(label, home, away)
        return ("DNB", s, 0.0) if s in ("H", "A") else None
    if base in ("Spread", "Corners Spread", "Bookings Spread"):
        mt = _TEAM_RE.match(label.strip())
        if not mt:
            return None
        s = _side(mt.group(1), home, away)
        mk = {"Spread": "AH", "Corners Spread": "CORNERS_AH", "Bookings Spread": "CARDS_AH"}[base]
        return (mk, s, float(mt.group(2))) if s in ("H", "A") else None
    mo = _OU_RE.match(label.strip())
    ou_markets = {"Totals": "OU", "Goals Over/Under": "OU", "Corners Totals": "CORNERS_OU",
                  "Number of Cards In Match": "CARDS_OU", "Bookings Totals": "CARDS_OU",
                  "Match Shots": "SHOTS_OU", "Match Shots on Target": "SOT_OU"}
    team_ou = {"Team Total Goals Home": ("TEAM_OU", "H"), "Team Total Goals Away": ("TEAM_OU", "A"),
               "Corners Totals Home": ("CORNERS_OU", "H"), "Corners Totals Away": ("CORNERS_OU", "A"),
               "Team Corners Home": ("CORNERS_OU", "H"), "Team Corners Away": ("CORNERS_OU", "A"),
               "Bookings Totals Home": ("CARDS_OU", "H"), "Bookings Totals Away": ("CARDS_OU", "A"),
               "Team Shots Home": ("SHOTS_OU", "H"), "Team Shots Away": ("SHOTS_OU", "A"),
               "Team Shots on Target Home": ("SOT_OU", "H"), "Team Shots on Target Away": ("SOT_OU", "A")}
    if mo and base in ou_markets:
        return (ou_markets[base], "O" if mo.group(1).lower() == "over" else "U", float(mo.group(2)))
    if mo and base in team_ou:
        mk, side = team_ou[base]
        return (mk, side + ("O" if mo.group(1).lower() == "over" else "U"), float(mo.group(2)))
    if mo and base.endswith(" Total Goals"):
        team = base[: -len(" Total Goals")]
        s = _side(team, home, away)
        if s in ("H", "A"):
            return ("TEAM_OU", s + ("O" if mo.group(1).lower() == "over" else "U"), float(mo.group(2)))
    if mo and base.endswith(" Total Cards"):
        team = base[: -len(" Total Cards")]
        s = _side(team, home, away)
        if s in ("H", "A"):
            return ("CARDS_OU", s + ("O" if mo.group(1).lower() == "over" else "U"), float(mo.group(2)))
    if base == "Both Teams To Score":
        return ("BTTS", "Y" if label.lower().startswith("y") else "N", None)
    if base == "Correct Score" and re.match(r"^\d+-\d+$", label.strip()):
        return ("CS", label.strip(), None)
    return None


def fix_ah_signs(rows: list[dict]) -> list[dict]:
    """Algunas casas (Pinnacle en Statshub) traen el signo del hándicap invertido.

    Para cada lado, la cuota debe BAJAR cuando la línea sube (más ventaja = más
    probable). Si en una casa y lado la relación sale al revés, se invierte su
    signo (en la captura, Pinnacle trae invertido solo el lado local).
    """
    by_bk: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["market"] in ("AH", "CORNERS_AH", "CARDS_AH") and r["selection"] in ("H", "A"):
            by_bk.setdefault((r["bookmaker"], r["market"], r["period"], r["selection"]), []).append(r)
    flip = set()
    for key, rs in by_bk.items():
        pts = sorted({(r["line"], r["odds"]) for r in rs})
        if len(pts) < 2:
            continue
        # correlación de signo entre línea y cuota
        s = 0
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                dl = pts[j][0] - pts[i][0]
                do = pts[j][1] - pts[i][1]
                if dl and do:
                    s += 1 if dl * do > 0 else -1
        if s > 0:
            flip.add(key)
    if not flip:
        return rows
    out = []
    for r in rows:
        if (r["bookmaker"], r["market"], r["period"], r["selection"]) in flip and r["line"] is not None:
            r = dict(r)
            r["line"] = -r["line"] + 0.0
        out.append(r)
    return out


def parse_team_performance(rows: list[dict], team_id: int) -> list[dict]:
    """Stats por partido de `team/{id}/performance` → filas para match_stats."""
    out = []
    for r in rows:
        ev = r.get("event", {})
        home = r.get("homeTeam", {})
        is_home = home.get("id") == team_id
        st, op = r.get("statistics") or {}, r.get("opponentStatistics") or {}
        out.append({
            "match_id": ev.get("id"),
            "kickoff": int(ev.get("timeStartTimestamp") or 0),
            "home_id": home.get("id"), "away_id": (r.get("awayTeam") or {}).get("id"),
            "home_name": home.get("name"), "away_name": (r.get("awayTeam") or {}).get("name"),
            "league_id": (r.get("league") or {}).get("id"),
            "H": st if is_home else op, "A": op if is_home else st,
            "score90": r.get("ninetyMinuteScore") or r.get("score"),
        })
    return out


def store_events(entries: list[dict]) -> list[int]:
    """Guarda partidos (solo competiciones objetivo) y sus equipos."""
    ids = []
    for e in entries:
        m = parse_event(e)
        if not m or not m.get("id"):
            continue
        teams = m.pop("_teams")
        for tid, name, short, slug, country, c1, c2 in teams:
            if tid:
                db.execute("INSERT INTO teams(id,name,short,slug,country,color1,color2) VALUES(?,?,?,?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET name=excluded.name, short=excluded.short, slug=excluded.slug, "
                           "color1=COALESCE(excluded.color1,color1), color2=COALESCE(excluded.color2,color2)",
                           (tid, name, short, slug, country, c1, c2))
        ids.append(db.upsert_match(m))
    return ids


def store_odds_rows(match_id: int, rows: list[dict], phase: str, ts: int | None = None, minute: float | None = None,
                    score: tuple[int, int] | None = None, source: str = "statshub") -> int:
    ts = ts or int(time.time())
    if phase in ("snap", "open", "close") and rows:
        # solo se guardan en el histórico las cuotas que han cambiado (la base de datos no crece sin aportar nada)
        last = {(r["bookmaker"], r["market"], r["selection"], r["line_k"], r["period"]): r["odds"]
                for r in db.q("SELECT bookmaker, market, selection, line_k, period, odds FROM odds_now WHERE match_id=?", (match_id,))}
        key = lambda r: (r["bookmaker"], r["market"], r["selection"], -999.0 if r["line"] is None else round(float(r["line"]), 2), r.get("period", "FT"))
        changed = [r for r in rows if last.get(key(r)) != r["odds"]]
        db.upsert_odds_now(match_id, [(r["bookmaker"], r["market"], r["selection"], r["line"], r.get("period", "FT"), r["odds"]) for r in changed], ts)
        db.kv_set(f"odds_checked:{match_id}", ts)
        if phase != "close":
            rows = changed
    db.executemany(
        "INSERT INTO odds(match_id,ts,phase,bookmaker,market,selection,line,period,odds,minute,score_h,score_a,source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(match_id, ts, phase, r["bookmaker"], r["market"], r["selection"], r["line"], r.get("period", "FT"), r["odds"],
          minute, score[0] if score else None, score[1] if score else None, source) for r in rows])
    return len(rows)


def store_team_stats(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        if not r["match_id"]:
            continue
        for side in ("H", "A"):
            db.execute("INSERT INTO match_stats(match_id,side,stats,source) VALUES(?,?,?,?) "
                       "ON CONFLICT(match_id,side) DO UPDATE SET stats=excluded.stats",
                       (r["match_id"], side, json.dumps(r[side]), "statshub"))
        n += 1
    return n
