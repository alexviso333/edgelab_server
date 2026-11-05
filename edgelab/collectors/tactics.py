"""Datos tácticos: mapas de calor, posición media, entrenadores y pases.

- Statshub /api/event/{id}/team-lineup?heatmap=true → los puntos de calor de todos los jugadores de
  un equipo en un partido (una petición por equipo y partido). Se guardan resumidos: rejilla 12×8,
  posición media y número de acciones. Coordenadas relativas al equipo: x 0 portería propia → 100
  portería rival; y 0 banda derecha → 100 banda izquierda.
- Sofascore /event/{id}/managers → entrenadores (para nombrarlos en los análisis).
- Sofascore /event/{id}/player/{pid}/rating-breakdown → pases, conducciones y acciones defensivas
  con coordenadas (bajo demanda, cuando alguien abre la ficha de una apuesta de ese jugador).
"""
from __future__ import annotations

import json
import time

from ..core import db

GX, GY = 12, 8          # rejilla del mapa de calor


def grid_of(points: list[dict]) -> list[list[int]]:
    g = [[0] * GX for _ in range(GY)]
    for p in points:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            continue
        g[min(GY - 1, max(0, int(y / 100 * GY)))][min(GX - 1, max(0, int(x / 100 * GX)))] += 1
    return g


def parse_team_lineup(rows: list[dict], match_id: int, team_id: int) -> list[tuple]:
    out = []
    for p in rows or []:
        pts = p.get("heatmap") or []
        if not p.get("playerId") or not pts:
            continue
        xs = [float(q["x"]) for q in pts if q.get("x") is not None]
        ys = [float(q["y"]) for q in pts if q.get("y") is not None]
        if not xs:
            continue
        out.append((match_id, int(p["playerId"]), team_id, p.get("position"), float(p.get("minutesPlayed") or 0),
                    round(sum(xs) / len(xs), 1), round(sum(ys) / len(ys), 1), len(xs), json.dumps(grid_of(pts))))
    return out


def store_heatmaps(rows: list[tuple]) -> int:
    db.executemany("INSERT OR REPLACE INTO heatmaps(match_id,player_id,team_id,position,minutes,avg_x,avg_y,n,grid) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def clean_heatmaps() -> int:
    """Una vez: borra mapas de calor de jugadores que no jugaron ese partido (la fuente a veces mezclaba partidos)."""
    if db.kv_get("heat_cleaned"):
        return 0
    db.execute("DELETE FROM heatmaps WHERE match_id IN (SELECT match_id FROM player_stats GROUP BY match_id HAVING COUNT(*)>=15) "
                     "AND NOT EXISTS (SELECT 1 FROM player_stats ps WHERE ps.match_id=heatmaps.match_id AND ps.player_id=heatmaps.player_id)")
    db.kv_set("heat_cleaned", 1)
    return 1


def collect_heatmaps(hours_ahead: float = 96, per_team: int = 6, max_calls: int = 400, log=None) -> int:
    """Mapas de calor de los últimos partidos de cada equipo que juega pronto (solo lo que falta)."""
    clean_heatmaps()
    from ..live.engine import shub
    now = int(time.time())
    teams: list[int] = []
    for m in db.q("SELECT home_id, away_id FROM matches WHERE status IN ('notstarted','inprogress') AND kickoff BETWEEN ? AND ? "
                  "ORDER BY kickoff", (now - 3 * 3600, now + int(hours_ahead * 3600))):
        for t in (m["home_id"], m["away_id"]):
            if t and t > 0 and t not in teams:
                teams.append(t)
    calls = n = 0
    for t in teams:
        last = db.q("SELECT id FROM matches WHERE (home_id=? OR away_id=?) AND status='finished' AND kickoff<? "
                    "ORDER BY kickoff DESC LIMIT ?", (t, t, now, per_team))
        for r in last:
            if db.q1("SELECT 1 AS x FROM heatmaps WHERE match_id=? AND team_id=? LIMIT 1", (r["id"], t)):
                continue
            if db.kv_get(f"heat_none:{r['id']}:{t}"):
                continue
            if calls >= max_calls:
                return n
            calls += 1
            try:
                rows = parse_team_lineup(shub().team_lineup(r["id"], t, heatmap=True), r["id"], t)
                played = {x["player_id"] for x in db.q("SELECT player_id FROM player_stats WHERE match_id=? AND minutes>0", (r["id"],))}
                if rows and played and sum(1 for x in rows if x[1] in played) < 0.6 * len(rows):
                    rows = []          # mapa de calor de otro partido: no se guarda
                if rows:
                    n += store_heatmaps(rows)
                else:
                    db.kv_set(f"heat_none:{r['id']}:{t}", 1)
            except Exception as e:
                db.log_job("tactics", "error", f"heatmap {r['id']}/{t}: {e}")
    if log:
        log(f"{n} mapas de calor ({calls} peticiones)")
    return n


def collect_managers(hours_ahead: float = 96) -> int:
    from ..live.engine import sofa
    now = int(time.time())
    n = 0
    for m in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + int(hours_ahead * 3600))):
        if db.kv_get(f"managers:{m['id']}") is not None:
            continue
        try:
            d = sofa()._get(f"/event/{m['id']}/managers", ttl=86400)
            db.kv_set(f"managers:{m['id']}", {"H": (d.get("homeManager") or {}).get("name"),
                                              "A": (d.get("awayManager") or {}).get("name")})
            n += 1
        except Exception:
            db.kv_set(f"managers:{m['id']}", {})
    return n


