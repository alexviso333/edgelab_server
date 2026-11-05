"""Mercados secundarios: córners, tarjetas, tiros, tiros a puerta.

Media esperada por equipo = media de liga × fuerza "a favor" del equipo ×
fuerza "en contra" del rival (con contracción hacia la media según el número de
partidos), × árbitro (tarjetas) × derbi. La distribución es binomial negativa
(admite sobre-dispersión) y el total es la convolución de los dos equipos.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import nbinom, poisson

STAT_KEYS = {
    # clave interna: (claves posibles en Statshub, claves en football-data)
    "corners": (("cornerKicks",), ("corners",)),
    "cards": (("cards", "yellowCards"), ("yellow",)),
    "fouls": (("fouls",), ("fouls",)),
    "shots": (("totalShotsOnGoal", "totalShots"), ("shots",)),
    "sot": (("shotsOnGoal",), ("sot",)),
    "xg": (("expectedGoals", "xg_understat"), ()),
}


def stat_value(st: dict, key: str) -> float | None:
    sh, fd = STAT_KEYS[key]
    for k in sh + fd:
        v = st.get(k)
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if key == "cards" and k == "yellowCards":
                v += float(st.get("redCards") or 0)
            if key == "cards" and k == "yellow":
                v += float(st.get("red") or 0)
            return v
    return None


@dataclass
class CountForecast:
    mu_h: float
    mu_a: float
    disp: float = 25.0   # parámetro r de la NB (∞ = Poisson)

    def pmf_team(self, mu: float, n: int = 40) -> np.ndarray:
        k = np.arange(n)
        if self.disp > 500:
            return poisson.pmf(k, mu)
        r = self.disp
        p = r / (r + mu)
        return nbinom.pmf(k, r, p)

    def total_pmf(self, n: int = 40) -> np.ndarray:
        a, b = self.pmf_team(self.mu_h, n), self.pmf_team(self.mu_a, n)
        t = np.convolve(a, b)[:n]
        return t / t.sum()

    def p_over(self, line: float, side: str | None = None) -> tuple[float, float, float]:
        """(P ganar, P push, P perder) del Over (side None=total, 'H'/'A'=equipo)."""
        pmf = self.total_pmf() if side is None else self.pmf_team(self.mu_h if side == "H" else self.mu_a)
        k = np.arange(len(pmf))
        parts = [line - 0.25, line + 0.25] if abs((line * 4) % 2 - 1) < 1e-6 else [line]
        w = p = 0.0
        for L in parts:
            w += pmf[k > L + 1e-9].sum() / len(parts)
            p += pmf[np.abs(k - L) < 1e-9].sum() / len(parts)
        return float(w), float(p), float(max(0, 1 - w - p))

    def p_handicap(self, side: str, line: float) -> tuple[float, float, float]:
        a, b = self.pmf_team(self.mu_h), self.pmf_team(self.mu_a)
        joint = np.outer(a, b)
        n = len(a)
        diff = np.subtract.outer(np.arange(n), np.arange(n))
        if side == "A":
            diff = -diff
        parts = [line - 0.25, line + 0.25] if abs((line * 4) % 2 - 1) < 1e-6 else [line]
        w = p = 0.0
        for L in parts:
            w += joint[diff + L > 1e-9].sum() / len(parts)
            p += joint[np.abs(diff + L) < 1e-9].sum() / len(parts)
        return float(w), float(p), float(max(0, 1 - w - p))


def shrink(rate: float, n: float, prior: float, k: float = 6.0) -> float:
    return (rate * n + prior * k) / (n + k)


def forecast_counts(team_for_h: tuple[float, int], team_against_a: tuple[float, int],
                    team_for_a: tuple[float, int], team_against_h: tuple[float, int],
                    league_avg_h: float, league_avg_a: float, extra_mult: float = 1.0, disp: float = 25.0) -> CountForecast:
    """Cada argumento (media, nº partidos). Modelo multiplicativo con contracción."""
    fh = shrink(team_for_h[0], team_for_h[1], league_avg_h) / max(league_avg_h, 1e-6)
    aa = shrink(team_against_a[0], team_against_a[1], league_avg_h) / max(league_avg_h, 1e-6)
    fa = shrink(team_for_a[0], team_for_a[1], league_avg_a) / max(league_avg_a, 1e-6)
    ah = shrink(team_against_h[0], team_against_h[1], league_avg_a) / max(league_avg_a, 1e-6)
    return CountForecast(league_avg_h * fh * aa * extra_mult, league_avg_a * fa * ah * extra_mult, disp)


def referee_factor(ref_avg_cards: float | None, ref_games: int | None, league_avg_cards: float) -> float:
    if not ref_avg_cards or not ref_games:
        return 1.0
    shr = shrink(ref_avg_cards, ref_games, league_avg_cards, k=10)
    return shr / max(league_avg_cards, 1e-6)


def fit_dispersion(values: list[float], means: list[float]) -> float:
    """Estima r de la NB por momentos (var = mu + mu²/r)."""
    v = np.array(values, float)
    m = np.array(means, float)
    if len(v) < 30:
        return 25.0
    resid = ((v - m) ** 2 - m)
    denom = (m ** 2).sum()
    if denom <= 0:
        return 25.0
    inv_r = max(resid.sum() / denom, 1e-3)
    return float(min(1000.0, 1.0 / inv_r))
