"""Construcción del dataset de partidos con variables PRE-partido sin fuga de
información (cada fila solo usa lo ocurrido antes de su saque inicial).

Variables (todas recalculables a diario):
  Fuerza: Elo, Dixon-Coles goles, Dixon-Coles xG (reajuste semanal), forma xG/goles.
  Contexto: días de descanso, partido europeo previo, eliminado/goleado en Europa,
  partido "sándwich" (siguiente partido grande en ≤4 días), km de viaje, recién
  ascendido, posición/puntos, jornada, árbitro (tarjetas), clima.
  Mercado: probabilidades sin margen de apertura y cierre (Pinnacle, bet365, media),
  λ implícitas, margen de la casa, movimiento de apertura a cierre.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from ..core import db
from ..core.markets import devig
from . import dixon_coles
from .elo import Elo, comp_weight
from .poisson import implied_lambdas, probs_1x2, prob_over
from .secondary import stat_value

EURO = {"UCL", "UEL", "UECL"}
BIG_COMPS = {"UCL", "UEL"}


def load_matches(comps: list[str] | None = None, since: int | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM matches WHERE kickoff IS NOT NULL"
    params: list = []
    if comps:
        sql += f" AND comp IN ({','.join('?' * len(comps))})"
        params += comps
    if since:
        sql += " AND kickoff >= ?"
        params.append(since)
    df = pd.DataFrame(db.q(sql + " ORDER BY kickoff", params))
    if df.empty:
        return df
    stats = db.q("SELECT match_id, side, stats FROM match_stats")
    sd: dict[tuple, dict] = {(s["match_id"], s["side"]): json.loads(s["stats"]) for s in stats}
    for key in ("xg", "corners", "cards", "fouls", "shots", "sot"):
        df[f"{key}_h"] = [stat_value(sd.get((i, "H"), {}), key) for i in df["id"]]
        df[f"{key}_a"] = [stat_value(sd.get((i, "A"), {}), key) for i in df["id"]]
    return df


def market_block(match_ids: list[int]) -> pd.DataFrame:
    """Probabilidades de mercado (sin margen) y cuotas clave por partido (vectorizado)."""
    from ..core.markets import devig_power_vec
    if not match_ids:
        return pd.DataFrame()
    rows = []
    for k in range(0, len(match_ids), 800):
        chunk = match_ids[k:k + 800]
        rows += db.q(f"SELECT match_id, ts, phase, bookmaker, market, selection, line, odds FROM odds "
                     f"WHERE period='FT' AND market IN ('1X2','OU','AH') AND match_id IN ({','.join('?' * len(chunk))})", chunk)
    if not rows:
        return pd.DataFrame({"id": match_ids})
    od = pd.DataFrame(rows).sort_values("ts")
    od["bk"] = od["bookmaker"].map(_bk)
    od["line_k"] = od["line"].fillna(-99.0)
    keys = ["match_id", "bk", "market", "selection", "line_k"]
    parts = []
    for phase, phases, how in (("open", ("open", "snap"), "first"), ("close", ("close", "snap"), "last")):
        sub = od[od["phase"].isin(phases)]
        g = sub.groupby(keys, sort=False)["odds"].agg(how).reset_index()
        g["ph"] = phase
        parts.append(g)
    G = pd.concat(parts, ignore_index=True)
    out = pd.DataFrame({"id": sorted(set(od["match_id"]))}).set_index("id")
    cols: dict[str, pd.Series] = {}
    # 1X2
    x = G[G.market == "1X2"].pivot_table(index=["match_id", "bk", "ph"], columns="selection", values="odds", aggfunc="first")
    x = x.dropna(subset=[c for c in ("H", "D", "A") if c in x.columns]) if {"H", "D", "A"} <= set(x.columns) else x.iloc[0:0]
    if len(x):
        P = devig_power_vec(x[["H", "D", "A"]].to_numpy())
        x = x.assign(pH=P[:, 0], pD=P[:, 1], pA=P[:, 2], orr=(1 / x[["H", "D", "A"]]).sum(axis=1) - 1).reset_index()
        for (bk, ph), g in x.groupby(["bk", "ph"]):
            g = g.set_index("match_id")
            tag = f"{bk}_{ph}"
            for s_ in "HDA":
                cols[f"o_{tag}_{s_}"] = g[s_]
                cols[f"p_{tag}_{s_}"] = g[f"p{s_}"]
            cols[f"or_{tag}"] = g["orr"]
    # O/U 2.5
    y = G[(G.market == "OU") & ((G.line_k - 2.5).abs() < 1e-6)].pivot_table(index=["match_id", "bk", "ph"], columns="selection", values="odds", aggfunc="first")
    if {"O", "U"} <= set(y.columns):
        y = y.dropna(subset=["O", "U"])
        P = devig_power_vec(y[["O", "U"]].to_numpy())
        y = y.assign(pO=P[:, 0]).reset_index()
        for (bk, ph), g in y.groupby(["bk", "ph"]):
            g = g.set_index("match_id")
            tag = f"{bk}_{ph}"
            cols[f"o_{tag}_O25"], cols[f"o_{tag}_U25"], cols[f"p_{tag}_O25"] = g["O"], g["U"], g["pO"]
    # AH: línea principal = la más equilibrada con ambos lados
    a = G[G.market == "AH"]
    if len(a):
        hs = a[a.selection == "H"][["match_id", "bk", "ph", "line_k", "odds"]].rename(columns={"odds": "oh"})
        as_ = a[a.selection == "A"][["match_id", "bk", "ph", "line_k", "odds"]].rename(columns={"odds": "oa"})
        as_ = as_.assign(line_k=-as_["line_k"] + 0.0)
        z = hs.merge(as_, on=["match_id", "bk", "ph", "line_k"])
        if len(z):
            z["bal"] = (z["oh"] - z["oa"]).abs()
            z = z.sort_values("bal").drop_duplicates(["match_id", "bk", "ph"])
            for (bk, ph), g in z.groupby(["bk", "ph"]):
                g = g.set_index("match_id")
                tag = f"{bk}_{ph}"
                cols[f"ah_{tag}_line"], cols[f"o_{tag}_AHH"], cols[f"o_{tag}_AHA"] = g["line_k"], g["oh"], g["oa"]
    if cols:
        out = out.join(pd.DataFrame(cols))
    return out.reset_index().rename(columns={"index": "id"})


def _bk(bk: str) -> str:
    return {"Bet365": "b365", "Pinnacle": "pin", "Max": "max", "Avg": "avg", "Betfair Exchange": "bfx"}.get(bk, bk.lower().replace(" ", ""))


def best_market_probs(row: pd.Series, phase: str) -> tuple[float, float, float] | None:
    for bk in ("pin", "bfx", "avg", "b365", "max"):
        k = f"p_{bk}_{phase}_H"
        if k in row and pd.notna(row[k]):
            return row[k], row[f"p_{bk}_{phase}_D"], row[f"p_{bk}_{phase}_A"]
    return None


def best_over_prob(row: pd.Series, phase: str) -> float | None:
    for bk in ("pin", "avg", "b365", "max"):
        k = f"p_{bk}_{phase}_O25"
        if k in row and pd.notna(row[k]):
            return row[k]
    return None


def build(df: pd.DataFrame | None = None, refit_every_days: int = 7, with_dc: bool = True) -> pd.DataFrame:
    """Dataset completo con variables pre-partido."""
    df = load_matches() if df is None else df
    if df.empty:
        return df
    df = df.sort_values("kickoff").reset_index(drop=True)
    mk = market_block(df["id"].tolist())
    if not mk.empty:
        df = df.merge(mk, on="id", how="left")

    elo = Elo()
    hist = defaultdict(lambda: deque(maxlen=12))   # team -> (ts, gf, ga, xgf, xga, comp, opp)
    last_play = {}
    last_comp = {}
    last_result_euro = {}
    season_teams = defaultdict(set)
    results_seq = defaultdict(list)
    h2h = defaultdict(list)
    table = defaultdict(lambda: {"pts": 0, "gp": 0, "gd": 0})

    # calendario por equipo (para "sándwich": próximo partido)
    sched = defaultdict(list)
    for r in df[["id", "kickoff", "home_id", "away_id", "comp"]].itertuples(index=False):
        sched[r.home_id].append((r.kickoff, r.comp))
        sched[r.away_id].append((r.kickoff, r.comp))
    for t in sched:
        sched[t].sort()

    feats = []
    dc_g = dc_x = None
    next_fit = 0
    matches_for_dc = []
    for i, r in df.iterrows():
        ko = int(r["kickoff"])
        h, a = r["home_id"], r["away_id"]
        if with_dc and ko >= next_fit:
            dc_g = dixon_coles.fit(matches_for_dc, ko, kind="goals", warm=dc_g)
            dc_x = dixon_coles.fit(matches_for_dc, ko, kind="xg", warm=dc_x)
            next_fit = ko + refit_every_days * 86400
        f = {"id": r["id"]}
        f["elo_h"], f["elo_a"] = elo.get(h), elo.get(a)
        f["elo_diff"] = elo.diff(h, a, neutral=False)
        for side, t in (("h", h), ("a", a)):
            hh = list(hist[t])
            f[f"n_{side}"] = len(hh)
            if hh:
                wts = np.array([0.85 ** (len(hh) - 1 - k) for k in range(len(hh))])
                arr = np.array([[x[1], x[2], x[3] if x[3] is not None else np.nan, x[4] if x[4] is not None else np.nan] for x in hh], float)
                for j, nm in enumerate(("gf", "ga", "xgf", "xga")):
                    col = arr[:, j]
                    m = ~np.isnan(col)
                    f[f"{nm}_{side}"] = float((col[m] * wts[m]).sum() / wts[m].sum()) if m.any() else np.nan
                f[f"rest_{side}"] = (ko - last_play.get(t, ko - 7 * 86400)) / 86400
                f[f"prev_euro_{side}"] = 1.0 if last_comp.get(t) in EURO and f[f"rest_{side}"] <= 4.5 else 0.0
                f[f"euro_hangover_{side}"] = last_result_euro.get(t, 0.0) if f[f"prev_euro_{side}"] else 0.0
            else:
                for nm in ("gf", "ga", "xgf", "xga"):
                    f[f"{nm}_{side}"] = np.nan
                f[f"rest_{side}"] = 7.0
                f[f"prev_euro_{side}"] = 0.0
                f[f"euro_hangover_{side}"] = 0.0
            # sándwich: próximo partido de este equipo en ≤4 días y es europeo/copa importante
            nxt = [x for x in sched[t] if x[0] > ko][:1]
            f[f"next_big_{side}"] = 1.0 if nxt and nxt[0][0] - ko <= 4.5 * 86400 and nxt[0][1] in BIG_COMPS else 0.0
            f[f"days_next_{side}"] = (nxt[0][0] - ko) / 86400 if nxt else 7.0
            prev_season_teams = season_teams.get((r["comp"], _prev_season(r["season"])))
            f[f"promoted_{side}"] = 1.0 if (r["comp"] in ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1")
                                             and prev_season_teams and t not in prev_season_teams) else 0.0
            tb = table[(r["comp"], r["season"], t)]
            f[f"ppg_{side}"] = tb["pts"] / tb["gp"] if tb["gp"] else np.nan
            f[f"gp_{side}"] = tb["gp"]
            # posición en la tabla en ese momento
            rivals = [(v["pts"], v["gd"]) for (c_, s_, _t), v in table.items() if c_ == r["comp"] and s_ == r["season"]]
            f[f"rank_{side}"] = 1 + sum(1 for x in rivals if x > (tb["pts"], tb["gd"])) if rivals else np.nan
            f[f"nteams_{side}"] = len(rivals)
            res_seq = results_seq[t]
            streak = 0
            for x in reversed(res_seq):
                if x > 0:
                    streak += 1
                else:
                    break
            f[f"win_streak_{side}"] = streak
            f[f"last_margin_{side}"] = res_seq[-1] if res_seq else 0
        f["rest_diff"] = f["rest_h"] - f["rest_a"]
        # enfrentamientos directos previos (cualquier campo y competición)
        hh = h2h[frozenset((h, a))][-6:]
        f["h2h_n"] = len(hh)
        if hh:
            pts = [3 if (x[1] == h and x[2] > x[3]) or (x[1] != h and x[3] > x[2]) else (1 if x[2] == x[3] else 0) for x in hh]
            f["h2h_pts_h"] = float(np.mean(pts))
            f["h2h_goals"] = float(np.mean([x[2] + x[3] for x in hh]))
            f["h2h_btts"] = float(np.mean([1.0 if x[2] > 0 and x[3] > 0 else 0.0 for x in hh]))
            f["h2h_draws"] = float(np.mean([1.0 if x[2] == x[3] else 0.0 for x in hh]))
            cards = [x[4] for x in hh if x[4] is not None]
            f["h2h_cards"] = float(np.mean(cards)) if cards else np.nan
        else:
            f["h2h_pts_h"] = f["h2h_goals"] = f["h2h_btts"] = f["h2h_draws"] = f["h2h_cards"] = np.nan
        if dc_g is not None:
            lg = dc_g.lambdas(h, a, neutral=False)
            if lg:
                f["dc_lh"], f["dc_la"] = lg
                ph, pd_, pa = probs_1x2(dc_g.matrix(h, a))
                f["dc_pH"], f["dc_pD"], f["dc_pA"] = ph, pd_, pa
                f["dc_pO25"] = prob_over(dc_g.matrix(h, a), 2.5)
        if dc_x is not None:
            lx = dc_x.lambdas(h, a, neutral=False)
            if lx:
                f["xdc_lh"], f["xdc_la"] = lx
                mx = dc_x.matrix(h, a)
                f["xdc_pH"], f["xdc_pD"], f["xdc_pA"] = probs_1x2(mx)
                f["xdc_pO25"] = prob_over(mx, 2.5)
        feats.append(f)

        # ---------------- actualizar estado con el resultado (después de usarlo) --------------
        if r["status"] == "finished" and pd.notna(r["hg"]):
            hg, ag = int(r["hg"]), int(r["ag"])
            elo.update(h, a, hg, ag, r["season"] or "", comp_weight(r["comp"]))
            xh = r.get("xg_h")
            xa = r.get("xg_a")
            hist[h].append((ko, hg, ag, xh if pd.notna(xh) else None, xa if pd.notna(xa) else None, r["comp"], a))
            hist[a].append((ko, ag, hg, xa if pd.notna(xa) else None, xh if pd.notna(xh) else None, r["comp"], h))
            for t, gf, ga in ((h, hg, ag), (a, ag, hg)):
                last_play[t] = ko
                last_comp[t] = r["comp"]
                results_seq[t].append(gf - ga)
                if r["comp"] in EURO:
                    last_result_euro[t] = 1.0 if ga - gf >= 2 else (0.5 if ga > gf else 0.0)
                tb = table[(r["comp"], r["season"], t)]
                tb["gp"] += 1
                tb["gd"] += gf - ga
                tb["pts"] += 3 if gf > ga else (1 if gf == ga else 0)
                season_teams[(r["comp"], r["season"])].add(t)
            ch, ca = r.get("cards_h"), r.get("cards_a")
            h2h[frozenset((h, a))].append((ko, h, hg, ag, (ch + ca) if pd.notna(ch) and pd.notna(ca) else None))
            matches_for_dc.append({"home_id": h, "away_id": a, "kickoff": ko, "hg": hg, "ag": ag,
                                   "xgh": xh if pd.notna(xh) else None, "xga": xa if pd.notna(xa) else None,
                                   "comp": r["comp"]})
    F = pd.DataFrame(feats)
    out = df.merge(F, on="id", how="left")
    # factor alineación + duelos (si hay datos de jugadores)
    try:
        from .players import lineup_features
        LU = lineup_features()
        if not LU.empty:
            out = out.merge(LU, on="id", how="left")
    except Exception as e:  # sin datos de jugadores no se rompe nada
        db.log_job("features", "warn", f"alineaciones: {e}")
    # probabilidades de mercado: mejor casa disponible (Pinnacle > Betfair > media > bet365 > máx.)
    from .poisson import implied_lambdas_vec
    for ph_ in ("open", "close"):
        for s_ in ("H", "D", "A", "O25"):
            col = pd.Series(np.nan, index=out.index)
            for bk in ("max", "b365", "avg", "bfx", "pin"):   # orden inverso: la última pisa
                c = f"p_{bk}_{ph_}_{s_}"
                if c in out.columns:
                    col = out[c].where(out[c].notna(), col)
            out[f"mkt_{ph_}_{s_}"] = col
        lh, la = implied_lambdas_vec(out[f"mkt_{ph_}_H"].to_numpy(), out[f"mkt_{ph_}_D"].to_numpy(),
                                     out[f"mkt_{ph_}_A"].to_numpy(), out[f"mkt_{ph_}_O25"].to_numpy())
        out[f"mkt_{ph_}_lh"], out[f"mkt_{ph_}_la"] = lh, la
    db.save_params("elo", elo.to_dict())
    if dc_g is not None:
        db.save_params("dc_goals", dc_g.to_dict())
    if dc_x is not None:
        db.save_params("dc_xg", dc_x.to_dict())
    return out


def _prev_season(s: str | None) -> str | None:
    if not s or len(s) != 4:
        return None
    a = int(s[:2])
    return f"{(a - 1) % 100:02d}{a:02d}"
