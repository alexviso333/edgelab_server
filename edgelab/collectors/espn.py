"""ESPN (API pública, gratis, sin clave): sustituto de Sofascore para lo que nos bloquea.

- Goles, tarjetas y rojas con su minuto → cronología del partido (quién marcó primero, cuándo, rojas).
- Estadio y ciudad de cada partido → coordenadas (geocodificador de Open-Meteo) → clima previsto.
Una petición por competición y día, guardada en caché.
"""
from __future__ import annotations

import difflib
import json
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone

from ..core import db

SLUG = {"UCL": "uefa.champions", "UEL": "uefa.europa", "UECL": "uefa.europa.conf", "LALIGA": "esp.1", "EPL": "eng.1",
        "SERIEA": "ita.1", "BUNDES": "ger.1", "LIGUE1": "fra.1", "CDR": "esp.copa_del_rey"}
_UA = {"User-Agent": "Mozilla/5.0 edgelab"}
_STOP = {"fc", "cf", "cd", "ud", "sd", "afc", "ac", "as", "sc", "rc", "real", "club", "de", "del", "la", "1.", "fk", "sk", "vfb", "vfl",
         "tsg", "rb", "ss", "ssc", "us", "olympique", "sporting", "athletic", "atletico", "the", "and", "united", "city"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _STOP]
    return " ".join(toks) or s


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def scoreboard(comp: str, ts: int, live: bool = False) -> list[dict]:
    slug = SLUG.get(comp)
    if not slug:
        return []
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
    key = f"espn:{slug}:{day}"
    c = db.kv_get(key)
    fresh = (60 if live else 600) if abs(time.time() - ts) < 6 * 3600 else 6 * 3600
    if c and time.time() - c.get("ts", 0) < fresh:
        return c["events"]
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={day}"
        d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=15).read())
    except Exception:
        return (c or {}).get("events", [])
    evs = []
    for e in d.get("events", []):
        comp_ = (e.get("competitions") or [{}])[0]
        teams = {x.get("homeAway"): x for x in comp_.get("competitors", [])}
        if "home" not in teams or "away" not in teams:
            continue
        ids = {teams["home"]["team"]["id"]: "H", teams["away"]["team"]["id"]: "A"}
        det = []
        for x in comp_.get("details", []):
            t = ((x.get("type") or {}).get("text") or "").lower()
            mm = re.match(r"(\d+)(?:'\+(\d+))?", ((x.get("clock") or {}).get("displayValue") or "").replace("’", "'"))
            minute = float(mm.group(1)) + (float(mm.group(2)) / 10 if mm and mm.group(2) else 0) if mm else None
            side = ids.get(str((x.get("team") or {}).get("id")))
            kind = "goal" if "goal" in t and "disallowed" not in t else "red" if "red card" in t else "yellow" if "yellow" in t else None
            if kind and minute is not None and side:
                det.append({"kind": kind, "minute": minute, "side": side, "own": "own goal" in t,
                            "player": ", ".join(a.get("displayName", "") for a in (x.get("athletesInvolved") or []))})
        v = comp_.get("venue") or {}
        stt = (e.get("status") or {})
        def _sc(x):
            try:
                return int(x.get("score"))
            except Exception:
                return None
        evs.append({"id": e.get("id"), "hid": str(teams["home"]["team"]["id"]), "aid": str(teams["away"]["team"]["id"]),
                    "home": teams["home"]["team"]["displayName"], "away": teams["away"]["team"]["displayName"],
                    "hg": _sc(teams["home"]), "ag": _sc(teams["away"]), "clock": stt.get("displayClock"),
                    "period": stt.get("period"), "detail": ((stt.get("type") or {}).get("shortDetail") or ""),
                    "ts": int(datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()) if e.get("date") else None,
                    "state": ((e.get("status") or {}).get("type") or {}).get("state"), "details": det,
                    "sname": ((e.get("status") or {}).get("type") or {}).get("name") or "",
                    "venue": v.get("fullName"), "city": (v.get("address") or {}).get("city"), "country": (v.get("address") or {}).get("country")})
    db.kv_set(key, {"ts": int(time.time()), "events": evs})
    return evs


def find_event(m: dict, live: bool = False) -> dict | None:
    best, bs = None, 0.0
    for e in scoreboard(m["comp"], m["kickoff"], live):
        sc = (_sim(m["home_name"], e["home"]) + _sim(m["away_name"], e["away"])) / 2
        if e.get("ts") and abs(e["ts"] - m["kickoff"]) > 6 * 3600:
            sc -= 0.3
        if sc > bs:
            best, bs = e, sc
    return best if bs >= 0.62 else None


