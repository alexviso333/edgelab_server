"""Cruce de factores: todas las parejas de variables × todos los mercados.

Para cada par de factores (p. ej. "descanso" × "duelos", "cara a cara" × "goles
esperados") se parte cada uno en tercios y se mide el yield real a cuota de
bet365 en cada una de las 9 casillas, por mercado y selección. Las casillas con
más ventaja y muestra suficiente se convierten en estrategias candidatas (origen
"cross") que el laboratorio valida fuera de muestra como cualquier otra.
El resultado también alimenta el mapa de calor de la web.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from .dsl import Strategy

FACTORS = {
    "fav_prob": "Prob. del favorito", "exp_goals": "Goles esperados", "elo_diff": "Diferencia Elo",
    "rest_diff": "Diferencia de descanso", "xg_total_form": "Forma xG conjunta", "overperf_gap": "Suerte goleadora",
    "h2h_pts_h": "Cara a cara (puntos local)", "h2h_goals": "Cara a cara (goles)", "lu_gap_h": "Once local vs habitual",
    "lu_gap_a": "Once visitante vs habitual", "duel_edge": "Ventaja en duelos", "rotation_gap": "Diferencia de rotaciones",
    "win_streak_max": "Racha de victorias", "rank_h": "Posición local", "rank_a": "Posición visitante",
}
TARGETS = [("1X2", "H"), ("1X2", "D"), ("1X2", "A"), ("OU", "O"), ("OU", "U"), ("AH", "H"), ("AH", "A")]


def factor_grid(bt: pd.DataFrame, min_cell: int = 60, odds_col: str = "odds_b365_open") -> list[dict]:
    rows = []
    feats = [f for f in FACTORS if f in bt.columns and bt[f].notna().sum() > 300]
    for mk, sel in TARGETS:
        sub = bt[(bt["market"] == mk) & (bt["selection"] == sel) & bt[odds_col].notna() & bt["win"].notna()]
        if len(sub) < min_cell * 9:
            continue
        ret = (sub["win"] * (sub[odds_col] - 1) - sub["lose"]).to_numpy()
        for fa, fb in itertools.combinations(feats, 2):
            a, b = sub[fa], sub[fb]
            ok = a.notna() & b.notna()
            if ok.sum() < min_cell * 9:
                continue
            try:
                qa = pd.qcut(a[ok], 3, labels=False, duplicates="drop")
                qb = pd.qcut(b[ok], 3, labels=False, duplicates="drop")
            except ValueError:
                continue
            ea = np.quantile(a[ok], [0, 1 / 3, 2 / 3, 1])
            eb = np.quantile(b[ok], [0, 1 / 3, 2 / 3, 1])
            r = ret[ok.to_numpy()]
            cells = []
            for i in range(3):
                for j in range(3):
                    m = ((qa == i) & (qb == j)).to_numpy()
                    n = int(m.sum())
                    if n < min_cell:
                        cells.append(None)
                        continue
                    mean = float(r[m].mean())
                    se = float(r[m].std(ddof=1) / np.sqrt(n))
                    cells.append({"i": i, "j": j, "n": n, "yield": mean, "z": mean / se if se > 0 else 0.0,
                                  "a": [float(ea[i]), float(ea[i + 1])], "b": [float(eb[j]), float(eb[j + 1])]})
            valid = [c for c in cells if c]
            if not valid:
                continue
            spread = max(c["yield"] for c in valid) - min(c["yield"] for c in valid)
            rows.append({"market": mk, "selection": sel, "fa": fa, "fb": fb, "cells": cells, "spread": spread,
                         "best": max(valid, key=lambda c: c["z"])})
    rows.sort(key=lambda r: -r["best"]["z"])
    return rows


def grid_to_strategies(grid: list[dict], top: int = 40, min_z: float = 2.0) -> list[Strategy]:
    out = []
    for g in grid[:top * 3]:
        c = g["best"]
        if c["z"] < min_z:
            continue
        filters = []
        for f, (lo, hi), k in ((g["fa"], c["a"], c["i"]), (g["fb"], c["b"], c["j"])):
            if k > 0:
                filters.append({"f": f, "op": ">=", "v": round(lo, 4)})
            if k < 2:
                filters.append({"f": f, "op": "<=", "v": round(hi, 4)})
        mk = {"market": g["market"], "selection": g["selection"]}
        if g["market"] == "OU":
            mk["line"] = 2.5
        if g["market"] == "AH":
            mk["line"] = "main"
        s = Strategy(name=f"Cruce {FACTORS[g['fa']]} × {FACTORS[g['fb']]} → {g['market']} {g['selection']}", phase="pre",
                     family="CROSS", origin="cross", market=mk, context={"filters": filters}, price={"source": "b365_open"},
                     hypothesis=f"Casilla con yield {c['yield']:+.1%} (z={c['z']:.1f}, n={c['n']}) al cruzar dos factores en entrenamiento")
        out.append(s)
        if len(out) >= top:
            break
    return out