def player_actions(player_id: int, team_id: int | None = None, n_matches: int = 3) -> dict:
    """Pases, conducciones y acciones defensivas (con coordenadas) de sus últimos partidos. Bajo demanda."""
    from ..live.engine import sofa
    ms = db.q("SELECT h.match_id, m.kickoff, m.home_name, m.away_name, m.home_id FROM heatmaps h JOIN matches m ON m.id=h.match_id "
              "WHERE h.player_id=? ORDER BY m.kickoff DESC LIMIT ?", (player_id, n_matches))
    if not ms:
        ms = db.q("SELECT ps.match_id, m.kickoff, m.home_name, m.away_name, m.home_id FROM player_stats ps JOIN matches m ON m.id=ps.match_id "
                  "WHERE ps.player_id=? AND ps.minutes>0 ORDER BY m.kickoff DESC LIMIT ?", (player_id, n_matches))
    out = []
    for m in ms:
        try:
            d = sofa()._get(f"/event/{m['match_id']}/player/{player_id}/rating-breakdown", ttl=86400 * 60)
        except Exception:
            continue
        flip = None

        def pt(c):
            return {"x": c.get("x"), "y": c.get("y")} if c else None
        passes = [{"a": pt(p.get("playerCoordinates")), "b": pt(p.get("passEndCoordinates")), "ok": p.get("outcome", True),
                   "key": bool(p.get("keypass"))} for p in d.get("passes") or []]
        carries = [{"a": pt(p.get("playerCoordinates")), "b": pt(p.get("passEndCoordinates") or p.get("carryEndCoordinates"))}
                   for p in d.get("ball-carries") or []]
        defensive = [{"a": pt(p.get("playerCoordinates")), "t": p.get("eventActionType")} for p in d.get("defensive") or []]
        dribbles = [{"a": pt(p.get("playerCoordinates")), "ok": p.get("outcome", True)} for p in d.get("dribbles") or []]
        out.append({"match_id": m["match_id"], "kickoff": m["kickoff"], "label": f"{m['home_name']} – {m['away_name']}",
                    "passes": passes, "carries": carries, "defensive": defensive, "dribbles": dribbles, "flip": flip})
    return {"player_id": player_id, "matches": out}



SP_SITUATIONS = {"penalty", "free-kick", "corner", "set-piece"}