def store_timeline(mid: int) -> int:
    """Goles y rojas con minuto (si Sofascore no nos dio la cronología)."""
    if db.q1("SELECT 1 AS x FROM timeline WHERE match_id=? AND kind='goal' LIMIT 1", (mid,)):
        return 0
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return 0
    e = find_event(m)
    if not e or e.get("state") != "post" or cancel_status(e):
        return 0
    rows = [(mid, d["minute"], d["kind"], d["side"], None, d["player"], "espn") for d in e["details"] if d["kind"] in ("goal", "red")]
    db.execute("DELETE FROM timeline WHERE match_id=? AND source='espn'", (mid,))
    if rows:
        db.executemany("INSERT INTO timeline(match_id,minute,kind,side,xg,player,source) VALUES(?,?,?,?,?,?,?)", rows)
    return len(rows)


def venue_coords(m: dict) -> dict | None:
    """Estadio y ciudad del partido (ESPN) → coordenadas (geocodificador gratuito de Open-Meteo)."""
    e = find_event(m)
    if not e or not e.get("city"):
        return None
    if not e.get("country"):          # sin país, el del equipo local (si no, «Zaragoza» puede caer en México)
        t = db.q1("SELECT country FROM teams WHERE id=?", (m["home_id"],)) or {}
        e = {**e, "country": t.get("country") or ("Spain" if m.get("comp") in ("CDR", "LALIGA") else None)}
    key = f"geo2:{_norm(e['city'])}:{_norm(e.get('country') or '')}"
    g = db.kv_get(key)
    if g is None:
        try:
            q = urllib.parse.quote(e["city"])
            d = json.loads(urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=5", timeout=15).read())
            res = d.get("results") or []
            ctry = _norm(e.get("country") or "")
            ES = {"spain": "españa", "germany": "alemania", "england": "united kingdom", "turkiye": "turkey"}
            want = e.get("country") or ""
            pick = next((r for r in res if want and (_sim(r.get("country", ""), want) > 0.7 or
                                                      _sim(r.get("country", ""), ES.get(_norm(want), want)) > 0.7 or
                                                      r.get("country_code", "").lower() == want.lower()[:2])), None)
            if pick is None:
                pick = res[0] if res and not want else None
            g = {"lat": pick["latitude"], "lon": pick["longitude"]} if pick else {}
        except Exception:
            return None
        db.kv_set(key, g)
    return {**g, "city": e["city"], "name": e.get("venue")} if g else None


import urllib.parse  # noqa: E402


def cancel_status(e: dict) -> str | None:
    sn = (e.get("sname") or "").upper()
    det = (e.get("detail") or "").lower()
    if "POSTPONED" in sn or "postponed" in det or "DELAYED" in sn and e.get("state") == "post":
        return "postponed"
    if any(k in sn for k in ("CANCELED", "CANCELLED", "ABANDONED", "SUSPENDED", "FORFEIT")) or \
            any(k in det for k in ("canceled", "cancelled", "abandoned", "suspended")):
        return "canceled"
    return None


def live_state(m: dict) -> dict | None:
    """Marcador, minuto y si ha acabado (cada minuto como mucho), con el mismo formato que el de Sofascore."""
    e = find_event(m, live=True)
    if not e:
        return None
    st = e.get("state")
    det = (e.get("detail") or "").lower()
    mm = re.match(r"(\d+)(?:'\+(\d+))?", (e.get("clock") or "").replace("’", "'"))
    minute = float(mm.group(1)) + (float(mm.group(2)) if mm and mm.group(2) else 0) if mm else None
    typ = "finished" if st == "post" else "inprogress" if st == "in" else "notstarted"
    # ¡OJO! ESPN pone «post» también a los aplazados/suspendidos/cancelados (no son 0-0 terminados)
    bad = cancel_status(e)
    if bad:
        typ = bad
    reds = (sum(1 for d in e["details"] if d["kind"] == "red" and d["side"] == "H"),
            sum(1 for d in e["details"] if d["kind"] == "red" and d["side"] == "A"))
    return {"type": typ, "desc": det, "hg": e.get("hg"), "ag": e.get("ag"), "ht": det.startswith("ht") or "half" in det,
            "minute": minute if typ == "inprogress" else None, "reds": reds}


# ------------------------------------------------------------------ estadísticas en directo (cada minuto, sin bloqueos)
_SUM: dict = {}
TEAM_MAP = {"totalShots": "shots", "shotsOnTarget": "sot", "wonCorners": "corners", "foulsCommitted": "fouls",
            "possessionPct": "poss", "yellowCards": "yellow", "redCards": "red", "offsides": "offsides", "saves": "saves",
            "blockedShots": "blocked", "totalCrosses": "crosses", "totalTackles": "tackles", "interceptions": "interceptions",
            "totalClearance": "clearances", "totalPasses": "passes", "accuratePasses": "passes_ok", "totalLongBalls": "long_balls"}
# ESPN (jugador) → clave de Sofascore en las alineaciones
PLAYER_MAP = {"totalShots": "totalShots", "shotsOnTarget": "onTargetScoringAttempt", "foulsCommitted": "fouls",
              "foulsSuffered": "wasFouled", "totalGoals": "goals", "yellowCards": "yellowCard", "expectedGoals": "expectedGoals",
              "offsides": "totalOffside"}
# solo ESPN las da (Sofascore no): se copian tal cual
PLAYER_ONLY = {"headShots": "_headShots", "headSot": "_headSot", "outShots": "_outShots", "outSot": "_outSot", "ownXg": "_ownXg"}


def _flag(x) -> bool:
    if isinstance(x, dict):
        return bool(x.get("didSub", True))
    return bool(x)


def parse_summary(d: dict, hid: str | None = None, aid: str | None = None) -> dict:
    def side_of(t: dict, i: int) -> str:
        ha = t.get("homeAway")
        if ha in ("home", "away"):
            return "H" if ha == "home" else "A"
        tid = str((t.get("team") or {}).get("id"))
        if hid and tid == hid:
            return "H"
        if aid and tid == aid:
            return "A"
        return "H" if i == 0 else "A"
    T = {"H": {}, "A": {}}
    for i, t in enumerate((d.get("boxscore") or {}).get("teams") or []):
        sd = side_of(t, i)
        for s_ in t.get("statistics") or []:
            k = TEAM_MAP.get(s_.get("name"))
            if not k:
                continue
            try:
                T[sd][k] = float(s_.get("displayValue") if s_.get("displayValue") not in (None, "") else s_.get("value"))
            except (TypeError, ValueError):
                pass
    P = {"H": [], "A": []}
    xg = {"H": None, "A": None}
    for i, r in enumerate(d.get("rosters") or []):
        sd = side_of(r, i)
        for p in r.get("roster") or []:
            a = p.get("athlete") or {}
            st = {}
            for s_ in p.get("stats") or []:
                try:
                    st[s_.get("name")] = float(s_.get("value"))
                except (TypeError, ValueError):
                    pass
            if "expectedGoals" in st:
                xg[sd] = (xg[sd] or 0.0) + st["expectedGoals"]
            P[sd].append({"name": a.get("displayName") or a.get("fullName") or "", "short": a.get("shortName") or "",
                          "jersey": str(p.get("jersey") or a.get("jersey") or ""), "starter": bool(p.get("starter")),
                          "sub_in": _flag(p.get("subbedIn")), "sub_out": _flag(p.get("subbedOut")), "st": st})
    if not P["H"] and not P["A"]:
        xg = {"H": None, "A": None}
    tid = {}
    for i, t in enumerate((d.get("boxscore") or {}).get("teams") or []):
        tid[str((t.get("team") or {}).get("id"))] = side_of(t, i)
        tid[_norm((t.get("team") or {}).get("displayName") or "")] = side_of(t, i)     # en el comentario solo viene el nombre
    plays = parse_plays(d.get("commentary") or [], tid)
    own = {"H": 0.0, "A": 0.0}
    for x in plays["shots"]:
        own[x["side"]] += x["xg"]
    if xg["H"] is None and plays["shots"] is not None and (P["H"] or plays["shots"]):
        xg = {"H": own["H"], "A": own["A"]}         # durante el partido ESPN no da xG: el nuestro, tiro a tiro
    # por jugador: tiros de cabeza / desde fuera del área (mercados propios en bet365)
    for sd in ("H", "A"):
        for p in P[sd]:
            mine = [x for x in plays["shots"] if x["side"] == sd and x["player"] and _sim(x["player"], p["name"]) >= 0.9]
            if mine:
                p["st"]["headShots"] = sum(1 for x in mine if x["body"] == "head")
                p["st"]["headSot"] = sum(1 for x in mine if x["body"] == "head" and x["res"] in ("on", "goal"))
                p["st"]["outShots"] = sum(1 for x in mine if x["zone"] == "out")
                p["st"]["outSot"] = sum(1 for x in mine if x["zone"] == "out" and x["res"] in ("on", "goal"))
                p["st"]["ownXg"] = round(sum(x["xg"] for x in mine), 3)
    return {"T": T, "P": P, "xg": (None if xg["H"] is None else round(xg["H"], 2), None if xg["A"] is None else round(xg["A"], 2)),
            "own_xg": (round(own["H"], 2), round(own["A"], 2)), "shots": plays["shots"], "mom": plays["mom"],
            "events": plays["events"], "ts": int(time.time())}


# ------------------------------------------------------------------ jugada a jugada (comentario de ESPN)
import math  # noqa: E402

_MIN = re.compile(r"(\d+)'(?:\+(\d+)')?")


def _minute(txt: str | None) -> float | None:
    mm = _MIN.match((txt or "").replace("’", "'"))
    if not mm:
        return None
    return float(mm.group(1)) + (float(mm.group(2)) if mm.group(2) else 0.0)


def shot_xg(x: float | None, y: float | None, body: str, zone: str, penalty: bool, free_kick: bool) -> float:
    """xG propio por tiro con la posición que da ESPN (x: 0→100 hacia la portería rival; y: 0→100 a lo ancho).
    Modelo clásico distancia + ángulo de portería, con castigo a los remates de cabeza."""
    if penalty:
        return 0.76
    if x is not None and y is not None and x > 40:
        dx = max(0.5, (100 - x) * 1.05)
        dy = (y - 50) * 0.68
        d = math.hypot(dx, dy)
        ang = math.atan2(7.32 * dx, dx * dx + dy * dy - 3.66 ** 2)
        if ang < 0:
            ang += math.pi
        z = -0.6 - 0.11 * d + 1.0 * ang
    else:
        z = {"six": 0.0, "box": -1.9, "side": -2.6, "out": -3.3}.get(zone, -2.4)
    if body == "head":
        z -= 0.9
    if free_kick:
        z -= 0.3
    return round(min(0.95, max(0.01, 1 / (1 + math.exp(-z)))), 3)


def parse_plays(comm: list, tid: dict) -> dict:
    """Tiros (con posición, de cabeza o no, zona, resultado y xG propio), córners, faltas, fueras de juego, tarjetas
    y cambios con su minuto exacto, y la «presión» de los últimos 10 minutos de cada equipo."""
    shots, events = [], []
    last_min = 0.0
    for c in comm:
        p = c.get("play")
        if not p:
            continue
        t = ((p.get("type") or {}).get("text") or "").lower()
        txt = (c.get("text") or p.get("text") or "")
        lt = txt.lower()
        minute = _minute((c.get("time") or {}).get("displayValue") or (p.get("clock") or {}).get("displayValue"))
        try:
            wall = datetime.fromisoformat(str(p.get("wallclock")).replace("Z", "+00:00")).timestamp() if p.get("wallclock") else None
        except ValueError:
            wall = None
        if minute is None:
            continue
        last_min = max(last_min, minute)
        tm = p.get("team") or {}
        side = tid.get(str(tm.get("id"))) or tid.get(_norm(tm.get("displayName") or ""))
        parts = [((a.get("athlete") or {}).get("displayName") or "") for a in (p.get("participants") or [])]
        if not side:
            continue
        if "shot" in t or t.startswith("goal") or "penalty - scored" in t or "penalty - missed" in t or "penalty - saved" in t:
            if "own goal" in lt:
                continue
            res = "goal" if (t.startswith("goal") or "scored" in t) else "on" if ("on target" in t or "saved" in t) else \
                  "blocked" if "blocked" in t else "off"
            body = "head" if "header" in lt else "left" if "left footed" in lt else "right" if "right footed" in lt else "other"
            zone = ("out" if "outside the box" in lt or "more than 35 yards" in lt or "long range" in lt else
                    "six" if "six yard box" in lt else "side" if ("side of the box" in lt or "difficult angle" in lt) else
                    "box" if "box" in lt else "?")
            pen = "penalty" in lt or "penalty" in t
            fk = "free kick" in lt
            xg = shot_xg(p.get("fieldPositionX"), p.get("fieldPositionY"), body, zone, pen, fk)
            shots.append({"minute": minute, "wall": wall, "side": side, "player": parts[0] if parts else "", "res": res, "body": body,
                          "zone": zone, "pen": pen, "fk": fk, "cross": "with a cross" in lt, "xg": xg,
                          "x": p.get("fieldPositionX"), "y": p.get("fieldPositionY")})
        elif "corner" in t:
            events.append({"minute": minute, "wall": wall, "side": side, "kind": "corner"})
        elif t == "foul" or "free kick" in t:
            # cada falta sale dos veces: «Foul by X (equipo)» y «Y (equipo) wins a free kick…»
            if lt.startswith("foul by"):
                events.append({"minute": minute, "wall": wall, "side": side, "kind": "foul", "player": parts[0] if parts else ""})
            elif "wins a free kick" in lt:
                events.append({"minute": minute, "wall": wall, "side": side, "kind": "fouled", "player": parts[0] if parts else ""})
        elif "offside" in t:
            events.append({"minute": minute, "wall": wall, "side": side, "kind": "offside", "player": parts[0] if parts else ""})
        elif "red card" in t or "second yellow" in lt:
            events.append({"minute": minute, "wall": wall, "side": side, "kind": "red", "player": parts[0] if parts else ""})
        elif "yellow card" in t:
            events.append({"minute": minute, "wall": wall, "side": side, "kind": "yellow", "player": parts[0] if parts else ""})
        elif "substitution" in t:
            events.append({"minute": minute, "wall": wall, "side": side, "kind": "sub", "player_in": parts[0] if parts else "",
                           "player_out": parts[1] if len(parts) > 1 else ""})
    # presión: xG de sus tiros + córners en los últimos 10'
    mom = {"H": 0.0, "A": 0.0}
    for x in shots:
        if x["minute"] >= last_min - 10:
            mom[x["side"]] += x["xg"] + 0.03
    for e in events:
        if e["minute"] < last_min - 10:
            continue
        if e["kind"] == "corner":
            mom[e["side"]] += 0.03
    tot = mom["H"] + mom["A"]
    return {"shots": shots, "events": events,
            "mom": {"H": round(mom["H"], 2), "A": round(mom["A"], 2), "share_h": round(mom["H"] / tot, 2) if tot > 0.05 else None,
                    "upto": last_min}}


def live_summary(m: dict, max_age: float = 50) -> dict | None:
    """Estadísticas de equipo y de cada jugador en directo (tiros, a puerta, faltas, córners, posesión, xG por jugador)."""
    c = _SUM.get(m["id"])
    if c and time.time() - c["ts"] < max_age:
        return c
    e = find_event(m, live=True)
    if not e or not e.get("id"):
        return None
    slug = SLUG.get(m["comp"])
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/summary?event={e['id']}"
        d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=12).read())
    except Exception:
        return c
    out = parse_summary(d, e.get("hid"), e.get("aid"))
    if not any(out["T"][s] for s in ("H", "A")) and not out["P"]["H"]:
        return c
    _SUM[m["id"]] = out
    return out


