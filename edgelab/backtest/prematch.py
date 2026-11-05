"""Backtest pre-partido con cuotas REALES (football-data + grabador propio).

Para que el buscador pueda probar miles de variantes en segundos, todo se
precalcula en una "tabla de apuestas candidatas": una fila por partido y
selección (1X2 H/D/A, O/U 2.5, AH línea principal) con sus cuotas (bet365
apertura/cierre, Pinnacle, máximo), probabilidades (ensemble, mercado, DC),
liquidación, roles (favorito, descansado, ascendido...) y todas las variables
del partido. Una estrategia pre-partido es entonces una máscara + staking.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..core.markets import Outcome, Selection, settle
from ..strategies.dsl import FEATURE_OPS, Strategy

POPULAR = {"Real Madrid", "Barcelona", "Manchester United", "Manchester City", "Liverpool", "Arsenal", "Chelsea",
           "FC Bayern München", "Juventus", "Paris Saint-Germain", "Milan", "Inter", "Borussia Dortmund", "Tottenham Hotspur"}


def derive(ds: pd.DataFrame) -> pd.DataFrame:
    """Variables de partido que usan las estrategias del catálogo."""
    d = ds.copy()
    for c in ("mkt_open_H", "mkt_open_D", "mkt_open_A", "mkt_close_H", "mkt_close_A", "mkt_open_lh", "mkt_open_la",
              "xgf_h", "xgf_a", "xga_h", "xga_a", "gf_h", "gf_a", "rest_h", "rest_a", "ppg_h", "ppg_a",
              "rank_h", "rank_a", "nteams_h", "win_streak_h", "win_streak_a", "last_margin_h", "last_margin_a",
              "p_pin_open_H", "p_pin_close_H", "p_pin_open_A", "p_pin_close_A"):
        if c not in d.columns:
            d[c] = np.nan
    fav_home = d["mkt_open_H"] >= d["mkt_open_A"]
    d["fav_side_code"] = np.where(fav_home, "H", "A")
    d["fav_prob"] = np.where(fav_home, d["mkt_open_H"], d["mkt_open_A"])
    d["exp_goals"] = d["mkt_open_lh"] + d["mkt_open_la"]
    d["pin_move"] = (d["p_pin_close_H"] - d["p_pin_open_H"]).abs()
    d["pin_move_abs"] = d["pin_move"]
    d["rest_gap_abs"] = (d["rest_h"] - d["rest_a"]).abs()
    d["tired_prev_euro"] = np.where(d["rest_h"] < d["rest_a"], d.get("prev_euro_h", 0), d.get("prev_euro_a", 0))
    d["hangover_any"] = ((d.get("euro_hangover_h", 0).fillna(0) > 0) ^ (d.get("euro_hangover_a", 0).fillna(0) > 0)).astype(int)
    d["any_promoted"] = ((d.get("promoted_h", 0).fillna(0) + d.get("promoted_a", 0).fillna(0)) == 1).astype(int)
    d["fav_next_big"] = np.where(fav_home, d.get("next_big_h", 0), d.get("next_big_a", 0))
    d["fav_prev_euro"] = np.where(fav_home, d.get("prev_euro_h", 0), d.get("prev_euro_a", 0))
    d["overperf_h"] = d["gf_h"] - d["xgf_h"]
    d["overperf_a"] = d["gf_a"] - d["xgf_a"]
    d["overperf_gap"] = (d["overperf_h"] - d["overperf_a"]).abs()
    d["xg_diff_form_h"] = d["xgf_h"] - d["xga_h"]
    d["xg_diff_form_a"] = d["xgf_a"] - d["xga_a"]
    d["xg_table_mismatch"] = np.where(
        (d["ppg_h"] - d["ppg_a"]) * (d["xg_diff_form_h"] - d["xg_diff_form_a"]) < 0,
        (d["xg_diff_form_h"] - d["xg_diff_form_a"]).abs(), 0.0)
    d["xg_total_form"] = (d["xgf_h"] + d["xga_a"]) / 2 + (d["xgf_a"] + d["xga_h"]) / 2
    n = d["nteams_h"].fillna(20)
    def motivated(rank, gp):
        return ((rank <= 4) | (rank >= n - 3)).astype(float)
    d["motivated_h"] = motivated(d["rank_h"], d.get("gp_h", 0))
    d["motivated_a"] = motivated(d["rank_a"], d.get("gp_a", 0))
    d["motivation_gap"] = (d["motivated_h"] - d["motivated_a"]).abs()
    d["relegation_fight"] = (((d["rank_h"] >= n - 3) & (d["rank_a"].between(7, n - 6))) |
                             ((d["rank_a"] >= n - 3) & (d["rank_h"].between(7, n - 6)))).astype(int)
    d["win_streak_max"] = d[["win_streak_h", "win_streak_a"]].max(axis=1)
    d["fav_last_heavy_loss"] = np.where(fav_home, d["last_margin_h"] <= -3, d["last_margin_a"] <= -3).astype(int)
    d["duel_edge_abs"] = d["duel_edge"].abs() if "duel_edge" in d.columns else np.nan
    d["fav_popular"] = np.where(fav_home, d["home_name"].isin(POPULAR), d["away_name"].isin(POPULAR)).astype(int)
    extra = d.get("extra")
    d["ref_strict"] = 0
    d["derby"] = d.get("derby", 0)
    d["bad_weather"] = d.get("bad_weather", 0)
    for c in ("lu_gap_h", "lu_gap_a", "rotation_h", "rotation_a", "duel_edge", "h2h_n", "h2h_pts_h", "h2h_goals", "h2h_draws", "h2h_cards"):
        if c not in d.columns:
            d[c] = np.nan
    for c in ("new_coach_any", "key_absences_gap", "ah_1x2_gap", "travel_km_tired", "second_leg_deficit", "rotation_gap"):
        if c not in d.columns:
            d[c] = np.nan
    return d


_ROLE_FUNCS = {
    "fav": lambda r, s: s == r["fav_side_code"],
    "dog": lambda r, s: s in ("H", "A") and s != r["fav_side_code"],
    "rested": lambda r, s: (s == "H" and r["rest_h"] > r["rest_a"]) or (s == "A" and r["rest_a"] > r["rest_h"]),
    "opp_of_overperformer": lambda r, s: (s == "H" and r["overperf_a"] > r["overperf_h"]) or (s == "A" and r["overperf_h"] > r["overperf_a"]),
    "motivated": lambda r, s: (s == "H" and r["motivated_h"] > r["motivated_a"]) or (s == "A" and r["motivated_a"] > r["motivated_h"]),
    "promoted": lambda r, s: (s == "H" and r.get("promoted_h", 0) == 1) or (s == "A" and r.get("promoted_a", 0) == 1),
    "opp_of_hungover": lambda r, s: (s == "H" and (r.get("euro_hangover_a") or 0) > 0) or (s == "A" and (r.get("euro_hangover_h") or 0) > 0),
    "xg_better": lambda r, s: (s == "H" and r["xg_diff_form_h"] > r["xg_diff_form_a"]) or (s == "A" and r["xg_diff_form_a"] > r["xg_diff_form_h"]),
    "opp_of_streak": lambda r, s: (s == "H" and r["win_streak_a"] > r["win_streak_h"]) or (s == "A" and r["win_streak_h"] > r["win_streak_a"]),
    "relegation_side": lambda r, s: (s == "H" and r["rank_h"] > r["rank_a"]) or (s == "A" and r["rank_a"] > r["rank_h"]),
    "weakened_opp": lambda r, s: (s == "H" and (r["lu_gap_a"] or 0) < (r["lu_gap_h"] or 0)) or (s == "A" and (r["lu_gap_h"] or 0) < (r["lu_gap_a"] or 0)),
    "opp_of_rotated": lambda r, s: (s == "H" and (r["rotation_a"] or 0) > (r["rotation_h"] or 0)) or (s == "A" and (r["rotation_h"] or 0) > (r["rotation_a"] or 0)),
    "duel_favoured": lambda r, s: (s == "H" and (r["duel_edge"] or 0) > 0) or (s == "A" and (r["duel_edge"] or 0) < 0),
    "h2h_dominant": lambda r, s: (s == "H" and (r["h2h_pts_h"] or 0) >= 2) or (s == "A" and r["h2h_n"] and (r["h2h_pts_h"] or 3) <= 0.7),
    "steamed": lambda r, s: s in ("H", "A") and ((r.get(f"p_pin_close_{s}") or 0) - (r.get(f"p_pin_open_{s}") or 0)) > 0,
}


def bet_table(ds: pd.DataFrame, include_future: bool = False) -> pd.DataFrame:
    """Una fila por (partido, selección candidata)."""
    d = derive(ds)
    done = d if include_future else d[d["hg"].notna()]
    rows = []
    for _, r in done.iterrows():
        o = Outcome(int(r["hg"]), int(r["ag"])) if pd.notna(r["hg"]) and r.get("status") == "finished" else None
        cands = []
        for s in ("H", "D", "A"):
            cands.append(("1X2", s, None, r.get(f"o_b365_open_{s}"), r.get(f"o_b365_close_{s}"), r.get(f"o_max_open_{s}"),
                          r.get(f"o_pin_close_{s}"), r.get(f"p_pin_open_{s}"), r.get(f"p_pin_close_{s}"),
                          r.get(f"ens_open_p{s}"), r.get(f"mkt_open_{s}"), r.get(f"dc_p{s}"), r.get(f"xdc_p{s}")))
        po = r.get("ens_open_pO25")
        for s, k in (("O", "O25"), ("U", "U25")):
            pin_o = r.get("p_pin_open_O25")
            pin_c = r.get("p_pin_close_O25")
            cands.append(("OU", s, 2.5, r.get(f"o_b365_open_{k}"), r.get(f"o_b365_close_{k}"), r.get(f"o_max_open_{k}"),
                          r.get(f"o_pin_close_{k}"),
                          pin_o if s == "O" or pin_o is None or pd.isna(pin_o) else 1 - pin_o,
                          pin_c if s == "O" or pin_c is None or pd.isna(pin_c) else 1 - pin_c,
                          po if s == "O" or po is None or pd.isna(po) else 1 - po,
                          r.get("mkt_open_O25") if s == "O" else (1 - r.get("mkt_open_O25") if pd.notna(r.get("mkt_open_O25")) else np.nan),
                          r.get("dc_pO25") if s == "O" else (1 - r["dc_pO25"] if pd.notna(r.get("dc_pO25")) else np.nan), np.nan))
        line = r.get("ah_b365_open_line")
        if line is not None and pd.notna(line):
            for s, k, L in (("H", "AHH", line), ("A", "AHA", -line)):
                cands.append(("AH", s, float(L) + 0.0, r.get(f"o_b365_open_{k}"), r.get(f"o_b365_close_{k}") if r.get("ah_b365_close_line") == line else np.nan,
                              r.get(f"o_max_open_{k}") if r.get("ah_max_open_line") == line else np.nan,
                              r.get(f"o_pin_close_{k}") if r.get("ah_pin_close_line") == line else np.nan,
                              np.nan, np.nan, np.nan, np.nan, np.nan, np.nan))
        for mk, s, L, ob_o, ob_c, omax, opin_c, ppin_o, ppin_c, pens, pmkt, pdc, pxdc in cands:
            if ob_o is None or pd.isna(ob_o):
                continue
            sel = Selection(mk, s, L if mk != "1X2" else None)
            w, p = settle(sel, o) if o is not None else (np.nan, np.nan)
            # probabilidades para AH a partir de λ del ensemble (si hay)
            if mk == "AH" and pd.notna(r.get("ens_open_lh")):
                from ..models.poisson import score_matrix
                from ..core.markets import selection_probs
                M = score_matrix(r["ens_open_lh"], r["ens_open_la"], -0.05)
                ew, ep, el = selection_probs(sel, M)
                pens = ew / max(ew + el, 1e-9)  # prob. "efectiva" sin push
            row = {"id": r["id"], "kickoff": r["kickoff"], "season": r["season"], "comp": r["comp"],
                   "market": mk, "selection": s, "line": L, "odds_b365_open": ob_o, "odds_b365_close": ob_c,
                   "odds_max_open": omax, "odds_pin_close": opin_c, "p_pin_open": ppin_o, "p_pin_close": ppin_c,
                   "p_ens": pens, "p_mkt": pmkt, "p_dc": pdc, "p_xdc": pxdc, "win": w, "push": p, "lose": 1 - w - p,
                   "is_home": s == "H", "fav_side_code": r["fav_side_code"]}
            for role, fn in _ROLE_FUNCS.items():
                try:
                    row[f"role_{role}"] = bool(fn(r, s))
                except Exception:
                    row[f"role_{role}"] = False
            rows.append(row)
    bt = pd.DataFrame(rows)
    if bt.empty:
        return bt
    keep = [c for c in d.columns if c not in bt.columns and c not in ("extra",)]
    bt = bt.merge(d[["id"] + keep], on="id", how="left")
    bt["clv"] = np.where(bt["p_pin_close"].notna(), bt["odds_b365_open"] * bt["p_pin_close"] - 1, np.nan)
    return bt


def _mask(bt: pd.DataFrame, st: Strategy) -> pd.Series:
    m = pd.Series(True, index=bt.index)
    mk = st.market
    m &= bt["market"] == mk.get("market")
    sel = mk.get("selection", "any")
    if sel in ("H", "D", "A", "O", "U"):
        m &= bt["selection"] == sel
    elif sel != "any":
        col = f"role_{sel}"
        if col not in bt.columns:
            return pd.Series(False, index=bt.index)
        m &= bt[col]
    if mk.get("market") == "OU" and mk.get("line") not in (None, "main"):
        m &= (bt["line"] - float(mk["line"])).abs() < 1e-6
    c = st.context
    if c.get("comps"):
        m &= bt["comp"].isin(c["comps"])
    if c.get("fav_prob"):
        lo, hi = c["fav_prob"]
        m &= bt["fav_prob"].between(lo, hi)
    if c.get("fav_side"):
        m &= bt["fav_side_code"] == ("H" if c["fav_side"] == "home" else "A")
    for f in c.get("filters", []):
        if f["f"] not in bt.columns:
            return pd.Series(False, index=bt.index)
        col = bt[f["f"]]
        m &= FEATURE_OPS[f["op"]](col, f["v"]).fillna(False)
    return m


def run(bt: pd.DataFrame, st: Strategy, bankroll: float = 1000.0) -> pd.DataFrame:
    """Aplica una estrategia pre-partido. Devuelve las apuestas con stake y P&L."""
    if bt.empty:
        return bt
    m = _mask(bt, st)
    src = st.price.get("source", "b365_open")
    oc = {"b365_open": "odds_b365_open", "b365_close": "odds_b365_close", "max_open": "odds_max_open",
          "pin_close": "odds_pin_close"}[src]
    sub = bt[m & bt[oc].notna()].copy()
    if sub.empty:
        return sub
    sub["odds"] = sub[oc]
    if st.price.get("min_odds"):
        sub = sub[sub["odds"] >= st.price["min_odds"]]
    if st.price.get("max_odds"):
        sub = sub[sub["odds"] <= st.price["max_odds"]]
    mf = st.model_filter
    probcol = {"ens": "p_ens", "pin_open": "p_pin_open", "mkt": "p_mkt", "dc": "p_dc", "xdc": "p_xdc"}.get(mf.get("prob", "ens"), "p_ens")
    sub["p_used"] = sub[probcol] if probcol in sub.columns else np.nan
    if mf.get("min_edge") is not None:
        edge = sub["p_used"] * sub["odds"] - 1
        sub = sub[edge >= mf["min_edge"]]
        if mf.get("max_edge") is not None:
            sub = sub[(sub["p_used"] * sub["odds"] - 1) <= mf["max_edge"]]
        if mf.get("consensus"):
            agree = (sub[["p_dc", "p_xdc", "p_mkt"]].mul(sub["odds"], axis=0) - 1 > 0).all(axis=1)
            sub = sub[agree]
    sub["edge"] = sub["p_used"] * sub["odds"] - 1
    # una apuesta por partido y mercado: la de mayor ventaja (evita apostar a H y A a la vez)
    if not sub.empty:
        order = sub["edge"].fillna(-9).rank(method="first", ascending=False)
        sub = sub.assign(_o=order).sort_values("_o").drop_duplicates(["id", "market"]).drop(columns="_o")
    sk = st.staking
    if sk.get("type") == "kelly":
        b = sub["odds"] - 1
        pw = sub["p_used"].fillna(1 / sub["odds"])
        f = ((pw * b - (1 - pw)) / b).clip(lower=0) * sk.get("fraction", 0.25)
        sub["stake"] = (f.clip(upper=sk.get("cap", 0.03)) * bankroll).round(2)
        sub = sub[sub["stake"] > 0]
    else:
        sub["stake"] = sk.get("units", 1.0)
    sub["pnl"] = sub["stake"] * (sub["win"] * (sub["odds"] - 1) - sub["lose"])
    sub["strategy_id"] = st.id
    return sub.sort_values("kickoff")
