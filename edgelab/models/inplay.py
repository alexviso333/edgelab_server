"""Modelo en directo: cuota justa de cualquier mercado en cualquier minuto.

Es una cadena de Markov minuto a minuto sobre el marcador. En cada minuto la
probabilidad de gol de cada equipo depende de:
  - su λ pre-partido (sacado de las cuotas de cierre, idealmente Pinnacle),
  - el perfil temporal (se marca más al final de cada parte y en el añadido),
  - el estado del marcador (quien pierde aprieta, quien gana se protege),
  - las expulsiones,
  - el xG en vivo, si lo hay (actualización bayesiana: si un equipo genera más
    xG de lo esperado, sube su intensidad para lo que queda).
Todos los parámetros se recalibran con los partidos jugados (máxima
verosimilitud sobre la cronología de goles), así el modelo aprende solo.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.optimize import minimize

from ..core.markets import Selection, fair_odds, selection_probs

MAXG = 12
T_END = 96  # minutos efectivos (90 + añadido medio de la 2ª parte; el de la 1ª va en el 45)


@dataclass
class InplayParams:
    # perfil temporal: intensidad relativa = 1 + slope*(m/90), +bump en el añadido de cada parte
    slope: float = 0.35
    ht_bump: float = 1.9       # minuto 45 concentra el añadido de la 1ª parte
    ft_bump: float = 1.35      # minutos 90-96
    # multiplicadores por estado del marcador (desde el punto de vista del equipo)
    trail1: float = 1.12
    trail2: float = 1.05
    lead1: float = 0.92
    lead2: float = 0.90
    # expulsiones
    red_self: float = 0.68
    red_opp: float = 1.28
    # fuerza del prior para el ajuste por xG en vivo (en unidades de xG)
    xg_prior: float = 1.6
    home_lead_extra: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict | None) -> "InplayParams":
        p = InplayParams()
        for k, v in (d or {}).items():
            if hasattr(p, k):
                setattr(p, k, float(v))
        return p


def time_profile(p: InplayParams) -> np.ndarray:
    """w[m] para m=1..T_END (índice 0 = minuto 1), normalizado a suma 1."""
    m = np.arange(1, T_END + 1, dtype=float)
    w = 1.0 + p.slope * np.minimum(m, 90) / 90.0
    w[44] *= p.ht_bump          # minuto 45 (+añadido)
    w[90:] *= p.ft_bump         # 91-96
    return w / w.sum()


def _state_mult(diff: int, p: InplayParams) -> float:
    if diff <= -2:
        return p.trail2
    if diff == -1:
        return p.trail1
    if diff == 1:
        return p.lead1
    if diff >= 2:
        return p.lead2
    return 1.0


@dataclass
class LiveState:
    minute: float
    hg: int = 0
    ag: int = 0
    red_h: int = 0
    red_a: int = 0
    xg_h: float | None = None
    xg_a: float | None = None
    added_time: float | None = None   # minutos de añadido anunciados (2ª parte)


@dataclass
class InplayModel:
    params: InplayParams = field(default_factory=InplayParams)

    def _profile(self, added: float | None = None) -> np.ndarray:
        w = time_profile(self.params)
        if added is not None and added > 0:
            # el tramo 91-96 representa ~6' de añadido medio: se escala al anunciado
            w = w.copy()
            w[90:] *= max(0.0, min(15.0, float(added))) / 6.0
        return w

    def xg_factor(self, lam: float, xg: float | None, cum_w: float) -> float:
        if xg is None or cum_w <= 0.05:
            return 1.0
        k = self.params.xg_prior
        exp_xg = lam * cum_w
        return (k + xg) / (k + exp_xg)

    def final_distribution(self, lh: float, la: float, st: LiveState) -> np.ndarray:
        """Matriz P[i, j] del marcador FINAL dado el estado actual."""
        p = self.params
        w = self._profile(st.added_time)
        start = int(math.floor(st.minute))
        cum = float(w[:start].sum())
        fh = self.xg_factor(lh, st.xg_h, cum)
        fa = self.xg_factor(la, st.xg_a, cum)
        P = np.zeros((MAXG, MAXG))
        P[min(st.hg, MAXG - 1), min(st.ag, MAXG - 1)] = 1.0
        rh = p.red_self ** st.red_h * p.red_opp ** st.red_a
        ra = p.red_self ** st.red_a * p.red_opp ** st.red_h
        I = np.arange(MAXG)[:, None]
        J = np.arange(MAXG)[None, :]
        diff = I - J
        mult_h = np.vectorize(lambda d: _state_mult(int(d), p))(diff)
        mult_a = np.vectorize(lambda d: _state_mult(int(-d), p))(diff)
        for m in range(start, T_END):
            frac = 1.0 if m > st.minute else (m + 1 - st.minute)
            if frac <= 0:
                continue
            muh = lh * fh * rh * w[m] * frac * mult_h
            mua = la * fa * ra * w[m] * frac * mult_a
            ph = 1 - np.exp(-muh)
            pa = 1 - np.exp(-mua)
            stay = (1 - ph) * (1 - pa)
            newP = P * stay
            newP[1:, :] += (P * ph * (1 - pa))[:-1, :]
            newP[:, 1:] += (P * pa * (1 - ph))[:, :-1]
            newP[1:, 1:] += (P * ph * pa)[:-1, :-1]
            # desbordes al borde de la rejilla
            newP[-1, :] += (P * ph * (1 - pa))[-1, :]
            newP[:, -1] += (P * pa * (1 - ph))[:, -1]
            P = newP
        return P / P.sum()

    def next_goal(self, lh: float, la: float, st: LiveState) -> dict:
        p = self.params
        w = self._profile(st.added_time)
        start = int(math.floor(st.minute))
        cum = float(w[:start].sum())
        fh = self.xg_factor(lh, st.xg_h, cum)
        fa = self.xg_factor(la, st.xg_a, cum)
        d = st.hg - st.ag
        rh = p.red_self ** st.red_h * p.red_opp ** st.red_a
        ra = p.red_self ** st.red_a * p.red_opp ** st.red_h
        surv, ph_tot, pa_tot = 1.0, 0.0, 0.0
        for m in range(start, T_END):
            frac = 1.0 if m > st.minute else (m + 1 - st.minute)
            if frac <= 0:
                continue
            muh = lh * fh * rh * w[m] * frac * _state_mult(d, p)
            mua = la * fa * ra * w[m] * frac * _state_mult(-d, p)
            tot = muh + mua
            pg = 1 - math.exp(-tot)
            ph_tot += surv * pg * muh / tot
            pa_tot += surv * pg * mua / tot
            surv *= 1 - pg
        return {"H": ph_tot, "A": pa_tot, "NONE": surv}

    def fair_prices(self, lh: float, la: float, st: LiveState) -> dict:
        """Cuotas justas de los mercados principales en este estado."""
        P = self.final_distribution(lh, la, st)
        out: dict = {"state": asdict(st), "lambda": [lh, la]}
        h = float(np.tril(P, -1).sum())
        d = float(np.trace(P))
        a = float(np.triu(P, 1).sum())
        out["1X2"] = {"H": _o(h), "D": _o(d), "A": _o(a), "p": [h, d, a]}
        out["DC"] = {"1X": _o(h + d), "X2": _o(d + a), "12": _o(h + a)}
        ah = {}
        for L in np.arange(-3.0, 3.01, 0.25):
            L = round(float(L), 2)
            for side in ("H", "A"):
                sel = Selection("AH", side, L, base_h=st.hg, base_a=st.ag)
                w_, p_, l_ = selection_probs(sel, P)
                if w_ > 0.01 and l_ > 0.005:
                    ah[f"{side}{L:+g}"] = round(fair_odds(w_, p_, l_), 3)
        out["AH"] = ah
        cur = st.hg + st.ag
        ou = {}
        for L in np.arange(cur + 0.5, cur + 5.01, 0.25):
            L = round(float(L), 2)
            for side in ("O", "U"):
                w_, p_, l_ = selection_probs(Selection("OU", side, L), P)
                if w_ > 0.01 and l_ > 0.005:
                    ou[f"{side}{L:g}"] = round(fair_odds(w_, p_, l_), 3)
        out["OU"] = ou
        btts = float(P[1:, 1:].sum())
        out["BTTS"] = {"Y": _o(btts), "N": _o(1 - btts)}
        ng = self.next_goal(lh, la, st)
        out["NEXT_GOAL"] = {k: _o(v) for k, v in ng.items()}
        cs = sorted(((float(P[i, j]), f"{i}-{j}") for i in range(MAXG) for j in range(MAXG)), reverse=True)[:12]
        out["CS"] = {k: _o(v) for v, k in cs}
        out["matrix"] = P
        return out


def _o(p: float) -> float:
    return round(1.0 / p, 3) if p > 1e-6 else 1e6


# ---------------------------------------------------------------------------
# Calibración por máxima verosimilitud con cronologías reales
# ---------------------------------------------------------------------------
def _expand(matches: list[dict]):
    """Convierte partidos (λ pre-partido + lista de goles/rojas) en arrays por minuto."""
    rows = []  # (lam, minute_idx, diff_cat, red_self, red_opp, goal)
    for m in matches:
        goals = sorted((e["minute"], e["side"]) for e in m["events"] if e["kind"] == "goal")
        reds = sorted((e["minute"], e["side"]) for e in m["events"] if e["kind"] == "red")
        hg = ag = rh = ra = 0
        gi = ri = 0
        for minute in range(1, T_END + 1):
            # eventos que ocurren dentro de este minuto
            gh_now = ga_now = 0
            while gi < len(goals) and min(math.ceil(goals[gi][0]) if goals[gi][0] > 0 else 1, T_END) <= minute:
                if goals[gi][1] == "H":
                    gh_now += 1
                else:
                    ga_now += 1
                gi += 1
            for side, lam, own, opp, gnow, rs, ro in (("H", m["lh"], hg, ag, gh_now, rh, ra), ("A", m["la"], ag, hg, ga_now, ra, rh)):
                d = own - opp
                cat = 0 if d <= -2 else 1 if d == -1 else 2 if d == 0 else 3 if d == 1 else 4
                rows.append((lam, minute - 1, cat, rs, ro, gnow))
            hg += gh_now
            ag += ga_now
            while ri < len(reds) and math.ceil(max(reds[ri][0], 1)) <= minute:
                if reds[ri][1] == "H":
                    rh += 1
                else:
                    ra += 1
                ri += 1
    return np.array(rows, dtype=float)


def calibrate(matches: list[dict], init: InplayParams | None = None) -> tuple[InplayParams, dict]:
    """matches: [{lh, la, events:[{minute, kind:'goal'|'red', side}]}]."""
    if len(matches) < 200:
        return init or InplayParams(), {"n": len(matches), "note": "pocos partidos, se mantienen los parámetros"}
    X = _expand(matches)
    lam, midx, cat, rs, ro, g = (X[:, i] for i in range(6))
    midx = midx.astype(int)
    cat = cat.astype(int)
    p0 = init or InplayParams()
    names = ["slope", "ht_bump", "ft_bump", "trail2", "trail1", "lead1", "lead2", "red_self", "red_opp"]
    x0 = np.log([max(getattr(p0, n), 1e-3) for n in names])

    def nll(x):
        v = dict(zip(names, np.exp(x)))
        p = InplayParams(**{**p0.to_dict(), **v})
        w = time_profile(p)
        cm = np.array([p.trail2, p.trail1, 1.0, p.lead1, p.lead2])
        mu = lam * w[midx] * cm[cat] * (p.red_self ** rs) * (p.red_opp ** ro)
        mu = np.maximum(mu, 1e-12)
        ll = g * np.log(mu) - mu
        reg = 0.5 * np.sum((x - np.log([getattr(InplayParams(), n) for n in names])) ** 2) * 2.0
        return -ll.sum() + reg

    res = minimize(nll, x0, method="L-BFGS-B")
    v = dict(zip(names, np.exp(res.x)))
    p = InplayParams(**{**p0.to_dict(), **v})
    return p, {"n": len(matches), "nll": float(res.fun), "success": bool(res.success)}


def fit_prematch_lambdas(model: "InplayModel", p_home: float, p_draw: float, p_away: float,
                         p_over25: float | None = None) -> tuple[float, float]:
    """λ que hacen que el modelo en vivo, en el minuto 0, reproduzca el mercado."""
    from .poisson import implied_lambdas
    lh0, la0 = implied_lambdas(p_home, p_draw, p_away, p_over25)
    st = LiveState(minute=0.0)

    def loss(x):
        lh, la = np.exp(x)
        P = model.final_distribution(lh, la, st)
        h = np.tril(P, -1).sum(); d = np.trace(P); a = np.triu(P, 1).sum()
        e = (h - p_home) ** 2 + (d - p_draw) ** 2 + (a - p_away) ** 2
        if p_over25 is not None:
            tot = np.add.outer(np.arange(MAXG), np.arange(MAXG))
            e += (P[tot > 2.5].sum() - p_over25) ** 2
        return e
    r = minimize(loss, np.log([lh0, la0]), method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-9, "maxiter": 120})
    lh, la = np.exp(r.x)
    return float(lh), float(la)