def _num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _after(es: dict, t0: float | None):
    """Jugadas de ESPN ocurridas DESPUÉS de la última consulta a Sofascore (por su hora real). ESPN va 1-2 min tarde:
    comparar totales contaría dos veces lo que Sofascore ya tenía y ESPN aún no; así no se duplica nada (lo que pasó
    justo antes y Sofascore aún no tenía se recoge en su siguiente consulta)."""
    sh = [x for x in es.get("shots") or [] if t0 is None or (x.get("wall") and x["wall"] > t0)]
    ev = [x for x in es.get("events") or [] if t0 is None or (x.get("wall") and x["wall"] > t0)]
    return sh, ev


def merge_team(sofa: dict | None, es: dict, t0: float | None) -> dict:
    """Base de Sofascore (más completa y más al día) + lo ocurrido desde entonces según ESPN."""
    if not sofa or not (sofa.get("H") or sofa.get("A")):
        return {sd: dict((es.get("T") or {}).get(sd) or {}) for sd in ("H", "A")}       # sin Sofascore: ESPN tal cual
    sh, ev = _after(es, t0)
    out = {}
    for sd in ("H", "A"):
        b = dict(sofa.get(sd) or {})
        add = {"shots": sum(1 for x in sh if x["side"] == sd),
               "sot": sum(1 for x in sh if x["side"] == sd and x["res"] in ("on", "goal")),
               "corners": sum(1 for e in ev if e["side"] == sd and e["kind"] == "corner"),
               "fouls": sum(1 for e in ev if e["side"] == sd and e["kind"] == "foul"),
               "offsides": sum(1 for e in ev if e["side"] == sd and e["kind"] == "offside"),
               "yellow": sum(1 for e in ev if e["side"] == sd and e["kind"] == "yellow")}
        for k, v in add.items():
            if k not in b:
                tv = ((es.get("T") or {}).get(sd) or {}).get(k)
                if tv is not None:
                    b[k] = tv            # Sofascore no lo tenía: el total de ESPN
            elif v:
                b[k] = _num(b[k]) + v
        for k, v in ((es.get("T") or {}).get(sd) or {}).items():
            if k not in b:
                b[k] = v                 # lo que Sofascore no da (paradas, centros, entradas…): ESPN
        if "poss" in ((es.get("T") or {}).get(sd) or {}) and "poss" not in (sofa.get(sd) or {}):
            b["poss"] = es["T"][sd]["poss"]
        out[sd] = b
    return out


