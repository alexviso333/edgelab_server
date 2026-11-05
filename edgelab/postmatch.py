"""Después de cada partido: traer TODO lo que pasó, resolver las apuestas y compararlo con lo que esperábamos.

1. Antes del saque (≈40 min) se guarda la «foto» de lo que esperábamos: goles, córners, tarjetas, tiros, faltas…
   de cada equipo y, para cada jugador del once, sus tiros, tiros a puerta, faltas, faltas recibidas y entradas.
2. Al acabar se trae de Sofascore la estadística final del partido y de cada jugador (minutos incluidos) y se
   resuelven en el momento todas las apuestas abiertas (recomendaciones, escáner y las tuyas).
3. Se comparan esperado y real: por equipo y por jugador. Separando el error de minutos (jugó menos) del error de
   ritmo (con los minutos que jugó, hizo más o menos de lo esperado).
4. Con muchos partidos se aprende el sesgo de cada estadística (p. ej. «sobrestimamos los córners un 6 %») y el
   modelo lo corrige, con prudencia (se necesitan muchos partidos para mover algo).
"""
from __future__ import annotations

import json
import math
import time

from .core import db

PSTATS = ("shots", "sot", "fouls", "was_fouled", "tackles")
TEAM_CODES = ("CORNERS", "CARDS", "SHOTS", "SOT", "FOULS", "OFFSIDES", "SAVES")
K_BIAS = 25.0      # partidos «imaginarios» sin sesgo: hasta tener bastantes, la corrección es mínima


