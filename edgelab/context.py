"""Contexto que no sale en las estadísticas a favor y en contra: cansancio, calendario y lo que se juega cada equipo."""
from __future__ import annotations

import time

from .core import db

LEAGUES = {"EPL": (20, 4, 3), "LALIGA": (20, 4, 3), "SERIEA": (20, 4, 3), "BUNDES": (18, 4, 2), "LIGUE1": (18, 3, 2)}   # equipos, plazas Champions, descenso directo
GAMES = ("(SELECT id, kickoff, home_id, away_id, comp, status FROM matches UNION "
         "SELECT match_id, kickoff, home_id, away_id, tournament, 'finished' FROM game_meta WHERE match_id NOT IN (SELECT id FROM matches))")


def schedule(tid: int, kickoff: int) -> dict:
    prev = db.q(f"SELECT kickoff, comp FROM {GAMES} WHERE (home_id=? OR away_id=?) AND kickoff<? AND status='finished' "
                "ORDER BY kickoff DESC LIMIT 6", (tid, tid, kickoff - 3600))
    nxt = db.q1("SELECT kickoff, comp, home_name, away_name FROM matches WHERE (home_id=? OR away_id=?) AND kickoff>? ORDER BY kickoff LIMIT 1",
                (tid, tid, kickoff + 3600))
    rest = (kickoff - prev[0]["kickoff"]) / 86400 if prev else None
    n14 = sum(1 for p in prev if p["kickoff"] > kickoff - 14 * 86400)
    nxt_days = (nxt["kickoff"] - kickoff) / 86400 if nxt else None
    return {"rest_days": None if rest is None else round(rest, 1), "games_14d": n14,
            "next_days": None if nxt_days is None else round(nxt_days, 1), "next_comp": nxt["comp"] if nxt else None}


def table(comp: str, season: str | None, before: int) -> list[dict]:
    rows = db.q("SELECT home_id, away_id, home_name, away_name, hg, ag FROM matches WHERE comp=? AND season=? AND status='finished' "
                "AND hg IS NOT NULL AND kickoff<?", (comp, season, before))
    t: dict = {}
    for r in rows:
        for tid, nm, gf, ga in ((r["home_id"], r["home_name"], r["hg"], r["ag"]), (r["away_id"], r["away_name"], r["ag"], r["hg"])):
            x = t.setdefault(tid, {"id": tid, "name": nm, "pj": 0, "pts": 0, "gd": 0})
            x["pj"] += 1
            x["gd"] += gf - ga
            x["pts"] += 3 if gf > ga else 1 if gf == ga else 0
    out = sorted(t.values(), key=lambda x: (-x["pts"], -x["gd"]))
    for i, x in enumerate(out):
        x["pos"] = i + 1
    return out


def stakes(m: dict, side: str) -> dict | None:
    """Qué se juega en la liga: título, Champions, descenso o nada."""
    tid = m["home_id"] if side == "H" else m["away_id"]
    comp, season = m["comp"], m.get("season")
    if comp not in LEAGUES:      # partido de copa o Europa: se mira cómo va en su liga
        lg = db.q1("SELECT comp, season FROM matches WHERE (home_id=? OR away_id=?) AND comp IN ('EPL','LALIGA','SERIEA','BUNDES','LIGUE1') "
                   "AND kickoff<? ORDER BY kickoff DESC LIMIT 1", (tid, tid, m["kickoff"]))
        if not lg:
            return None
        comp, season = lg["comp"], lg["season"]
    n, ucl, rel = LEAGUES[comp]
    tb = table(comp, season, m["kickoff"])
    me = next((x for x in tb if x["id"] == tid), None)
    if not me or me["pj"] < 6 or len(tb) < n - 2:
        return None
    left = (n - 1) * 2 - me["pj"]
    pts_ucl = tb[ucl - 1]["pts"] if len(tb) >= ucl else None
    pts_safe = tb[n - rel - 1]["pts"] if len(tb) >= n - rel else None
    lead = tb[0]["pts"]
    out = {"pos": me["pos"], "pts": me["pts"], "left": left, "comp": comp}
    if me["pos"] <= 2 and me["pts"] >= lead - 6:
        out["fight"] = "pelea por el título"
    elif pts_ucl is not None and abs(me["pts"] - pts_ucl) <= 5:
        out["fight"] = "pelea por la Champions"
    elif pts_safe is not None and me["pts"] - pts_safe <= 4:
        out["fight"] = "pelea por no descender"
    elif left <= 6:
        out["fight"] = "sin nada en juego"
    return out


def match_context(mid: int) -> dict:
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {}
    out = {}
    for side in ("H", "A"):
        tid = m["home_id"] if side == "H" else m["away_id"]
        sc = schedule(tid, m["kickoff"])
        notes = []
        if sc["rest_days"] is not None and sc["rest_days"] < 2.8:
            notes.append(f"solo {sc['rest_days']:.0f} días de descanso".replace(".", ","))
        if sc["games_14d"] >= 5:
            notes.append(f"{sc['games_14d']} partidos en 14 días")
        if sc["next_days"] is not None and sc["next_days"] <= 3.5:
            notes.append(f"vuelve a jugar en {sc['next_days']:.0f} días{' (' + str(sc['next_comp']) + ')' if sc['next_comp'] else ''}: posibles rotaciones")
        st = stakes(m, side)
        if st and st.get("fight"):
            notes.append(f"{st['pos']}.º, {st['fight']}")
        out[side] = {**sc, "stakes": st, "notes": notes, "tired": bool(sc["rest_days"] is not None and sc["rest_days"] < 2.8) or sc["games_14d"] >= 5,
                     "rotation": bool(sc["next_days"] is not None and sc["next_days"] <= 3.5)}
    try:
        out["weather"] = weather(mid)
    except Exception:
        out["weather"] = None
    return out


