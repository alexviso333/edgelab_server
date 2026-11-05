"""Dixon-Coles con ponderación temporal, en dos versiones:
- sobre goles (clásico, con corrección ρ de marcadores bajos)
- sobre xG (más estable: el xG tiene mucha menos varianza que los goles)

Ajuste por máxima verosimilitud con gradiente analítico (rápido incluso en la
VM gratuita).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from .poisson import score_matrix


@dataclass
class DCModel:
    teams: list[int] = field(default_factory=list)
    attack: dict[int, float] = field(default_factory=dict)
    defence: dict[int, float] = field(default_factory=dict)
    home: float = 0.25
    rho: float = -0.05
    mu: float = 0.1
    fitted_at: int = 0
    kind: str = "goals"

    def lambdas(self, h: int, a: int, neutral: bool = False) -> tuple[float, float] | None:
        if h not in self.attack or a not in self.attack:
            return None
        ha = 0.0 if neutral else self.home
        lh = math.exp(self.mu + ha + self.attack[h] + self.defence[a])
        la = math.exp(self.mu + self.attack[a] + self.defence[h])
        return lh, la

    def matrix(self, h: int, a: int, neutral: bool = False):
        l = self.lambdas(h, a, neutral)
        if l is None:
            return None
        return score_matrix(l[0], l[1], self.rho if self.kind == "goals" else 0.0)

    def to_dict(self) -> dict:
        return {"teams": self.teams, "attack": {str(k): v for k, v in self.attack.items()},
                "defence": {str(k): v for k, v in self.defence.items()}, "home": self.home, "rho": self.rho,
                "mu": self.mu, "fitted_at": self.fitted_at, "kind": self.kind}

    @staticmethod
    def from_dict(d: dict) -> "DCModel":
        m = DCModel(teams=d["teams"], home=d["home"], rho=d["rho"], mu=d["mu"], fitted_at=d["fitted_at"], kind=d.get("kind", "goals"))
        m.attack = {int(k): v for k, v in d["attack"].items()}
        m.defence = {int(k): v for k, v in d["defence"].items()}
        return m


def fit(matches: list[dict], at_ts: int, xi: float = 0.0019, kind: str = "goals", min_weight: float = 0.02,
        l2: float = 0.5, neutral_comps: tuple = (), warm: "DCModel | None" = None) -> DCModel:
    """matches: dicts con home_id, away_id, kickoff y (hg, ag) o (xgh, xga). Solo usa partidos anteriores a `at_ts`."""
    rows = []
    for m in matches:
        if m["kickoff"] >= at_ts:
            continue
        if kind == "goals":
            if m.get("hg") is None:
                continue
            gh, ga = float(m["hg"]), float(m["ag"])
        else:
            if m.get("xgh") is None:
                continue
            gh, ga = float(m["xgh"]), float(m["xga"])
        w = math.exp(-xi * (at_ts - m["kickoff"]) / 86400.0)
        if w < min_weight:
            continue
        rows.append((m["home_id"], m["away_id"], gh, ga, w, 0.0 if m.get("comp") in neutral_comps else 1.0))
    model = DCModel(fitted_at=at_ts, kind=kind)
    if len(rows) < 30:
        return model
    teams = sorted({r[0] for r in rows} | {r[1] for r in rows})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    H = np.array([idx[r[0]] for r in rows])
    A = np.array([idx[r[1]] for r in rows])
    GH = np.array([r[2] for r in rows])
    GA = np.array([r[3] for r in rows])
    W = np.array([r[4] for r in rows])
    HA = np.array([r[5] for r in rows])

    def nll(x):
        att, de, home, mu = x[:n], x[n:2 * n], x[2 * n], x[2 * n + 1]
        lh = np.exp(mu + home * HA + att[H] + de[A])
        la = np.exp(mu + att[A] + de[H])
        ll = W * (GH * np.log(lh) - lh + GA * np.log(la) - la)
        f = -ll.sum() + l2 * (np.sum(att ** 2) + np.sum(de ** 2))
        # gradiente
        rh = W * (GH - lh)
        ra = W * (GA - la)
        g = np.zeros_like(x)
        np.add.at(g, H, -rh)
        np.add.at(g, A, -ra)
        np.add.at(g, n + A, -rh)
        np.add.at(g, n + H, -ra)
        g[:n] += 2 * l2 * att
        g[n:2 * n] += 2 * l2 * de
        g[2 * n] = -(rh * HA).sum()
        g[2 * n + 1] = -(rh.sum() + ra.sum())
        return f, g

    x0 = np.zeros(2 * n + 2)
    x0[2 * n] = 0.25
    x0[2 * n + 1] = math.log(max(1e-3, (GH * W).sum() / W.sum() * 0.5 + (GA * W).sum() / W.sum() * 0.5))
    if warm is not None and warm.attack:
        for t, i in idx.items():
            x0[i] = warm.attack.get(t, 0.0)
            x0[n + i] = warm.defence.get(t, 0.0)
        x0[2 * n], x0[2 * n + 1] = warm.home, warm.mu
    res = minimize(nll, x0, jac=True, method="L-BFGS-B")
    x = res.x
    model.teams = teams
    model.attack = {t: float(x[idx[t]]) for t in teams}
    model.defence = {t: float(x[n + idx[t]]) for t in teams}
    model.home = float(x[2 * n])
    model.mu = float(x[2 * n + 1])
    if kind == "goals":
        model.rho = _fit_rho(model, rows)
    return model


def _fit_rho(model: DCModel, rows) -> float:
    low = [(h, a, gh, ga, w) for h, a, gh, ga, w, _ in rows if gh <= 1 and ga <= 1]
    if not low:
        return -0.05
    lam = np.array([model.lambdas(h, a) for h, a, *_ in low])
    lh, la = lam[:, 0], lam[:, 1]
    gh = np.array([r[2] for r in low]).astype(int)
    ga = np.array([r[3] for r in low]).astype(int)
    w = np.array([r[4] for r in low])
    best, best_v = -0.05, math.inf
    for rho in np.linspace(-0.25, 0.15, 41):
        t = np.ones_like(lh)
        t = np.where((gh == 0) & (ga == 0), 1 - lh * la * rho, t)
        t = np.where((gh == 0) & (ga == 1), 1 + lh * rho, t)
        t = np.where((gh == 1) & (ga == 0), 1 + la * rho, t)
        t = np.where((gh == 1) & (ga == 1), 1 - rho, t)
        v = -(w * np.log(np.maximum(t, 1e-9))).sum()
        if v < best_v:
            best, best_v = float(rho), v
    return best