# ---------------------------------------------------------------------------- 1. lo que esperábamos
def pre_snapshot(mid: int, force: bool = False) -> dict:
    cur = db.kv_get(f"pre:{mid}")
    if cur and not force:
        return cur
    from .insights import match_simulation, player_rate, _player_rows
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {}
    sim = match_simulation(mid) or {}
    teams = {x["code"]: {"H": round(x["home"], 2), "A": round(x["away"], 2)} for x in sim.get("stats", [])}
    lam = sim.get("lam") or {}
    lu = db.q("SELECT player_id, name, side, starter, source FROM lineups WHERE match_id=? AND source IN ('confirmed','predicted')", (mid,))
    if not lu and time.time() > m["kickoff"]:
        # partido ya jugado sin once guardado: los que salieron de titular (lo esperado sigue usando solo datos de antes)
        lu = db.q("SELECT ps.player_id, COALESCE(p.name, '') AS name, ps.side, ps.starter, 'played' AS source FROM player_stats ps "
                  "LEFT JOIN players p ON p.id=ps.player_id WHERE ps.match_id=? AND ps.starter=1", (mid,))
    players = []
    for r in lu:
        pid = r["player_id"]
        rows = _player_rows(pid, m["kickoff"], 10)
        starts = sorted(x["minutes"] for x in rows if x["starter"] and (x["minutes"] or 0) > 0)
        exp_min = (min(88.0, starts[len(starts) // 2]) if starts else 75.0) if r["starter"] else 15.0
        rates = {}
        for k in PSTATS:
            v, n, mins = player_rate(pid, m["kickoff"], k, 15)
            if v is not None:
                rates[k] = round(v, 3)
        if rates:
            players.append({"id": pid, "name": r["name"], "side": r["side"], "starter": bool(r["starter"]), "exp_min": round(exp_min),
                            "rate90": rates, "exp": {k: round(v * exp_min / 90, 2) for k, v in rates.items()}})
    rows = []
    if time.time() < m["kickoff"]:            # solo antes del partido: después las cuotas ya no son las de antes
        try:
            from .pricing import price_match
            pr = price_match(mid) or {}
            for r in pr.get("rows", []):
                if r.get("p") is None:
                    continue
                rows.append({"m": r["market"], "pe": r.get("period") or "FT", "s": r["selection"], "l": r.get("line"),
                             "g": r.get("group"), "lb": r.get("label"), "p": r["p"], "pu": r.get("push") or 0,
                             "f": r.get("fair"), "o": r.get("b365"), "ev": r.get("ev")})
        except Exception as e:
            db.log_job("postmatch", "error", f"foto mercados {mid}: {e}")
    snap = {"ts": int(time.time()), "lam": {"H": lam.get("lh"), "A": lam.get("la")}, "rows": rows,
            "p1x2": [sim.get("home_win"), sim.get("draw"), sim.get("away_win")], "teams": teams, "players": players,
            "lineup": (lu[0]["source"] if lu else None), "retro": time.time() > m["kickoff"]}
    db.kv_set(f"pre:{mid}", snap)
    return snap


def snapshot_upcoming(minutes_ahead: int = 45) -> int:
    now = int(time.time())
    n = 0
    for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + minutes_ahead * 60)):
        cur = db.kv_get(f"pre:{r['id']}")
        if not cur or now - cur.get("ts", 0) > 1800:      # se rehace cerca del saque para pillar el once oficial
            try:
                pre_snapshot(r["id"], force=True)
                n += 1
            except Exception as e:
                db.log_job("postmatch", "error", f"foto previa {r['id']}: {e}")
    return n


# ---------------------------------------------------------------------------- 2. lo que pasó
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_final(mid: int) -> dict:
    """Estadística final del partido y de cada jugador desde Sofascore (si no la hay ya de Statshub)."""
    from .live.engine import sofa
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {"ok": False}
    _from_statshub(m)          # primero Statshub (no nos frena); Sofascore solo para lo que falte
    have_team = db.q1("SELECT COUNT(*) AS n FROM match_stats WHERE match_id=?", (mid,))["n"] >= 2
    have_pl = db.q1("SELECT COUNT(*) AS n FROM player_stats WHERE match_id=?", (mid,))["n"]
    if have_team and have_pl >= 20:
        return {"ok": True, "team": 1, "players": have_pl, "src": "statshub"}
    if not sofa().ok:
        return {"ok": have_team and have_pl >= 20}
    out = {"ok": True, "team": 0, "players": 0}
    if not have_team:
        d = sofa().statistics(mid)
        st = {"H": {}, "A": {}}
        for per in d.get("statistics", []) if d else []:
            if per.get("period") != "ALL":
                continue
            for g in per.get("groups", []):
                for it in g.get("statisticsItems", []):
                    k = it.get("key")
                    if not k:
                        continue
                    for side, key in (("H", "homeValue"), ("A", "awayValue")):
                        v = _num(it.get(key))
                        if v is not None:
                            st[side][k] = v
        if st["H"]:
            for side in ("H", "A"):
                st[side]["cards"] = (st[side].get("yellowCards") or 0) + (st[side].get("redCards") or 0)
                db.execute("INSERT INTO match_stats(match_id,side,stats,source) VALUES(?,?,?,?) ON CONFLICT(match_id,side) DO NOTHING",
                           (mid, side, json.dumps(st[side]), "sofascore"))
            out["team"] = 1
    have_pl = db.q1("SELECT COUNT(*) AS n FROM player_stats WHERE match_id=?", (mid,))["n"]
    if have_pl < 20:
        lu = sofa().lineups(mid)
        incs = sofa().incidents(mid)
        yel, red = {}, {}
        for i in incs or []:
            if i.get("incidentType") == "card" and not i.get("rescinded"):
                pid = (i.get("player") or {}).get("id")
                if i.get("incidentClass") == "yellow":
                    yel[pid] = yel.get(pid, 0) + 1
                elif i.get("incidentClass") in ("red", "yellowRed"):
                    red[pid] = 1
        rows = []
        for side, key in (("H", "home"), ("A", "away")):
            tid = m["home_id"] if side == "H" else m["away_id"]
            for p in (lu.get(key) or {}).get("players", []) if lu else []:
                pl, s = p.get("player") or {}, p.get("statistics") or {}
                mins = _num(s.get("minutesPlayed")) or 0
                if not pl.get("id") or (mins <= 0 and p.get("substitute")):
                    continue
                shots = sum(int(_num(s.get(k)) or 0) for k in ("onTargetScoringAttempt", "shotOffTarget", "blockedScoringAttempt", "hitWoodwork"))
                rows.append((pl["id"], mid, tid, side, p.get("position") or pl.get("position"), 0 if p.get("substitute") else 1, mins,
                             int(_num(s.get("goals")) or 0), int(_num(s.get("goalAssist")) or 0), _num(s.get("expectedGoals")),
                             _num(s.get("expectedAssists")), shots, int(_num(s.get("onTargetScoringAttempt")) or 0),
                             int(_num(s.get("keyPass")) or 0), int(_num(s.get("wonContest")) or 0), int(_num(s.get("totalTackle")) or 0),
                             int(_num(s.get("interceptionWon")) or 0), int(_num(s.get("duelWon")) or 0), int(_num(s.get("duelLost")) or 0),
                             int(_num(s.get("aerialWon")) or 0), int(_num(s.get("fouls")) or 0), int(_num(s.get("wasFouled")) or 0),
                             yel.get(pl["id"], 0), red.get(pl["id"], 0), int(_num(s.get("totalCross")) or 0), int(_num(s.get("saves")) or 0),
                             _num(s.get("rating"))))
                db.execute("INSERT INTO players(id,name,team_id,position,updated) VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                           (pl["id"], pl.get("name"), tid, pl.get("position"), m["kickoff"]))
        if rows:
            db.executemany("INSERT OR IGNORE INTO player_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            out["players"] = len(rows)
    return out


def _from_statshub(m: dict) -> None:
    """Estadística final desde Statshub: los últimos partidos de cada equipo (con cada jugador) y su rendimiento."""
    from .collectors import players as pl
    from .collectors import statshub as sh
    from .config import COMPETITIONS
    from .live.engine import shub
    mid = m["id"]
    for tid in (m["home_id"], m["away_id"]):
        if not tid or tid <= 0:
            continue
        try:
            if db.q1("SELECT COUNT(*) AS n FROM player_stats WHERE match_id=?", (mid,))["n"] < 20:
                pl.store_last_games([g for g in pl.parse_last_games(shub().team_last_games(tid, 1, 3, ttl=900)) if g["match_id"] == mid])
            if db.q1("SELECT COUNT(*) AS n FROM match_stats WHERE match_id=?", (mid,))["n"] < 2:
                rows = shub().team_performance(tid, [c.unique_id for c in COMPETITIONS.values()], limit=3)
                sh.store_team_stats([r for r in sh.parse_team_performance(rows, tid) if r["match_id"] == mid])
        except Exception as e:
            db.log_job("postmatch", "error", f"statshub {mid}/{tid}: {e}")


# ---------------------------------------------------------------------------- 3. comparar
def compare(mid: int) -> dict:
    from .insights import TEAM_KEY, team_stat
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    pre = db.kv_get(f"pre:{mid}") or pre_snapshot(mid)
    if pre.get("retro") and not pre.get("players"):
        pre = pre_snapshot(mid, force=True)
    st = {r["side"]: json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (mid,))}
    teams = []
    for code in TEAM_CODES:
        e = (pre.get("teams") or {}).get(code)
        key = TEAM_KEY.get(code, (None,))[0]
        if not e or not key or "H" not in st:
            continue
        aH, aA = team_stat(st["H"], key), team_stat(st.get("A", {}), key)
        if aH is None or aA is None:
            continue
        teams.append({"code": code, "word": TEAM_KEY[code][1], "exp": [e["H"], e["A"]], "act": [aH, aA]})
    goals = {"exp": [pre.get("lam", {}).get("H"), pre.get("lam", {}).get("A")], "act": [m["hg"], m["ag"]], "p1x2": pre.get("p1x2")}
    players = []
    for p in pre.get("players", []):
        ps = db.q1("SELECT * FROM player_stats WHERE match_id=? AND player_id=?", (mid, p["id"]))
        if not ps:
            continue
        mins = ps["minutes"] or 0
        act = {"shots": ps["shots"], "sot": ps["sot"], "fouls": ps["fouls"], "was_fouled": ps["was_fouled"], "tackles": ps["tackles"]}
        exp_at_mins = {k: round(r * mins / 90, 2) for k, r in p["rate90"].items()}      # lo esperado con los minutos que jugó de verdad
        players.append({"id": p["id"], "name": p["name"], "side": p["side"], "exp_min": p["exp_min"], "min": mins,
                        "exp": p["exp"], "exp_at_min": exp_at_mins, "act": act})
    # cada mercado, cada línea, cada selección que enseñaba la web: ¿salió?
    markets = []
    for r in pre.get("rows", []):
        try:
            res = settle_row(m, r["m"], r["pe"], r["s"], r["l"])
        except Exception:
            res = None
        if res is None:
            continue
        w, pu = res
        markets.append({**r, "w": w, "pv": pu})
    # cambios y mapas de calor del propio partido: dónde jugó cada uno frente a dónde suele jugar
    subs = _subs(mid)
    heat = _heat_compare(m)
    duels = _duels(m, heat)
    for p in players:
        if p["id"] in duels:
            p["duel"] = duels[p["id"]]
        ps2 = db.q1("SELECT won_contest FROM player_stats WHERE match_id=? AND player_id=?", (mid, p["id"]))
        if ps2 and ps2["won_contest"] is not None:
            prev = db.q1("SELECT AVG(ps.won_contest * 90.0 / ps.minutes) AS r FROM player_stats ps JOIN matches mm ON mm.id=ps.match_id "
                         "WHERE ps.player_id=? AND mm.kickoff<? AND ps.minutes>=30", (p["id"], m["kickoff"]))
            p["dribbles"] = {"act": ps2["won_contest"], "exp": round((prev["r"] or 0) * max(p["min"], 1) / 90, 1) if prev and prev["r"] else None}
        h = heat.get(p["id"])
        if h:
            p["heat"] = h
        sb = subs.get(p["id"])
        if sb:
            p["sub"] = sb
    rep = {"ts": int(time.time()), "teams": teams, "goals": goals, "players": players, "retro": pre.get("retro"),
           "markets": markets, "mk_summary": _mk_summary(markets)}
    db.kv_set(f"post:{mid}", rep)
    _learn(mid, rep)
    return rep


def settle_row(m: dict, market: str, period: str, selection: str, line) -> tuple[float, float] | None:
    """(fracción ganada, fracción devuelta) de cualquier mercado, o None si no se puede saber con lo que hay."""
    if market.startswith("RAW:"):
        from .markets_all import settle_raw
        return settle_raw(m, market, period, selection, line)
    from .core.markets import Selection, settle
    from .learning.loop import _outcome
    o = _outcome({"id": m["id"], "hg": m["hg"], "ag": m["ag"], "hg_ht": m["hg_ht"], "ag_ht": m["ag_ht"]})
    if o is None:
        return None
    return settle(Selection(market, selection, line, period or "FT", 0, 0), o)


def _mk_summary(markets: list[dict]) -> dict:
    """Por tipo de apuesta: cuántas esperábamos que salieran y cuántas salieron (y el error cuadrático medio)."""
    out: dict = {}
    for r in markets:
        if r["pu"] > 0.02 and r["pv"] >= 0.99:
            continue                                   # devuelta: no cuenta
        g = out.setdefault(r.get("g") or "?", {"n": 0, "exp": 0.0, "won": 0.0, "brier": 0.0, "val_n": 0, "val_pnl": 0.0})
        g["n"] += 1
        g["exp"] += r["p"]
        g["won"] += r["w"]
        g["brier"] += (r["p"] - r["w"]) ** 2
        if r.get("ev") is not None and r["ev"] > 0.02 and r.get("o"):
            g["val_n"] += 1                             # las que tenían valor según la web: ¿habrían ganado dinero?
            g["val_pnl"] += r["w"] * (r["o"] - 1) - (1 - r["w"] - r["pv"])
    for g in out.values():
        g["brier"] = round(g["brier"] / g["n"], 4) if g["n"] else None
        g["exp"], g["won"], g["val_pnl"] = round(g["exp"], 2), round(g["won"], 2), round(g["val_pnl"], 2)
    return out


def _subs(mid: int) -> dict:
    """Quién entró y quién salió y cuándo (de los minutos jugados de cada uno)."""
    out = {}
    for r in db.q("SELECT player_id, starter, minutes FROM player_stats WHERE match_id=?", (mid,)):
        mins = r["minutes"] or 0
        if r["starter"] and 0 < mins < 85:
            out[r["player_id"]] = {"out": round(mins)}
        elif not r["starter"] and mins > 0:
            out[r["player_id"]] = {"in": max(1, round(90 - mins))}
    return out


def _duels(m: dict, heat: dict) -> dict:
    """Con quién se cruzó de verdad cada jugador (el rival más cercano en el mapa de calor del partido, dando la vuelta
    al campo porque atacan hacia lados opuestos) frente a con quién esperábamos (por sus posiciones de siempre)."""
    rows = db.q("SELECT h.player_id, h.team_id, h.avg_x, h.avg_y, COALESCE(pl.name, '') AS name FROM heatmaps h "
                "LEFT JOIN players pl ON pl.id=h.player_id JOIN player_stats ps ON ps.match_id=h.match_id AND ps.player_id=h.player_id "
                "WHERE h.match_id=? AND ps.minutes>0", (m["id"],))
    if not rows:
        return {}
    def near(me, others, usual):
        best, bd = None, None
        for o in others:
            if usual and o["player_id"] not in heat:
                continue
            ox, oy = (heat[o["player_id"]]["px"], heat[o["player_id"]]["py"]) if usual else (o["avg_x"], o["avg_y"])
            mx, my = (heat[me["player_id"]]["px"], heat[me["player_id"]]["py"]) if usual else (me["avg_x"], me["avg_y"])
            d = ((mx - (100 - ox)) ** 2 + (my - (100 - oy)) ** 2) ** 0.5
            if bd is None or d < bd:
                best, bd = o, d
        return best, bd
    out = {}
    for me in rows:
        others = [o for o in rows if o["team_id"] != me["team_id"]]
        real, dr = near(me, others, False)
        exp = None
        if me["player_id"] in heat:
            exp, _ = near(me, others, True)
        if real:
            out[me["player_id"]] = {"real": real["name"], "dist": round(dr), "exp": exp["name"] if exp else None}
    return out


def _heat_compare(m: dict) -> dict:
    """Mapa de calor del partido (Statshub) frente a la posición media de sus partidos anteriores."""
    from .collectors.tactics import parse_team_lineup, store_heatmaps
    from .live.engine import shub
    mid = m["id"]
    for tid in (m["home_id"], m["away_id"]):
        if tid and tid > 0 and not db.q1("SELECT 1 AS x FROM heatmaps WHERE match_id=? AND team_id=? LIMIT 1", (mid, tid)):
            try:
                rows = parse_team_lineup(shub().team_lineup(mid, tid, heatmap=True, ttl=1800), mid, tid)
                if rows:
                    store_heatmaps(rows)
            except Exception:
                pass
    # control de calidad: el mapa de calor tiene que ser de los que jugaron ESTE partido (a veces la fuente devuelve
    # el de otro partido: salían Rodri o Gabriel Jesus en un Barça - Aston Villa). Si no cuadra, se tira.
    played = {r["player_id"] for r in db.q("SELECT player_id FROM player_stats WHERE match_id=? AND minutes>0", (mid,))}
    for tid in (m["home_id"], m["away_id"]):
        hs = [r["player_id"] for r in db.q("SELECT player_id FROM heatmaps WHERE match_id=? AND team_id=?", (mid, tid))]
        if hs and played and sum(1 for x in hs if x in played) < 0.6 * len(hs):
            db.execute("DELETE FROM heatmaps WHERE match_id=? AND team_id=?", (mid, tid))
    out = {}
    for h in db.q("SELECT player_id, avg_x, avg_y FROM heatmaps WHERE match_id=?", (mid,)):
        if played and h["player_id"] not in played:
            continue
        prev = db.q1("SELECT AVG(h.avg_x) AS x, AVG(h.avg_y) AS y, COUNT(*) AS n FROM heatmaps h JOIN matches mm ON mm.id=h.match_id "
                     "WHERE h.player_id=? AND mm.kickoff<? AND mm.kickoff>?", (h["player_id"], m["kickoff"], m["kickoff"] - 120 * 86400))
        if prev and prev["n"]:
            out[h["player_id"]] = {"x": round(h["avg_x"], 1), "y": round(h["avg_y"], 1), "px": round(prev["x"], 1),
                                   "py": round(prev["y"], 1), "n": prev["n"]}
    return out


def _learn(mid: int, rep: dict) -> None:
    """Acumula esperado y real por estadística (equipo) y por estadística de jugador (separando minutos y ritmo)."""
    b = db.kv_get("bias") or {"team": {}, "player": {}, "minutes": {"exp": 0.0, "act": 0.0, "n": 0}, "matches": []}
    b.setdefault("pmatches", [])
    if mid in b["matches"] and (mid in b["pmatches"] or not rep["players"]):
        return
    new_team = mid not in b["matches"]
    b["matches"] = (b["matches"] + [mid])[-2000:] if new_team else b["matches"]
    if rep["players"]:
        b["pmatches"] = (b["pmatches"] + [mid])[-2000:]
    m = db.q1("SELECT home_id, away_id FROM matches WHERE id=?", (mid,)) or {}
    ent = b.setdefault("ent", {})
    for t in (rep["teams"] if new_team else []):
        d = b["team"].setdefault(t["code"], {"exp": 0.0, "act": 0.0, "n": 0})
        d["exp"] += sum(t["exp"])
        d["act"] += sum(t["act"])
        d["n"] += 1
        # cada equipo por separado: lo que hace él (no la media de todos)
        # repartido: la mitad de la desviación es «cosa del equipo» y la otra mitad «cosa del rival» (lo que concede):
        # si un equipo saca 3 córners en vez de 6, a lo mejor es que el rival no le deja, no que él haya cambiado
        ids = (m.get("home_id"), m.get("away_id"))
        for i, tid in enumerate(ids):
            if not tid or not t["exp"][i]:
                continue
            r = max(0.3, min(3.0, (t["act"][i] + 0.5) / (t["exp"][i] + 0.5)))
            half = t["exp"][i] * math.sqrt(r)
            e = ent.setdefault(f"tm:{tid}:{t['code']}", {"exp": 0.0, "act": 0.0, "n": 0})
            e["exp"] += t["exp"][i]
            e["act"] += half
            e["n"] += 1
            opp = ids[1 - i]
            if opp:
                o = ent.setdefault(f"op:{opp}:{t['code']}", {"exp": 0.0, "act": 0.0, "n": 0})
                o["exp"] += t["exp"][i]
                o["act"] += half
                o["n"] += 1
    # contexto del partido para no culpar al jugador de lo que fue cosa del partido: cómo le fue a SU equipo en ese
    # dato frente a lo esperado (marcador, rival, clima, quién tenía el balón… todo eso se refleja ahí)
    ctx: dict = {}
    for t in rep["teams"]:
        for i, sd in enumerate(("H", "A")):
            if t["exp"][i]:
                ctx[(sd, t["code"])] = max(0.4, min(2.5, (t["act"][i] + 0.5) / (t["exp"][i] + 0.5)))
    P2T = {"shots": ("SHOTS", 0), "sot": ("SOT", 0), "fouls": ("FOULS", 0), "was_fouled": ("FOULS", 1), "tackles": ("FOULS", 1)}
    for p in rep["players"]:
        if p["min"] <= 0:
            continue
        b["minutes"]["exp"] += p["exp_min"]
        b["minutes"]["act"] += p["min"]
        b["minutes"]["n"] += 1
        for k, e in p["exp_at_min"].items():
            a = p["act"].get(k)
            if a is None:
                continue
            pe = ent.setdefault(f"pl:{p['id']}:{k}", {"exp": 0.0, "act": 0.0, "n": 0})     # cada jugador por separado
            code, other = P2T.get(k, (None, 0))
            sd = p["side"] if not other else ("A" if p["side"] == "H" else "H")
            r = ctx.get((sd, code), 1.0) if code else 1.0
            # lo que esperábamos de él DADO cómo fue el partido para su equipo: si todo el equipo tiró la mitad, a él
            # también se le espera menos; solo lo que se desvía de eso es «suyo»
            pe["exp"] += e * r ** 0.8
            pe["act"] += a
            pe["n"] += 1
            d = b["player"].setdefault(k, {"exp": 0.0, "act": 0.0, "n": 0})
            d["exp"] += e
            d["act"] += a
            d["n"] += 1
    db.kv_set("bias", b)
    if rep.get("markets") and not rep.get("retro"):
        rc = db.kv_get("row_calib") or {"groups": {}, "buckets": {}, "matches": []}
        if mid not in rc["matches"]:
            rc["matches"] = (rc["matches"] + [mid])[-3000:]
            for r in rep["markets"]:
                if r["pu"] > 0.02 and r["pv"] >= 0.99:
                    continue
                g = rc["groups"].setdefault(r.get("g") or "?", {"n": 0, "exp": 0.0, "won": 0.0, "n_mid": 0, "exp_mid": 0.0, "won_mid": 0.0,
                                                                   "val_n": 0, "val_pnl": 0.0})
                g["n"] += 1
                g["exp"] += r["p"]
                g["won"] += r["w"]
                if 0.3 <= r["p"] <= 0.85:                 # la zona donde se apuesta
                    g["n_mid"] += 1
                    g["exp_mid"] += r["p"]
                    g["won_mid"] += r["w"]
                if r.get("ev") is not None and r["ev"] > 0.02 and r.get("o"):
                    g["val_n"] += 1
                    g["val_pnl"] += r["w"] * (r["o"] - 1) - (1 - r["w"] - r["pv"])
                bk = str(min(9, int(r["p"] * 10)))
                bb = rc["buckets"].setdefault(bk, {"n": 0, "exp": 0.0, "won": 0.0})
                bb["n"] += 1
                bb["exp"] += r["p"]
                bb["won"] += r["w"]
            rc["ts"] = int(time.time())
            db.kv_set("row_calib", rc)


def bias_factor(kind: str, key: str) -> float:
    """Corrección aprendida (1 = ninguna). Prudente: con pocos datos casi 1; nunca más de ±15 %."""
    b = _BC.get("v")
    if b is None or time.time() - _BC.get("t", 0) > 900:
        _BC["v"] = b = db.kv_get("bias") or {}
        _BC["t"] = time.time()
    d = ((b.get(kind) or {}).get(key)) or {}
    if not d.get("n") or not d.get("exp"):
        return 1.0
    mean_e = d["exp"] / d["n"]
    k = K_BIAS if kind == "team" else K_BIAS * 8     # en jugadores cada «dato» es un jugador en un partido: hace falta más
    f = (d["act"] + k * mean_e) / (d["exp"] + k * mean_e)
    return round(min(1.15, max(0.85, f)), 4)


_BC: dict = {}


def entity_factor(key: str) -> float:
    """Corrección propia de un jugador («pl:<id>:<dato>») o de un equipo («tm:<id>:<CÓDIGO>»), aprendida comparando lo
    que esperábamos de ÉL con lo que hizo, partido a partido. Con pocos partidos casi no corrige (se encoge hacia 1
    como si llevara 6 partidos haciendo justo lo esperado); con muchos, puede corregir hasta un 25 %."""
    b = _BC.get("v")
    if b is None or time.time() - _BC.get("t", 0) > 900:
        _BC["v"] = b = db.kv_get("bias") or {}
        _BC["t"] = time.time()
    d = (b.get("ent") or {}).get(key) or {}
    if not d.get("n") or not d.get("exp"):
        return 1.0
    mean_e = d["exp"] / d["n"]
    k = 6.0 if key.startswith("pl:") else 8.0
    f = (d["act"] + k * mean_e) / (d["exp"] + k * mean_e)
    return round(min(1.25, max(0.75, f)), 4)


# ---------------------------------------------------------------------------- 4. todo junto, tras cada partido
def finish_recent(hours: float = 30) -> int:
    from .plans import settle_raw_bets, tips_clv, tip_calibration
    from .learning import loop as learn
    now = int(time.time())
    n = 0
    for r in db.q("SELECT id FROM matches WHERE status='finished' AND kickoff BETWEEN ? AND ?", (now - int(hours * 3600), now)):
        pm = db.kv_get(f"post:{r['id']}")
        if pm and (pm.get("players") or now - pm.get("ts", 0) > 6 * 3600):
            continue
        try:
            f = fetch_final(r["id"])
            if not f.get("ok"):
                continue
            compare(r["id"])
            n += 1
        except Exception as e:
            db.log_job("postmatch", "error", f"{r['id']}: {e}")
    if True:
        learn.settle_open_bets()
        settle_raw_bets()
        tips_clv()
        tip_calibration()
        from .collectors.espn import store_timeline as espn_tl   # minuto de goles y rojas (sustituye a Sofascore)
        for r in db.q("SELECT id FROM matches WHERE status='finished' AND kickoff>?", (now - 3 * 86400,)):
            try:
                if espn_tl(r["id"]):
                    db.execute("UPDATE bets SET notes=REPLACE(notes, '\"why\"', '\"why_old\"') WHERE match_id=?", (r["id"],))
            except Exception:
                pass
        from .review import explain_match_bets      # por qué salió o no cada apuesta, con lo que pasó de verdad
        for r in db.q("SELECT DISTINCT b.match_id FROM bets b JOIN matches m ON m.id=b.match_id WHERE b.status NOT IN ('open','void') "
                      "AND m.kickoff>? AND (b.notes NOT LIKE '%\"why\"%' OR b.notes IS NULL)", (now - 7 * 86400,)):
            try:
                explain_match_bets(r["match_id"])
            except Exception as e:
                db.log_job("postmatch", "error", f"por qué {r['match_id']}: {e}")
        db.log_job("postmatch", "ok", f"{n} partidos cerrados: estadística final, apuestas resueltas y comparación")
    return n