def merge_xg(sofa_xg, es: dict, t0: float | None):
    """xG de Sofascore + xG propio (por posición del tiro) de los tiros posteriores según ESPN."""
    if not sofa_xg or sofa_xg[0] is None:
        return es.get("xg") or (None, None)
    sh, _ = _after(es, t0)
    return tuple(round(_num(sofa_xg[i]) + sum(x["xg"] for x in sh if x["side"] == sd), 2) for i, sd in enumerate(("H", "A")))


def overlay_lineup(lu_raw: dict, es: dict, t0: float | None) -> dict:
    """Pone al día las stats por jugador de la alineación de Sofascore con las jugadas de ESPN posteriores a ella
    (empareja por dorsal y, si no, por nombre). Marca cambios y añade los tiros de cabeza / desde fuera del área."""
    import copy
    out = copy.deepcopy(lu_raw)
    sh, ev = _after(es, t0)
    for sd, key in (("H", "home"), ("A", "away")):
        players = (out.get(key) or {}).get("players") or []
        es_pl = (es.get("P") or {}).get(sd, [])
        for p in players:
            pl = p.get("player") or {}
            jersey = str(p.get("jerseyNumber") or pl.get("jerseyNumber") or "")
            nm = pl.get("name") or pl.get("shortName") or ""
            hit = next((e for e in es_pl if jersey and e["jersey"] == jersey and _sim(nm, e["name"]) > 0.45), None)
            if hit is None:
                best = max(es_pl, key=lambda e: max(_sim(nm, e["name"]), _sim(pl.get("shortName") or nm, e["short"])), default=None)
                if best and max(_sim(nm, best["name"]), _sim(pl.get("shortName") or nm, best["short"])) >= 0.8:
                    hit = best
            if hit is None:
                continue
            en = hit["name"]
            mine = lambda name: bool(name) and _sim(name, en) >= 0.9          # noqa: E731
            st = dict(p.get("statistics") or {})
            ms = [x for x in sh if x["side"] == sd and mine(x["player"])]
            me = [e for e in ev if e["side"] == sd and mine(e.get("player"))]
            if ms:
                cur = st.get("totalShots")
                if cur is None:
                    cur = sum(_num(st.get(x)) for x in ("onTargetScoringAttempt", "shotOffTarget", "blockedScoringAttempt", "hitWoodwork"))
                st["totalShots"] = _num(cur) + len(ms)
                st["onTargetScoringAttempt"] = _num(st.get("onTargetScoringAttempt")) + sum(1 for x in ms if x["res"] in ("on", "goal"))
                st["goals"] = _num(st.get("goals")) + sum(1 for x in ms if x["res"] == "goal")
                st["expectedGoals"] = round(_num(st.get("expectedGoals")) + sum(x["xg"] for x in ms), 3)
            for kind, sk in (("foul", "fouls"), ("fouled", "wasFouled"), ("yellow", "yellowCard"), ("offside", "totalOffside")):
                n = sum(1 for e in me if e["kind"] == kind)
                if n:
                    st[sk] = _num(st.get(sk)) + n
            for ek, sk in PLAYER_ONLY.items():           # todo el partido (Sofascore no los da)
                if ek in hit["st"]:
                    st[sk] = hit["st"][ek]
            if st.get("expectedGoals") is None and "ownXg" in hit["st"]:
                st["expectedGoals"] = hit["st"]["ownXg"]
            if hit["sub_out"]:
                st["_off"] = 1
            if hit["sub_in"] and st.get("minutesPlayed") is None:
                st["minutesPlayed"] = 1
            p["statistics"] = st
    return out