# ---------------------------------------------------------------------------- clima (Open-Meteo: gratis, sin clave)
def _venue(team_id: int) -> dict | None:
    """Coordenadas del estadio del equipo local (Sofascore, una vez por equipo y se guarda para siempre)."""
    v = db.kv_get(f"venue:{team_id}")
    if v is not None:
        return v or None
    try:
        from .live.engine import sofa
        if not sofa().ok:
            return None
        t = (sofa()._get(f"/team/{team_id}") or {}).get("team") or {}
        vc = ((t.get("venue") or {}).get("venueCoordinates")) or {}
        v = {"lat": vc.get("latitude"), "lon": vc.get("longitude"), "city": ((t.get("venue") or {}).get("city") or {}).get("name"),
             "name": (t.get("venue") or {}).get("name")} if vc.get("latitude") is not None else {}
    except Exception:
        return None
    db.kv_set(f"venue:{team_id}", v)
    return v or None


def weather(mid: int) -> dict | None:
    """Tiempo previsto a la hora del partido: temperatura, lluvia y viento."""
    import json as _json
    import time as _time
    import urllib.request
    from datetime import datetime, timezone
    c = db.kv_get(f"weather3:{mid}")
    if c and _time.time() - c.get("ts", 0) < 3 * 3600:
        return c.get("w")
    m = db.q1("SELECT kickoff, home_id FROM matches WHERE id=?", (mid,))
    if not m or m["kickoff"] - _time.time() > 6 * 86400:
        return None
    try:                                   # estadio de ESTE partido (ESPN) + geocodificador; las coordenadas de Sofascore
        from .collectors.espn import venue_coords   # salían mal (Zaragoza a 28 °C en noviembre)
        mm = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
        v = venue_coords(mm)
    except Exception:
        v = None
    if not v:
        return None
    day = datetime.fromtimestamp(m["kickoff"], timezone.utc).strftime("%Y-%m-%d")
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={v['lat']}&longitude={v['lon']}&hourly=temperature_2m,precipitation,"
           f"wind_speed_10m&start_date={day}&end_date={day}&timezone=UTC")
    try:
        d = _json.loads(urllib.request.urlopen(url, timeout=15).read())
        hrs = d["hourly"]["time"]
        hh = datetime.fromtimestamp(m["kickoff"], timezone.utc).strftime("%Y-%m-%dT%H:00")
        i = hrs.index(hh) if hh in hrs else 0
        # durante el partido: la hora del saque y la siguiente
        j = min(i + 1, len(hrs) - 1)
        w = {"temp": round((d["hourly"]["temperature_2m"][i] + d["hourly"]["temperature_2m"][j]) / 2),
             "rain": round(d["hourly"]["precipitation"][i] + d["hourly"]["precipitation"][j], 1),
             "wind": round(max(d["hourly"]["wind_speed_10m"][i], d["hourly"]["wind_speed_10m"][j])),
             "city": v.get("city"), "venue": v.get("name")}
    except Exception:
        return None
    db.kv_set(f"weather3:{mid}", {"ts": int(_time.time()), "w": w})
    return w


def context_factors(m: dict) -> dict:
    """C3 + C4: multiplicadores de goles esperados por lo que se juega cada uno y por el tiempo.
    - Liga, tramo final: quien no se juega nada contra quien pelea (título, Europa, descenso) rinde algo menos.
    - Viento fuerte o mucha lluvia: algo menos de goles (peor control, menos precisión)."""
    import time as _time
    key = f"ctxf:{m['id']}"
    c = db.kv_get(key)
    if c and _time.time() - c.get("ts", 0) < 6 * 3600:
        return c["f"]
    f = {"H": 1.0, "A": 1.0, "why": []}
    try:
        if m.get("comp") in LEAGUES:
            sh, sa = stakes(m, "H") or {}, stakes(m, "A") or {}
            nada = lambda s_: s_.get("fight") == "sin nada en juego"      # noqa: E731
            pelea = lambda s_: bool(s_.get("fight")) and not nada(s_)      # noqa: E731
            for side, me, op in (("H", sh, sa), ("A", sa, sh)):
                if nada(me) and pelea(op):
                    f[side] *= 0.95
                    f["A" if side == "H" else "H"] *= 1.03
                    f["why"].append(f"{m['home_name'] if side == 'H' else m['away_name']} no se juega nada y el rival sí")
    except Exception:
        pass
    w = (db.kv_get(f"weather3:{m['id']}") or {}).get("w") or {}
    wind, rain = float(w.get("wind") or 0), float(w.get("rain") or 0)
    g = 0.94 if wind >= 35 else 0.97 if wind >= 25 else 1.0
    g *= 0.97 if rain >= 4 else 1.0
    if g < 1:
        f["H"] *= g
        f["A"] *= g
        f["why"].append(f"tiempo: viento {wind:.0f} km/h, lluvia {rain:.1f} mm")
    f["H"], f["A"] = round(f["H"], 4), round(f["A"], 4)
    db.kv_set(key, {"ts": int(_time.time()), "f": f})
    return f
