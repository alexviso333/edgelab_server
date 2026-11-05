"""FotMob (datos de Opta): la fuente en directo más rápida que hemos medido (≈25 s de retraso, como Sofascore;
ESPN va 1-2 min tarde) y sin bloqueos. Da xG por tiro de Opta (y xG a puerta), tiros con tipo (cabeza/pie),
zona y situación, estadísticas completas de equipo y de CADA jugador (tiros, a puerta, faltas, faltas recibidas,
entradas, toques en el área…), presión minuto a minuto, cambios y tarjetas.

Solo se leen páginas públicas que su robots.txt permite (/leagues/… y /match/…); su API (/api/…) la prohíbe y no
se usa. Una página por partido y minuto como mucho.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime

from ..core import db
from .espn import _norm, _sim

LEAGUE = {"UCL": 42, "UEL": 73, "UECL": 10216, "LALIGA": 87, "EPL": 47, "SERIEA": 55, "BUNDES": 54, "LIGUE1": 53, "CDR": 138}
BASE = "https://www.fotmob.com"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept-Language": "en"}
_ND = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_PAGE: dict = {}
_FAIL = {"until": 0.0, "n": 0}


def _page(path: str, timeout: int = 15) -> dict | None:
    if time.time() < _FAIL["until"]:
        return None
    try:
        html = urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_UA), timeout=timeout).read().decode("utf-8", "ignore")
        m = _ND.search(html)
        if not m:
            raise ValueError("sin datos")
        _FAIL["n"] = 0
        _FAIL["since"] = 0
        return json.loads(m.group(1)).get("props", {}).get("pageProps") or {}
    except Exception as e:
        _FAIL["n"] += 1
        _FAIL["since"] = _FAIL.get("since") or time.time()
        if _FAIL["n"] >= 3:                     # si nos rechaza, no insistir: 10 min de descanso
            _FAIL["until"] = time.time() + 600
            _FAIL["n"] = 0
            db.log_job("fotmob", "error", f"pausa 10 min: {e}")
        return None


def fixtures(comp: str) -> list[dict]:
    lid = LEAGUE.get(comp)
    if not lid:
        return []
    key = f"fotmob_fx:{lid}"
    c = db.kv_get(key)
    if c and time.time() - c.get("ts", 0) < 12 * 3600:
        return c["m"]
    pp = _page(f"/leagues/{lid}/fixtures")
    if not pp:
        return (c or {}).get("m", [])
    out = []
    for a in (pp.get("fixtures") or {}).get("allMatches") or []:
        try:
            ts = int(datetime.fromisoformat(str((a.get("status") or {}).get("utcTime")).replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = None
        out.append({"id": str(a.get("id")), "home": (a.get("home") or {}).get("name"), "away": (a.get("away") or {}).get("name"), "ts": ts})
    db.kv_set(key, {"ts": int(time.time()), "m": out})
    return out


def find_match(m: dict) -> str | None:
    key = f"fotmob_id:{m['id']}"
    c = db.kv_get(key)
    if c:
        return c or None
    best, bs = None, 0.0
    for f in fixtures(m["comp"]):
        if f["ts"] and abs(f["ts"] - m["kickoff"]) > 6 * 3600:
            continue
        sc = (_sim(m["home_name"], f["home"] or "") + _sim(m["away_name"], f["away"] or "")) / 2
        if sc > bs:
            best, bs = f, sc
    fid = best["id"] if best and bs >= 0.62 else None
    if fid:
        db.kv_set(key, fid)
    return fid


def _n(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    mm = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v))
    return float(mm.group(1)) if mm else None


TEAM_KEYS = {"BallPossesion": "poss", "expected_goals": "xg", "expected_goals_on_target": "xgot", "total_shots": "shots",
             "ShotsOnTarget": "sot", "blocked_shots": "blocked", "shots_inside_box": "shots_in", "shots_outside_box": "shots_out",
             "touches_opp_box": "box_touches", "big_chance": "big_chances", "corners": "corners", "fouls": "fouls",
             "yellow_cards": "yellow", "red_cards": "red", "Offsides": "offsides", "keeper_saves": "saves",
             "matchstats.headers.tackles": "tackles", "interceptions": "interceptions", "clearances": "clearances",
             "passes": "passes", "aerials_won": "aerials", "dribbles_succeeded": "dribbles", "player_throws": "throws"}
# FotMob (jugador) → clave de Sofascore en las alineaciones
PLAYER_KEYS = {"total_shots": "totalShots", "ShotsOnTarget": "onTargetScoringAttempt", "fouls": "fouls", "was_fouled": "wasFouled",
               "matchstats.headers.tackles": "totalTackle", "goals": "goals", "expected_goals": "expectedGoals",
               "touches_opp_box": "touchesInOppBox", "touches": "touches", "expected_goals_on_target_variant": "xgot"}


def parse_match(pp: dict) -> dict:
    gen = pp.get("general") or {}
    hid = str((gen.get("homeTeam") or {}).get("id"))
    side_of = lambda tid: "H" if str(tid) == hid else "A"          # noqa: E731
    hdr = pp.get("header") or {}
    stt = hdr.get("status") or {}
    teams = hdr.get("teams") or [{}, {}]
    lt = stt.get("liveTime") or {}
    mm = re.match(r"(\d+):(\d+)", str(lt.get("long") or ""))
    minute = int(mm.group(1)) + int(mm.group(2)) / 60 if mm else None
    out = {"ts": time.time(), "started": bool(stt.get("started")), "finished": bool(stt.get("finished")),
           "ht": str(lt.get("short") or "").upper().startswith("HT"), "minute": minute,
           "hg": (teams[0] or {}).get("score"), "ag": (teams[1] or {}).get("score"),
           "reds": (stt.get("numberOfHomeRedCards") or 0, stt.get("numberOfAwayRedCards") or 0)}
    c = pp.get("content") or {}
    T = {"H": {}, "A": {}}
    for g in (((c.get("stats") or {}).get("Periods") or {}).get("All") or {}).get("stats") or []:
        for s_ in g.get("stats") or []:
            k = TEAM_KEYS.get(s_.get("key"))
            v = s_.get("stats") or [None, None]
            if not k or len(v) < 2 or v[0] is None:
                continue
            h, a = _n(v[0]), _n(v[1])
            if h is not None and a is not None:
                T["H"][k], T["A"][k] = h, a
    out["T"] = T
    out["xg"] = (T["H"].get("xg"), T["A"].get("xg"))
    shots = []
    for s_ in (c.get("shotmap") or {}).get("shots") or []:
        if s_.get("isOwnGoal"):
            continue
        typ = str(s_.get("shotType") or "")
        sit = str(s_.get("situation") or "")
        res = ("goal" if s_.get("eventType") == "Goal" else "blocked" if s_.get("isBlocked") else
               "on" if s_.get("isOnTarget") else "off")
        shots.append({"minute": (s_.get("min") or 0) + (s_.get("minAdded") or 0), "side": side_of(s_.get("teamId")),
                      "player": s_.get("playerName") or "", "fid": s_.get("playerId"), "res": res,
                      "body": "head" if typ == "Header" else "left" if typ == "LeftFoot" else "right" if typ == "RightFoot" else "other",
                      "zone": "box" if s_.get("isFromInsideBox") else "out", "pen": sit == "Penalty", "fk": sit == "FreeKick",
                      "corner": sit == "FromCorner", "xg": float(s_.get("expectedGoals") or 0), "xgot": s_.get("expectedGoalsOnTarget"),
                      "x": s_.get("x"), "y": s_.get("y")})
    out["shots"] = shots
    P = {"H": [], "A": []}
    for p in (c.get("playerStats") or {}).values():
        flat = {}
        for g in p.get("stats") or []:
            for lab, v in (g.get("stats") or {}).items():
                k = v.get("key") or lab
                val = (v.get("stat") or {}).get("value")
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    flat[k] = float(val)
        mine = [x for x in shots if str(x["fid"]) == str(p.get("id"))]
        flat["headShots"] = sum(1 for x in mine if x["body"] == "head")
        flat["headSot"] = sum(1 for x in mine if x["body"] == "head" and x["res"] in ("on", "goal"))
        flat["outShots"] = sum(1 for x in mine if x["zone"] == "out")
        flat["outSot"] = sum(1 for x in mine if x["zone"] == "out" and x["res"] in ("on", "goal"))
        P[side_of(p.get("teamId"))].append({"name": p.get("name") or "", "short": "", "jersey": str(p.get("shirtNumber") or ""),
                                            "opta": p.get("optaId"), "st": flat, "sub_in": False, "sub_out": False})
    lu = c.get("lineup") or {}
    subs = {}
    for tk in ("homeTeam", "awayTeam"):
        for p in (lu.get(tk) or {}).get("starters", []) + (lu.get(tk) or {}).get("subs", []):
            for e in ((p.get("performance") or {}).get("substitutionEvents") or []):
                subs.setdefault(p.get("name"), {})[e.get("type")] = e.get("time")
    for sd in ("H", "A"):
        for p in P[sd]:
            s_ = subs.get(p["name"]) or {}
            p["sub_in"], p["sub_out"] = "subIn" in s_, "subOut" in s_
            p["sub_min"] = s_.get("subIn")
    out["P"] = P
    az = c.get("attackingZones") or {}
    out["zones"] = {sd: ((az.get(k) or {}).get("total") or None) for sd, k in (("H", "home"), ("A", "away"))} if az else None
    mo = ((c.get("momentum") or {}).get("main") or {}).get("data") or []
    out["momentum"] = [(d_.get("minute"), d_.get("value")) for d_ in mo]
    last = [v for mi, v in out["momentum"] if mi is not None and minute is not None and mi >= minute - 10 and v is not None]
    if last:
        pos = sum(v for v in last if v > 0)
        neg = -sum(v for v in last if v < 0)
        out["mom"] = {"H": round(pos / 100, 2), "A": round(neg / 100, 2),
                      "share_h": round(pos / (pos + neg), 2) if pos + neg > 30 else None, "upto": minute, "src": "fotmob"}
    else:
        out["mom"] = None
    return out


def live_match(m: dict, max_age: float = 40) -> dict | None:
    c = _PAGE.get(m["id"])
    if c and time.time() - c["ts"] < max_age:
        return c
    fid = find_match(m)
    if not fid:
        return None
    pp = _page(f"/match/{fid}")
    if not pp:
        return c
    try:
        out = parse_match(pp)
    except Exception as e:
        db.log_job("fotmob", "error", f"partido {m['id']}: {e}")
        return c
    if not out["T"]["H"] and not out["shots"]:
        return c
    _PAGE[m["id"]] = out
    return out


def merge_team(sofa: dict | None, fm: dict) -> dict:
    """FotMob va igual o más al día que Sofascore (los dos son Opta): para contadores, el mayor de los dos; el
    resto (posesión, xG de Opta, paradas…) de FotMob."""
    out = {}
    for sd in ("H", "A"):
        b = dict((sofa or {}).get(sd) or {})
        for k, v in (fm.get("T") or {}).get(sd, {}).items():
            if k in ("poss", "xg", "xgot") or k not in b:
                b[k] = v
            else:
                try:
                    b[k] = max(float(b[k] or 0), v)
                except (TypeError, ValueError):
                    b[k] = v
        out[sd] = b
    return out


def overlay_lineup(lu_raw: dict, fm: dict) -> dict:
    """Stats por jugador de FotMob sobre la alineación de Sofascore (dorsal + nombre)."""
    import copy
    out = copy.deepcopy(lu_raw)
    for sd, key in (("H", "home"), ("A", "away")):
        fl = (fm.get("P") or {}).get(sd, [])
        for p in (out.get(key) or {}).get("players") or []:
            pl = p.get("player") or {}
            jersey = str(p.get("jerseyNumber") or pl.get("jerseyNumber") or "")
            nm = pl.get("name") or pl.get("shortName") or ""
            hit = next((e for e in fl if jersey and e["jersey"] == jersey and _sim(nm, e["name"]) > 0.45), None)
            if hit is None:
                best = max(fl, key=lambda e: _sim(nm, e["name"]), default=None)
                hit = best if best and _sim(nm, best["name"]) >= 0.8 else None
            if hit is None:
                continue
            st = dict(p.get("statistics") or {})
            for fk, sk in PLAYER_KEYS.items():
                if fk not in hit["st"]:
                    continue
                v = hit["st"][fk]
                if sk in ("expectedGoals", "xgot"):
                    st[sk] = v
                else:
                    try:
                        st[sk] = max(float(st.get(sk) or 0), v)
                    except (TypeError, ValueError):
                        st[sk] = v
            for fk, sk in (("headShots", "_headShots"), ("headSot", "_headSot"), ("outShots", "_outShots"), ("outSot", "_outSot")):
                st[sk] = hit["st"].get(fk, 0)
            if hit["sub_out"]:
                st["_off"] = 1
            if hit["sub_in"] and st.get("minutesPlayed") is None:
                st["minutesPlayed"] = 1
            p["statistics"] = st
    return out


# ---------------------------------------------------------------------------
# Bajas (B3): la lista de no disponibles que FotMob publica en la página del partido (lesión, sanción…) con la fecha
# prevista de vuelta («Doubtful» = duda). Segunda fuente, para no depender solo de Sofascore.
# ---------------------------------------------------------------------------
def missing(m: dict) -> list[dict]:
    fid = find_match(m)
    if not fid:
        return []
    pp = _page(f"/match/{fid}")
    if not pp:
        return []
    lu = ((pp.get("content") or {}).get("lineup") or {})
    out = []
    for side, key in (("H", "homeTeam"), ("A", "awayTeam")):
        for p in (lu.get(key) or {}).get("unavailable") or []:
            u = p.get("unavailability") or {}
            ret = str(u.get("expectedReturn") or "")
            typ = str(u.get("type") or "").lower()
            out.append({"side": side, "fm_name": p.get("name") or "", "status": "duda" if "doubt" in ret.lower() else "baja",
                        "reason": "sancionado" if "susp" in typ or "card" in typ else "lesionado" if "injur" in typ else "baja",
                        "detail": ret if ret and "doubt" not in ret.lower() else ""})
    return out


def merge_missing(mid: int) -> int:
    """Añade a las bajas del partido (kv missing:<id>) las de FotMob que no estuvieran, emparejadas con nuestros
    jugadores por nombre dentro de cada equipo."""
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return 0
    fm = missing(m)
    if not fm:
        return 0
    cur = db.kv_get(f"missing:{mid}") or {}
    players = list(cur.get("players") or [])
    have = {p.get("id") for p in players if p.get("id")}
    since = m["kickoff"] - 300 * 86400
    added = 0
    for side, tid in (("H", m["home_id"]), ("A", m["away_id"])):
        squad = db.q("SELECT DISTINCT p.id, p.name, ps.position FROM player_stats ps JOIN players p ON p.id=ps.player_id "
                     "JOIN matches mt ON mt.id=ps.match_id WHERE ps.team_id=? AND mt.kickoff BETWEEN ? AND ?", (tid, since, m["kickoff"]))
        for f in [x for x in fm if x["side"] == side]:
            best = max(squad, key=lambda q: _sim(f["fm_name"], q["name"] or ""), default=None)
            if not best or _sim(f["fm_name"], best["name"] or "") < 0.8 or best["id"] in have:
                continue
            players.append({"side": side, "id": best["id"], "name": best["name"], "pos": best["position"], "status": f["status"],
                            "reason": f["reason"], "detail": f["detail"], "src": "fotmob"})
            have.add(best["id"])
            added += 1
    if added or not cur:
        db.kv_set(f"missing:{mid}", {"ts": cur.get("ts") or 0, "players": players, "fm_ts": int(time.time())})
    return added