def collect_set_pieces(hours_ahead: float = 96, per_team: int = 12, max_calls: int = 150, log=None) -> int:
    """Quién tira los penaltis y las faltas: tiros a balón parado (mapa de tiros de Sofascore) de los últimos
    partidos de todas las competiciones de cada equipo que juega pronto."""
    from ..live.engine import sofa
    if not sofa().ok:
        return 0
    now = int(time.time())
    teams: list[int] = []
    for m in db.q("SELECT home_id, away_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? ORDER BY kickoff",
                  (now, now + int(hours_ahead * 3600))):
        for t in (m["home_id"], m["away_id"]):
            if t and t > 0 and t not in teams:
                teams.append(t)
    calls = n = 0
    games = ("(SELECT id, kickoff, home_id, away_id FROM matches WHERE status='finished' UNION "
             "SELECT match_id, kickoff, home_id, away_id FROM game_meta WHERE match_id NOT IN (SELECT id FROM matches))")
    for t in teams:
        for g in db.q(f"SELECT * FROM {games} WHERE (home_id=? OR away_id=?) AND kickoff<? ORDER BY kickoff DESC LIMIT ?",
                      (t, t, now, per_team)):
            if db.kv_get(f"sp_done:{g['id']}"):
                continue
            if calls >= max_calls:
                return n
            calls += 1
            try:
                shots = sofa().shotmap(g["id"], ttl=86400 * 30)
            except Exception:
                shots = []
            rows = []
            for sh in shots or []:
                sit = sh.get("situation")
                if sit not in SP_SITUATIONS:
                    continue
                pl = sh.get("player") or {}
                rows.append((g["id"], g["home_id"] if sh.get("isHome") else g["away_id"], pl.get("id"), pl.get("name"), sit,
                             float(sh.get("time") or 0), float(sh.get("xg") or 0), sh.get("shotType"), g["kickoff"]))
            db.execute("DELETE FROM set_pieces WHERE match_id=?", (g["id"],))
            if rows:
                db.executemany("INSERT INTO set_pieces VALUES(?,?,?,?,?,?,?,?,?)", rows)
            db.kv_set(f"sp_done:{g['id']}", 1 if shots else 0)
            n += 1
    if log:
        log(f"balón parado: {n} partidos ({calls} peticiones)")
    return n


def set_piece_roles(team_id: int, before: int, n_games: int = 20) -> dict:
    """{'penalty': [(player_id, name, veces)], 'free-kick': [...]} en sus últimos partidos."""
    rows = db.q("SELECT player_id, name, situation, COUNT(*) AS n FROM set_pieces WHERE team_id=? AND kickoff<? AND kickoff>? "
                "AND situation IN ('penalty','free-kick') GROUP BY player_id, situation ORDER BY n DESC",
                (team_id, before, before - 300 * 86400))
    out: dict = {"penalty": [], "free-kick": []}
    for r in rows:
        out[r["situation"]].append((r["player_id"], r["name"], r["n"]))
    return out


# área rival en la rejilla 12×8: x ≥ 83 (columnas 10-11) y la franja central y 25-75 (filas 2-5)
BOX_COLS, BOX_ROWS = (10, 11), (2, 3, 4, 5)


def box_points(g) -> tuple[float, float]:
    """(acciones dentro del área rival, acciones totales) de una rejilla."""
    tot = float(sum(sum(r) for r in g))
    box = float(sum(g[y][x] for y in BOX_ROWS for x in BOX_COLS))
    return box, tot


def heat_profiles(pids, before: int, n: int = 8) -> dict:
    """Por jugador, con sus últimos `n` mapas de calor (una sola consulta):
    grid normalizada, cuota de acciones en el área rival reciente (3 últimos) y anterior, y nº de partidos."""
    import numpy as np
    ids = ",".join(str(int(i)) for i in pids if i)
    if not ids:
        return {}
    rows = db.q(f"SELECT h.player_id, h.grid FROM heatmaps h JOIN matches m ON m.id=h.match_id WHERE h.player_id IN ({ids}) "
                f"AND m.kickoff<? ORDER BY m.kickoff DESC", (before,))
    by: dict = {}
    for r in rows:
        if len(by.setdefault(r["player_id"], [])) < n:
            try:
                by[r["player_id"]].append(json.loads(r["grid"]))
            except Exception:
                pass
    out = {}
    for pid, gs in by.items():
        g = np.zeros((GY, GX))
        for x in gs:
            g += np.array(x, float)
        if g.sum() <= 0:
            continue
        def share(lst):
            b = t = 0.0
            for x in lst:
                bb, tt = box_points(x)
                b, t = b + bb, t + tt
            return b, t
        rb, rt = share(gs[:3])
        ob, ot = share(gs[3:])
        out[pid] = {"grid": g / g.sum(), "n": len(gs), "box_recent": (rb, rt), "box_older": (ob, ot), "box": share(gs)}
    return out


def box_trend(prof: dict | None, k: float = 40.0) -> float | None:
    """Cuánto pisa ahora el área rival frente a antes (≈1 = igual). Se contrae hacia «antes» con k acciones de
    margen para que un partido raro no cambie nada. None si no hay datos suficientes."""
    if not prof or prof["n"] < 5:
        return None
    (rb, rt), (ob, ot) = prof["box_recent"], prof["box_older"]
    if rt < 30 or ot < 40 or rb + ob < 8:          # casi nunca pisa el área: no hay tendencia que medir
        return None
    os_ = max(ob, 0.5) / ot
    rs = (rb + k * os_) / (rt + k)
    return rs / os_
