"""Validación estadística: lo que separa una estrategia real del ruido.

- Yield, ROI, intervalo de confianza por bootstrap.
- p-valor (una cola) de que el yield real sea > 0.
- Contracción bayesiana (empirical Bayes) del yield hacia −margen: 30 apuestas
  con +40 % pesan menos que 800 con +4 %.
- Probabilidad posterior de que la estrategia gane dinero.
- Benjamini-Hochberg sobre TODAS las variantes probadas (también las
  descartadas), para controlar los falsos positivos de la búsqueda masiva.
- CLV medio contra el cierre de Pinnacle (el mejor predictor de rentabilidad
  futura que existe).
- Dependencia de pocas cuotas altas (¿sigue ganando sin el 1 % de mejores apuestas?).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import norm, t as student_t

PRIOR_MEAN = -0.035   # una estrategia cualquiera pierde aprox. el margen
PRIOR_SD = 0.035      # yields reales sostenidos rara vez pasan de ±7 %


def summarize(bets: pd.DataFrame, n_boot: int = 1000, seed: int = 0) -> dict:
    if bets is None or bets.empty:
        return {"n": 0}
    stake = bets["stake"].to_numpy(float)
    pnl = bets["pnl"].to_numpy(float)
    n = len(pnl)
    staked = float(stake.sum())
    profit = float(pnl.sum())
    y = profit / staked if staked else 0.0
    r = pnl / np.where(stake > 0, stake, 1)
    sd = float(r.std(ddof=1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    rng = np.random.default_rng(seed)
    if n >= 10:
        idx = rng.integers(0, n, size=(n_boot, n))
        boots = pnl[idx].sum(axis=1) / stake[idx].sum(axis=1)
        lo, hi = np.percentile(boots, [5, 95])
    else:
        lo, hi = float("nan"), float("nan")
    pval = float(1 - student_t.cdf(r.mean() / se, df=n - 1)) if n > 2 and se > 0 else 1.0
    post_mean, post_sd = shrink(float(r.mean()), se)
    p_pos = float(1 - norm.cdf(0, loc=post_mean, scale=post_sd))
    odds = bets["odds"].to_numpy(float) if "odds" in bets.columns else np.full(n, np.nan)
    wins = bets["pnl"] > 0
    top = np.sort(pnl)[::-1]
    k = max(1, int(round(0.01 * n)))
    top_share = float(top[:k].sum() / profit) if profit > 0 else float("nan")   # >1: sin el 1 % mejor, pierde
    out = {
        "n": n, "staked": round(staked, 2), "profit": round(profit, 2), "yield": round(y, 4),
        "ci_lo": round(float(lo), 4), "ci_hi": round(float(hi), 4), "pvalue": round(pval, 5),
        "shrunk": round(post_mean, 4), "shrunk_sd": round(post_sd, 4), "p_profitable": round(p_pos, 4),
        "hit_rate": round(float(wins.mean()), 4), "avg_odds": round(float(np.nanmean(odds)), 3) if n else None,
        "max_drawdown": round(max_drawdown(pnl), 2), "top1pct_share": round(top_share, 3) if top_share == top_share else None,
        "longest_losing": longest_run(pnl < 0),
    }
    if "clv" in bets.columns and bets["clv"].notna().any():
        out["clv"] = round(float(bets["clv"].mean()), 4)
        out["clv_n"] = int(bets["clv"].notna().sum())
        out["clv_pos_rate"] = round(float((bets["clv"] > 0).mean()), 4)
    if "price_source" in bets.columns:
        out["price_source"] = "real" if (bets["price_source"] == "real").mean() > 0.5 else "simulated"
    if "season" in bets.columns:
        by = bets.groupby("season").apply(lambda g: g["pnl"].sum() / g["stake"].sum() if g["stake"].sum() else 0)
        out["by_season"] = {str(k): round(float(v), 4) for k, v in by.items()}
        out["seasons_positive"] = int((by > 0).sum())
        out["seasons"] = int(len(by))
    return out


def shrink(mean: float, se: float, prior_mean: float = PRIOR_MEAN, prior_sd: float = PRIOR_SD) -> tuple[float, float]:
    if not np.isfinite(se) or se <= 0:
        return prior_mean, prior_sd
    w_data = 1 / se ** 2
    w_prior = 1 / prior_sd ** 2
    post = (mean * w_data + prior_mean * w_prior) / (w_data + w_prior)
    return float(post), float(math.sqrt(1 / (w_data + w_prior)))


def max_drawdown(pnl: np.ndarray) -> float:
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0, eq])[1:]
    return float((peak - eq).max()) if len(eq) else 0.0


def longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return int(best)


def benjamini_hochberg(pvals: list[float], q: float = 0.10) -> list[bool]:
    p = np.asarray(pvals, float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p)
    thresh = q * (np.arange(1, n + 1) / n)
    passed = p[order] <= thresh
    k = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(n, bool)
    out[order[:k]] = True
    return list(out)


def bh_adjusted(pvals: list[float]) -> list[float]:
    p = np.asarray(pvals, float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p)
    adj = p[order] * n / np.arange(1, n + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(adj, 1)
    return list(out)


def promotion_verdict(oos: dict, is_: dict | None, bh_ok: bool, min_n: int = 100) -> tuple[str, list[str]]:
    """Decide el estado de una estrategia a partir de su test fuera de muestra."""
    reasons = []
    if oos.get("n", 0) < min_n:
        reasons.append(f"solo {oos.get('n', 0)} apuestas fuera de muestra (mín. {min_n})")
    if oos.get("shrunk", -1) <= 0:
        reasons.append(f"yield contraído {oos.get('shrunk', 0):+.1%} ≤ 0")
    if not bh_ok:
        reasons.append("no supera Benjamini-Hochberg (FDR 10 %) entre todas las variantes probadas")
    if oos.get("clv") is not None and oos["clv"] <= 0:
        reasons.append(f"CLV medio {oos['clv']:+.1%} ≤ 0")
    if oos.get("top1pct_share") and oos["top1pct_share"] > 1.0:
        reasons.append("sin el 1 % de apuestas más premiadas, pierde dinero (depende de pocas cuotas altas)")
    if oos.get("seasons") and oos.get("seasons_positive", 0) < max(1, oos["seasons"] // 2):
        reasons.append("positiva en menos de la mitad de las temporadas")
    if not reasons:
        return "validated", ["supera todos los filtros fuera de muestra"]
    if oos.get("n", 0) >= 30 and oos.get("shrunk", -1) > -0.01 and oos.get("p_profitable", 0) > 0.4:
        return "candidate", reasons
    return "rejected", reasons
