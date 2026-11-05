"""Utilidades de distribuciones de marcador."""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson

MAXG = 11


def dc_tau(i: int, j: int, lh: float, la: float, rho: float) -> float:
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


_LOGFACT = np.array([math.lgamma(k + 1) for k in range(40)])


def _pmf(lam: float, n: int) -> np.ndarray:
    k = np.arange(n)
    return np.exp(k * math.log(lam) - lam - _LOGFACT[:n])


def score_matrix(lh: float, la: float, rho: float = 0.0, maxg: int = MAXG) -> np.ndarray:
    ph = _pmf(max(lh, 1e-6), maxg)
    pa = _pmf(max(la, 1e-6), maxg)
    m = np.outer(ph, pa)
    if rho:
        for i in (0, 1):
            for j in (0, 1):
                m[i, j] *= max(dc_tau(i, j, lh, la, rho), 1e-9)
    return m / m.sum()


def probs_1x2(m: np.ndarray) -> tuple[float, float, float]:
    return float(np.tril(m, -1).sum()), float(np.trace(m)), float(np.triu(m, 1).sum())


def prob_over(m: np.ndarray, line: float) -> float:
    n = m.shape[0]
    tot = np.add.outer(np.arange(n), np.arange(m.shape[1]))
    return float(m[tot > line].sum())


def prob_btts(m: np.ndarray) -> float:
    return float(m[1:, 1:].sum())


def implied_lambdas(p_home: float, p_draw: float, p_away: float, p_over25: float | None = None,
                    rho: float = -0.05, prior_total: float = 2.65) -> tuple[float, float]:
    """λ local/visitante que reproducen unas probabilidades de mercado sin margen."""
    def loss(x):
        lh, la = np.exp(x)
        m = score_matrix(lh, la, rho)
        h, d, a = probs_1x2(m)
        e = (h - p_home) ** 2 + (d - p_draw) ** 2 + (a - p_away) ** 2
        if p_over25 is not None:
            e += (prob_over(m, 2.5) - p_over25) ** 2
        else:
            e += 0.002 * (lh + la - prior_total) ** 2
        return e
    best = None
    for x0 in ([math.log(1.5), math.log(1.1)], [math.log(1.1), math.log(1.5)], [math.log(1.3), math.log(1.3)]):
        r = minimize(loss, x0=np.array(x0), method="Nelder-Mead", options={"xatol": 1e-5, "fatol": 1e-10, "maxiter": 400})
        if best is None or r.fun < best.fun:
            best = r
    lh, la = np.exp(best.x)
    return float(lh), float(la)


# ---------------------------------------------------------------------------
# Inversión rápida y vectorizada (rejilla precalculada) para miles de partidos
# ---------------------------------------------------------------------------
_GRID = None


def _grid():
    global _GRID
    if _GRID is None:
        from scipy.stats import poisson as _p
        lam = np.arange(0.15, 4.2, 0.025)
        k = np.arange(MAXG)
        pm = _p.pmf(k[None, :], lam[:, None])           # (L, K)
        L = len(lam)
        ph = np.zeros((L, L)); pd_ = np.zeros((L, L)); po = np.zeros((L, L))
        tot = np.add.outer(k, k)
        low = tot <= 2
        tri_l = np.tril(np.ones((MAXG, MAXG)), -1).astype(bool)
        diag = np.eye(MAXG, dtype=bool)
        for i in range(L):
            M = pm[i][:, None] * pm[:, None, :]           # (L, K, K): local λ_i, visitante λ_j
            # corrección DC ρ=-0.05 en marcadores bajos
            rho = -0.05
            lh, la = lam[i], lam
            M[:, 0, 0] *= (1 - lh * la * rho)
            M[:, 0, 1] *= (1 + lh * rho)
            M[:, 1, 0] *= (1 + la * rho)
            M[:, 1, 1] *= (1 - rho)
            M /= M.sum(axis=(1, 2), keepdims=True)
            ph[i] = M[:, tri_l].sum(axis=1)
            pd_[i] = M[:, diag].sum(axis=1)
            po[i] = 1 - M[:, low].sum(axis=1)
        _GRID = (lam, ph.ravel(), pd_.ravel(), po.ravel())
    return _GRID


def implied_lambdas_vec(pH: np.ndarray, pD: np.ndarray, pA: np.ndarray, pO: np.ndarray | None = None,
                        prior_total: float = 2.65) -> tuple[np.ndarray, np.ndarray]:
    """Versión vectorizada de implied_lambdas (precisión ±0.0125 en λ)."""
    lam, gh, gd, go = _grid()
    L = len(lam)
    pH = np.asarray(pH, float); pD = np.asarray(pD, float)
    pO = None if pO is None else np.asarray(pO, float)
    out_h = np.full(len(pH), np.nan); out_a = np.full(len(pH), np.nan)
    tot_grid = (lam[:, None] + lam[None, :]).ravel()
    for s in range(0, len(pH), 256):
        sl = slice(s, s + 256)
        e = (gh[None, :] - pH[sl, None]) ** 2 + (gd[None, :] - pD[sl, None]) ** 2
        if pO is not None:
            po = pO[sl]
            has = np.isfinite(po)
            e = e + np.where(has[:, None], (go[None, :] - np.nan_to_num(po)[:, None]) ** 2,
                             0.002 * (tot_grid[None, :] - prior_total) ** 2)
        else:
            e = e + 0.002 * (tot_grid[None, :] - prior_total) ** 2
        idx = np.argmin(e, axis=1)
        ok = np.isfinite(pH[sl]) & np.isfinite(pD[sl])
        out_h[sl] = np.where(ok, lam[idx // L], np.nan)
        out_a[sl] = np.where(ok, lam[idx % L], np.nan)
    return out_h, out_a
