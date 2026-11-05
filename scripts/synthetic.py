"""Mundo sintético para probar el sistema de punta a punta sin red.

Genera ligas, partidos minuto a minuto, xG, rojas, córners, tarjetas y cuotas
de bet365/Pinnacle con ruido. Planta DOS ineficiencias conocidas para comprobar
que el laboratorio las encuentra fuera de muestra:
  0. Las alineaciones cambian la fuerza real (rotaciones/bajas) pero la apertura
     no las conoce y el cierre solo las recoge a medias (tipo P13 / I-P21).
  1. bet365 paga el empate un ~9 % por encima de lo justo en apertura cuando
     se esperan pocos goles (tipo P15).
  2. En directo, el equipo que pierde por 1 aprieta más de lo que asume el
     modelo de mercado (trail1 real = 1.35 vs 1.12) → el que pierde está
     infravalorado (tipo D06).
El control negativo (apostar siempre al visitante) NO debe pasar.

Uso: python -m scripts.synthetic --seasons 5 --db data/synth.sqlite
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(seasons: int = 5, db_path: str = "data/synth.sqlite", teams_per: int = 20, seed: int = 7, leagues=None) -> None:
    os.environ["EDGELAB_DB"] = db_path
    if os.path.exists(db_path):
        os.remove(db_path)
    from edgelab.config import settings
    settings.db_path = __import__("pathlib").Path(db_path)
    from edgelab.core import db
    from edgelab.core.markets import apply_margin, devig
    from edgelab.models.inplay import InplayParams, time_profile, T_END, _state_mult
    from edgelab.models.poisson import implied_lambdas, score_matrix, probs_1x2, prob_over

    rng = np.random.default_rng(seed)
    leagues = leagues or ["EPL", "LALIGA", "SERIEA"]
    true_p = InplayParams(trail1=1.35, trail2=1.15, lead1=0.88, lead2=0.85)
    w = time_profile(true_p)
    mid = 1
    tid = 1
    start_year = 2026 - seasons
    teams = {}
    POS = ["GK", "LB", "LCB", "RCB", "RB", "LCM", "CDM", "RCM", "LW", "ST", "RW"]
    BENCH = ["G", "D", "D", "M", "M", "F", "F"]
    CITIES = ["Norte", "Sur", "Este", "Oeste", "Puerto", "Valle", "Sierra", "Río", "Lago", "Monte", "Costa", "Llano",
              "Bahía", "Cumbre", "Ribera", "Campo", "Villa", "Torre", "Puente", "Olivar"]
    PAL = ["#c8102e", "#1d428a", "#f2c230", "#0b6e4f", "#6c2d91", "#e35205", "#00a3e0", "#222222", "#8a1538", "#2e7d32"]
    first = ["Álvaro", "Hugo", "Leo", "Marco", "Iker", "Teo", "Nico", "Dani", "Pablo", "Raúl", "Óscar", "Unai", "Gael", "Bruno", "Saúl", "Jon"]
    last = ["Ruiz", "Moreno", "Vidal", "Serrano", "Navas", "Ibarra", "Lozano", "Herrera", "Molina", "Rey", "Soler", "Pardo", "Castro", "Gil", "Mena", "Ortiz"]
    pid = 100000
    squads = {}
    for lg in leagues:
        teams[lg] = []
        for k in range(teams_per):
            att, de = rng.normal(0, 0.28), rng.normal(0, 0.24)
            teams[lg].append([tid, att, de])
            name = f"{CITIES[k % len(CITIES)]} {lg[:2].title()}"
            db.execute("INSERT INTO teams(id,name,short,country,color1,color2) VALUES(?,?,?,?,?,?)",
                       (tid, name, name.split()[0], lg.lower(), PAL[k % len(PAL)], "#ffffff"))
            sq = []
            for j, pos in enumerate(POS + BENCH):
                q = float(np.clip(rng.normal(1.0 if j < 11 else 0.8, 0.18), 0.4, 1.6))
                nm = f"{first[rng.integers(len(first))][0]}. {last[rng.integers(len(last))]}"
                sq.append({"id": pid, "pos": pos, "q": q, "name": nm, "foul": float(rng.uniform(0.6, 2.2)), "drib": float(rng.uniform(0.2, 2.5))})
                db.execute("INSERT INTO players(id,name,team_id,position) VALUES(?,?,?,?)", (pid, nm, tid, pos))
                pid += 1
            squads[tid] = sq
            tid += 1
    team_name = {t[0]: r["name"] for t in [x for v in teams.values() for x in v] for r in [db.q1("SELECT name FROM teams WHERE id=?", (t[0],))]}
    ps_rows, lu_rows = [], []
    odds_rows, tl_rows, stat_rows = [], [], []
    for si in range(seasons + 1):
        year = start_year + si
        season = f"{year % 100:02d}{(year + 1) % 100:02d}"
        future = si == seasons
        for lg in leagues:
            T = teams[lg]
            for t in T:   # deriva entre temporadas
                t[1] = 0.8 * t[1] + rng.normal(0, 0.12)
                t[2] = 0.8 * t[2] + rng.normal(0, 0.1)
            fixtures = [(a, b) for a in range(len(T)) for b in range(len(T)) if a != b]
            rng.shuffle(fixtures)
            n_fix = len(fixtures) if not future else 30
            for k, (a, b) in enumerate(fixtures[:n_fix]):
                ko = int(datetime(year, 8, 15, 15, tzinfo=timezone.utc).timestamp()) + (k // 10) * 7 * 86400 + (k % 10) * 7200
                if future:
                    ko = int(datetime.now(timezone.utc).timestamp()) + 86400 * (1 + k // 10) + (k % 10) * 7200
                h, aw = T[a], T[b]
                # once: cada titular falta con prob. 0.12 (rotación/lesión) → entra un suplente
                xi = {}
                for tt in (h[0], aw[0]):
                    sq = squads[tt]
                    starters = []
                    for j in range(11):
                        if rng.random() < 0.12:
                            bench = [p for p in sq[11:] if p["pos"][0] == {"GK": "G"}.get(sq[j]["pos"], sq[j]["pos"])[0] or True]
                            sub = sq[11 + int(rng.integers(len(sq) - 11))]
                            starters.append({**sub, "pos": sq[j]["pos"]})
                        else:
                            starters.append(sq[j])
                    xi[tt] = starters
                str_h = sum(p["q"] for p in xi[h[0]]) / 11
                str_a = sum(p["q"] for p in xi[aw[0]]) / 11
                lh0 = math.exp(0.12 + 0.25 + h[1] + aw[2])
                la0 = math.exp(0.12 + aw[1] + h[2])
                lh = lh0 * (str_h / str_a) ** 0.9
                la = la0 * (str_a / str_h) ** 0.9
                # simular minuto a minuto con dinámica "real"
                hg = ag = rh = ra = 0
                events = []
                xh = xa = 0.0
                for m in range(T_END):
                    d = hg - ag
                    muh = lh * w[m] * _state_mult(d, true_p) * (0.68 ** rh) * (1.28 ** ra)
                    mua = la * w[m] * _state_mult(-d, true_p) * (0.68 ** ra) * (1.28 ** rh)
                    # tiros con xG (señal ruidosa de la intensidad)
                    for side, mu in (("H", muh), ("A", mua)):
                        nsh = rng.poisson(mu * 9)
                        for _ in range(nsh):
                            xg = float(np.clip(rng.gamma(1.2, 0.09), 0.01, 0.9))
                            events.append({"minute": m + 1, "kind": "shot", "side": side, "xg": xg})
                    if rng.random() < 1 - math.exp(-muh):
                        hg += 1
                        events.append({"minute": m + 1, "kind": "goal", "side": "H", "xg": 0.3})
                    if rng.random() < 1 - math.exp(-mua):
                        ag += 1
                        events.append({"minute": m + 1, "kind": "goal", "side": "A", "xg": 0.3})
                    if rng.random() < 0.0011:
                        side = "H" if rng.random() < 0.5 else "A"
                        events.append({"minute": m + 1, "kind": "red", "side": side})
                        if side == "H":
                            rh += 1
                        else:
                            ra += 1
                ht_h = sum(1 for e in events if e["kind"] == "goal" and e["side"] == "H" and e["minute"] <= 45)
                ht_a = sum(1 for e in events if e["kind"] == "goal" and e["side"] == "A" and e["minute"] <= 45)
                status = "notstarted" if future else "finished"
                gh_ = [e for e in events if e["kind"] == "goal"]
                for side, tt, lam_, gl in (("H", h[0], lh, hg), ("A", aw[0], la, ag)):
                    goals_left = gl
                    for j, p in enumerate(xi[tt]):
                        att = p["pos"] in ("LW", "ST", "RW")
                        mid_ = p["pos"] in ("LCM", "CDM", "RCM")
                        xgp = lam_ * (0.25 if att else 0.06 if mid_ else 0.015) * p["q"]
                        g_ = 0
                        if goals_left and att and rng.random() < 0.45:
                            g_ = 1; goals_left -= 1
                        fouls = int(rng.poisson(p["foul"] * (1.2 if p["pos"] in ("LB", "RB", "CDM") else 0.8)))
                        lu_rows.append((mid, side, p["id"], p["name"], p["pos"], j + 1, 1, "predicted" if future else "played", "4-3-3", 0))
                        if not future:
                            ps_rows.append((p["id"], mid, tt, side, p["pos"], 1, 90.0, g_, 0, round(xgp * rng.uniform(0.5, 1.5), 3),
                                            round(xgp * 0.6 * rng.uniform(0.3, 1.5), 3), int(rng.poisson(xgp * 8)), int(rng.poisson(xgp * 3)),
                                            int(rng.poisson(1.2 * p["q"])), int(rng.poisson(p["drib"] if att else 0.4)),
                                            int(rng.poisson(2.2 if not att else 0.6)), int(rng.poisson(1.1 if not att else 0.3)),
                                            int(rng.poisson(5 * p["q"])), int(rng.poisson(4)), int(rng.poisson(1.5)), fouls,
                                            int(rng.poisson(1.0 + (p["drib"] if att else 0.3))), int(rng.random() < 0.06 * fouls + 0.03), 0,
                                            int(rng.poisson(2 if p["pos"] in ("LB", "RB", "LW", "RW") else 0.3)), 0,
                                            round(float(np.clip(6.7 + (p["q"] - 1) * 2 + rng.normal(0, 0.5), 5, 9.5)), 2)))
                db.execute("INSERT INTO matches(id,comp,season,kickoff,home_id,away_id,home_name,away_name,status,hg,ag,hg_ht,ag_ht,source,formation_h,formation_a) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'4-3-3','4-3-3')",
                           (mid, lg, season, ko, h[0], aw[0], team_name[h[0]], team_name[aw[0]], status,
                            None if future else hg, None if future else ag, None if future else ht_h, None if future else ht_a, "synthetic"))
                # cuotas
                M = score_matrix(lh, la, -0.05)
                pH, pD, pA = probs_1x2(M)
                pO = prob_over(M, 2.5)
                M0 = score_matrix(lh0, la0, -0.05)
                qH, qD, qA = probs_1x2(M0)   # lo que "sabe" el mercado sin alineaciones
                qO = prob_over(M0, 2.5)
                def noisy(ps, sd):
                    z = np.log(ps) + rng.normal(0, sd, len(ps))
                    e = np.exp(z)
                    return list(e / e.sum())
                book = {}
                mix = lambda x, y, w: [w * u + (1 - w) * v for u, v in zip(x, y)]
                book[("Pinnacle", "close")] = (noisy(mix([pH, pD, pA], [qH, qD, qA], 0.6), 0.03), noisy([pO, 1 - pO], 0.03), 0.025)
                book[("Pinnacle", "open")] = (noisy([qH, qD, qA], 0.06), noisy([qO, 1 - qO], 0.06), 0.03)
                b365o = noisy([qH, qD, qA], 0.07)
                if lh + la < 2.35:   # ineficiencia plantada 1
                    b365o = [b365o[0], b365o[1] / 1.09, b365o[2]]
                    s_ = sum(b365o)
                    b365o = [x / s_ for x in b365o]
                book[("Bet365", "open")] = (b365o, noisy([pO, 1 - pO], 0.07), 0.055)
                book[("Bet365", "close")] = (noisy(mix([pH, pD, pA], [qH, qD, qA], 0.5), 0.04), noisy([pO, 1 - pO], 0.04), 0.05)
                for (bk, ph), (p3, p2, mg) in book.items():
                    o3 = apply_margin(p3, mg)
                    if bk == "Bet365" and ph == "open" and lh + la < 2.35:
                        # el empate se paga ~9 % por encima del justo
                        o3[1] = (1 / qD) * 1.09
                    o2 = apply_margin(p2, mg)
                    tsx = ko - (3 * 86400 if ph == "open" else 60)
                    for sel, o in zip("HDA", o3):
                        odds_rows.append((mid, tsx, ph, bk, "1X2", sel, None, "FT", round(o, 2), "synthetic"))
                    for sel, o in zip("OU", o2):
                        odds_rows.append((mid, tsx, ph, bk, "OU", sel, 2.5, "FT", round(o, 2), "synthetic"))
                if not future:
                    for e in events:
                        tl_rows.append((mid, e["minute"] - 0.5 + rng.random() * 0.4, e["kind"], e["side"], e.get("xg"), None, "synthetic"))
                    xgh = sum(e["xg"] for e in events if e["kind"] == "shot" and e["side"] == "H")
                    xga = sum(e["xg"] for e in events if e["kind"] == "shot" and e["side"] == "A")
                    for side, xg, lam in (("H", xgh, lh), ("A", xga, la)):
                        stat_rows.append((mid, side, json.dumps({"expectedGoals": round(xg, 2), "cornerKicks": int(rng.poisson(4 + lam)),
                                                                  "yellowCards": int(rng.poisson(1.9)), "fouls": int(rng.poisson(11))}), "synthetic"))
                mid += 1
    db.executemany("INSERT INTO odds(match_id,ts,phase,bookmaker,market,selection,line,period,odds,source) VALUES(?,?,?,?,?,?,?,?,?,?)", odds_rows)
    db.executemany("INSERT INTO timeline(match_id,minute,kind,side,xg,player,source) VALUES(?,?,?,?,?,?,?)", tl_rows)
    db.executemany("INSERT INTO match_stats(match_id,side,stats,source) VALUES(?,?,?,?)", stat_rows)
    db.executemany("INSERT OR REPLACE INTO player_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ps_rows)
    db.executemany("INSERT OR REPLACE INTO lineups(match_id,side,player_id,name,position,jersey,starter,source,formation,ts) VALUES(?,?,?,?,?,?,?,?,?,?)", lu_rows)
    print(f"sintético: {mid - 1} partidos, {len(odds_rows)} cuotas, {len(tl_rows)} eventos en {db_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=5)
    ap.add_argument("--db", default="data/synth.sqlite")
    ap.add_argument("--teams", type=int, default=20)
    a = ap.parse_args()
    main(a.seasons, a.db, a.teams)
