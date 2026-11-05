"""Calculadora de coberturas.

Dos niveles:
1. Fórmulas cerradas de siempre (lay, AH 0.0 a cero, igualar, dutching,
   green-up, cuartos, Kelly), comprobables a mano.
2. Un solver general: dadas tus apuestas abiertas y las cuotas actuales de
   cualquier mercado, calcula cuánto meter en cada cobertura para un objetivo
   (igualar ramas, dejar una rama a cero, maximizar el beneficio garantizado,
   Kelly sobre la banca, o media-varianza). Trabaja sobre la rejilla de
   marcadores finales que da el modelo en vivo, así cada mercado (AH de cuarto,
   O/U, BTTS, exacto...) se liquida exactamente igual que en la casa.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog, minimize, minimize_scalar

from .markets import Outcome, Selection, settle


# ---------------------------------------------------------------------------
# Fórmulas cerradas
# ---------------------------------------------------------------------------
def lay_hedge(back_stake: float, back_odds: float, lay_odds: float, commission: float = 0.05) -> dict:
    """Lay en exchange para igualar el resultado en ambos casos."""
    lay_stake = back_stake * back_odds / (lay_odds - commission)
    win = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    lose = -back_stake + lay_stake * (1 - commission)
    return {"lay_stake": lay_stake, "liability": lay_stake * (lay_odds - 1), "if_back_wins": win, "if_back_loses": lose}


def hedge_to_zero(stake: float, hedge_odds: float) -> float:
    """Stake de cobertura que deja a 0 la rama en la que gana la cobertura
    (la apuesta original pierde `stake` y la cobertura lo recupera)."""
    return stake / (hedge_odds - 1.0)


def hedge_equalize(stake: float, odds: float, hedge_odds: float) -> float:
    """Stake de cobertura que iguala el beneficio de las dos ramas ganadoras
    (apuesta original gana / cobertura gana)."""
    return stake * odds / hedge_odds


def dutch(odds: list[float], total_stake: float | None = None, target_return: float | None = None) -> dict:
    inv = [1.0 / o for o in odds]
    if target_return is not None:
        stakes = [target_return / o for o in odds]
    else:
        stakes = [total_stake * i / sum(inv) for i in inv]
    tot = sum(stakes)
    ret = stakes[0] * odds[0]
    return {"stakes": stakes, "total": tot, "return": ret, "profit": ret - tot, "book": sum(inv)}


def green_up(stake: float, odds_taken: float, odds_now: float) -> dict:
    """Cash-out justo: cuánto vale hoy tu apuesta a las cuotas actuales y el lay/back equivalente."""
    value = stake * odds_taken / odds_now
    return {"fair_cashout": value, "profit_locked": value - stake, "hedge_stake_back_opposite_equiv": value}


def split_quarter(line: float, stake: float) -> list[tuple[float, float]]:
    """AH de cuarto = dos medias apuestas."""
    frac = round((line * 4) % 2, 6)
    if abs(frac - 1) < 1e-6:
        return [(line - 0.25, stake / 2), (line + 0.25, stake / 2)]
    return [(line, stake)]


def kelly_stake(prob: float, odds: float, bankroll: float, fraction: float = 0.25,
                push_prob: float = 0.0, cap_pct: float | None = 0.05) -> float:
    """Kelly fraccional (con push para líneas asiáticas)."""
    b = odds - 1.0
    w, p = prob, push_prob
    lose = max(0.0, 1.0 - w - p)
    if b <= 0 or w <= 0:
        return 0.0
    # Kelly con 3 resultados: f* = (w*b - lose) / (b * (w + lose)) aproximación exacta para push
    f = (w * b - lose) / (b * (w + lose)) if (w + lose) > 0 else 0.0
    f = max(0.0, f) * fraction
    if cap_pct is not None:
        f = min(f, cap_pct)
    return bankroll * f


# ---------------------------------------------------------------------------
# Solver general sobre rejilla de estados
# ---------------------------------------------------------------------------
@dataclass
class Position:
    sel: Selection
    odds: float
    stake: float
    label: str = ""


@dataclass
class HedgeResult:
    stakes: list[float]
    objective: str
    branches: dict[str, float]
    ev: float
    ev_without: float
    std: float
    std_without: float
    worst: float
    best: float
    hedge_cost_ev: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _state_grid(score_probs: np.ndarray, min_p: float = 1e-7):
    states, probs = [], []
    for i in range(score_probs.shape[0]):
        for j in range(score_probs.shape[1]):
            if score_probs[i, j] > min_p:
                states.append((i, j))
                probs.append(score_probs[i, j])
    probs = np.array(probs)
    return states, probs / probs.sum()


def _returns_matrix(sels: list[Selection], odds: list[float], states) -> np.ndarray:
    """R[k, s] = beneficio neto por unidad apostada en la selección k si el estado final es s."""
    R = np.zeros((len(sels), len(states)))
    for k, (sel, o) in enumerate(zip(sels, odds)):
        for s, (i, j) in enumerate(states):
            w, p = settle(sel, Outcome(i, j))
            R[k, s] = w * (o - 1.0) - (1.0 - w - p)
    return R


def branch_of(i: int, j: int) -> str:
    return "Gana local" if i > j else ("Gana visitante" if j > i else "Empate")


def solve_hedge(positions: list[Position], hedges: list[tuple[Selection, float]], score_probs: np.ndarray,
                objective: str = "equalize", bankroll: float | None = None, risk_aversion: float = 1.0,
                zero_branch: str | None = None, max_total: float | None = None,
                branches: list[str] | None = None) -> HedgeResult:
    """Calcula los stakes de cobertura.

    objective:
      - "equalize": maximiza el peor resultado (programación lineal). Si hay
        arbitraje da beneficio garantizado; si no, el peor caso menos malo.
      - "balance": iguala el resultado de las ramas `branches` (por defecto local/visitante).
      - "zero": deja a 0 la rama `zero_branch` ("Gana local", "Empate", "Gana visitante").
      - "kelly": maximiza E[log(banca)] (necesita bankroll).
      - "meanvar": maximiza EV − risk_aversion·Var/(2·banca).
      - "minvar": mínima varianza.
    """
    states, probs = _state_grid(score_probs)
    base = np.zeros(len(states))
    for pos in positions:
        base += pos.stake * _returns_matrix([pos.sel], [pos.odds], states)[0]
    sels = [h[0] for h in hedges]
    odds = [h[1] for h in hedges]
    R = _returns_matrix(sels, odds, states)
    n = len(sels)
    notes: list[str] = []
    bounds = [(0, max_total) for _ in range(n)]

    if n == 0:
        x = np.zeros(0)
    elif objective == "equalize":
        # max t  s.a.  base_s + sum_k x_k R[k,s] >= t
        c = np.zeros(n + 1)
        c[-1] = -1
        A = np.hstack([-R.T, np.ones((len(states), 1))])
        b = base.copy()
        bnds = bounds + [(None, None)]
        A_ub, b_ub = A, b
        if max_total is not None:
            A_ub = np.vstack([A_ub, np.r_[np.ones(n), 0]])
            b_ub = np.r_[b_ub, max_total]
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bnds, method="highs")
        x = res.x[:n] if res.success else np.zeros(n)
        if not res.success:
            notes.append("El solver no encontró solución; no se cubre.")
        else:
            # 2ª pasada: entre las soluciones con el mismo peor caso, la de mayor EV
            # y, a igualdad, la de menor stake total.
            t_star = res.x[-1] - 1e-6
            ev_coef = -(R @ probs) + 1e-6
            A2 = -R.T
            b2 = base - t_star
            if max_total is not None:
                A2 = np.vstack([A2, np.ones(n)])
                b2 = np.r_[b2, max_total]
            res2 = linprog(ev_coef, A_ub=A2, b_ub=b2, bounds=bounds, method="highs")
            if res2.success:
                x = res2.x
            if t_star + 1e-6 < 0:
                notes.append("No hay beneficio garantizado: esto es el peor caso menos malo.")
            else:
                notes.append("Beneficio garantizado en todos los marcadores.")
    elif objective == "zero":
        if zero_branch is None:
            raise ValueError("zero_branch requerido")
        mask = np.array([branch_of(i, j) == zero_branch for i, j in states])
        w = probs * mask
        w = w / w.sum() if w.sum() > 0 else mask / max(mask.sum(), 1)
        def f(x):
            pl = base + x @ R
            return float((w * pl ** 2).sum())
        res = minimize(f, x0=np.ones(n), bounds=[(0, None)] * n)
        x = res.x
    elif objective == "balance":
        # iguala el resultado medio de las ramas indicadas (p. ej. Gana local / Gana visitante)
        names = branches or ["Gana local", "Gana visitante"]
        masks = [np.array([branch_of(i, j) == b for i, j in states]) for b in names]
        def f(x):
            pl = base + x @ R
            avgs = []
            for m in masks:
                pm = probs * m
                avgs.append(float((pm * pl).sum() / pm.sum()) if pm.sum() > 0 else 0.0)
            return sum((avgs[a] - avgs[b]) ** 2 for a in range(len(avgs)) for b in range(a + 1, len(avgs))) + 1e-9 * float(x.sum())
        best = None
        for x0 in (1.0, 10.0, 100.0, 1000.0):
            r = minimize(f, x0=np.full(n, x0), bounds=[(0, max_total) for _ in range(n)], method="L-BFGS-B")
            if best is None or r.fun < best.fun:
                best = r
        x = best.x
    elif objective in ("kelly", "meanvar", "minvar"):
        bk = bankroll or 1000.0
        def f(x):
            pl = base + x @ R
            if objective == "kelly":
                wealth = bk + pl
                if np.any(wealth <= 0):
                    return 1e9
                return -float((probs * np.log(wealth)).sum())
            mu = float((probs * pl).sum())
            var = float((probs * (pl - mu) ** 2).sum())
            if objective == "minvar":
                return var
            return -(mu - risk_aversion * var / (2 * bk))
        best = None
        for x0 in (0.0, 5.0, 50.0):
            r = minimize(f, x0=np.full(n, x0), bounds=[(0, max_total) for _ in range(n)], method="L-BFGS-B")
            if best is None or r.fun < best.fun:
                best = r
        x = best.x
    else:
        raise ValueError(objective)

    x = np.maximum(0, np.asarray(x, dtype=float))
    pl = base + (x @ R if n else 0)
    ev = float((probs * pl).sum())
    ev0 = float((probs * base).sum())
    std = float(math.sqrt((probs * (pl - ev) ** 2).sum()))
    std0 = float(math.sqrt((probs * (base - ev0) ** 2).sum()))
    branches: dict[str, list] = {}
    for (i, j), p_, v in zip(states, probs, pl):
        b = branch_of(i, j)
        branches.setdefault(b, [0.0, 0.0])
        branches[b][0] += p_ * v
        branches[b][1] += p_
    branch_avg = {b: float(s / pp if pp > 0 else 0.0) for b, (s, pp) in branches.items()}
    return HedgeResult(stakes=[round(float(v), 2) for v in x], objective=objective, branches=branch_avg,
                       ev=ev, ev_without=ev0, std=std, std_without=std0,
                       worst=float(pl[probs > 1e-4].min()) if np.any(probs > 1e-4) else float(pl.min()),
                       best=float(pl.max()), hedge_cost_ev=ev - ev0, notes=notes)


def pnl_table(positions: list[Position], score_probs: np.ndarray) -> dict[str, float]:
    states, probs = _state_grid(score_probs)
    base = np.zeros(len(states))
    for pos in positions:
        base += pos.stake * _returns_matrix([pos.sel], [pos.odds], states)[0]
    out: dict[str, list] = {}
    for (i, j), p_, v in zip(states, probs, base):
        out.setdefault(branch_of(i, j), [0.0, 0.0])
        out[branch_of(i, j)][0] += p_ * v
        out[branch_of(i, j)][1] += p_
    return {k: float(s / pp) for k, (s, pp) in out.items() if pp > 0}
