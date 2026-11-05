"""Planes de varias patas (entrar, cubrirse, salir) evaluados sobre miles de partidos simulados.

Cada partido simulado se juega minuto a minuto con el mismo modelo que usa el directo
(goles esperados del pre-partido, perfil temporal, el que va perdiendo aprieta, etc.).
En cada momento en que un plan diría "ahora apuesta X", el precio en vivo se estima con
la cuota justa del modelo en ese marcador y minuto menos un margen típico de casa (≈3,5 %).
Así cada plan sale con: valor esperado, probabilidad de acabar ganando, peor caso y una
tabla de cuánto ganas o pierdes según cómo acabe el partido.

Lo que NO sabe la simulación: la cuota exacta que dará bet365 en ese minuto. Por eso cada
paso en directo lleva una "cuota mínima": si la casa paga menos, ese paso no tiene valor.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .config import settings
from .core import db
from .models.inplay import T_END, InplayModel, InplayParams, LiveState, _state_mult, time_profile
from .pricing import goal_probs

LIVE_MARGIN = 1.035      # la casa paga ≈ justa / 1,035 en directo
N_SIM = 6000


class Sim:
    def __init__(self, lh: float, la: float, n: int = N_SIM, seed: int = 7):
        self.lh, self.la, self.n = lh, la, n
        self.params = InplayParams.from_dict(db.load_params("inplay"))
        self.model = InplayModel(self.params)
        w = time_profile(self.params)
        rng = np.random.default_rng(seed)
        H = np.zeros((n, T_END + 1), np.int16)
        A = np.zeros((n, T_END + 1), np.int16)
        mult = np.array([_state_mult(d, self.params) for d in range(-2, 3)])
        hg = np.zeros(n, np.int16)
        ag = np.zeros(n, np.int16)
        for m in range(T_END):
            d = np.clip(hg - ag, -2, 2)
            ph = 1 - np.exp(-lh * w[m] * mult[d + 2])
            pa = 1 - np.exp(-la * w[m] * mult[-d + 2])
            hg = hg + (rng.random(n) < ph)
            ag = ag + (rng.random(n) < pa)
            H[:, m + 1] = hg
            A[:, m + 1] = ag
        self.H, self.A = H, A
        self.fh, self.fa = H[:, -1].astype(int), A[:, -1].astype(int)
        self._cache: dict = {}

    def dist(self, minute: int, hg: int, ag: int) -> np.ndarray:
        k = (minute // 3 * 3, min(hg, 6), min(ag, 6))
        if k not in self._cache:
            self._cache[k] = self.model.final_distribution(self.lh, self.la, LiveState(float(k[0]), k[1], k[2]))
        return self._cache[k]

    def live_odds(self, minute: int, hg: int, ag: int, market: str, sel: str, line=None, base=None) -> tuple[float, float]:
        """(cuota justa, cuota estimada de la casa) en ese estado."""
        P = self.dist(minute, hg, ag)
        r = goal_probs(market, sel, line, P, base or (hg, ag))
        if not r:
            return math.nan, math.nan
        w, p, l = r
        if w <= 1e-6:
            return math.inf, math.inf
        fair = 1 + l / w
        return fair, max(1.01, 1 + (fair - 1) / LIVE_MARGIN)


def _settle(market: str, sel: str, line, fh: np.ndarray, fa: np.ndarray, base=(0, 0)) -> tuple[np.ndarray, np.ndarray]:
    """Fracción ganada y devuelta por partido simulado (vectorizado)."""
    bh, ba = (np.broadcast_to(np.asarray(base[0]), fh.shape), np.broadcast_to(np.asarray(base[1]), fh.shape))
    if market == "1X2":
        win = {"H": fh > fa, "D": fh == fa, "A": fh < fa}[sel]
        return win.astype(float), np.zeros(len(fh))
    if market == "DC":
        win = {"1X": fh >= fa, "X2": fh <= fa, "12": fh != fa}[sel]
        return win.astype(float), np.zeros(len(fh))
    if market in ("AH", "DNB"):
        d = (fh - bh) - (fa - ba)
        d = d if sel == "H" else -d
        L = 0.0 if market == "DNB" else float(line)
        q = round(L * 4) / 4
        parts = [q - 0.25, q + 0.25] if abs(q * 2 - round(q * 2)) > 1e-9 else [q]
        w = sum((d + p > 1e-9).astype(float) for p in parts) / len(parts)
        pu = sum((np.abs(d + p) <= 1e-9).astype(float) for p in parts) / len(parts)
        return w, pu
    if market == "OU":
        tot = fh + fa
        q = float(line)
        parts = [q - 0.25, q + 0.25] if abs(q * 2 - round(q * 2)) > 1e-9 else [q]
        v = [(tot - p) if sel == "O" else (p - tot) for p in parts]
        return sum((x > 1e-9).astype(float) for x in v) / len(parts), sum((np.abs(x) <= 1e-9).astype(float) for x in v) / len(parts)
    if market == "BTTS":
        y = (fh > 0) & (fa > 0)
        return (y if sel == "Y" else ~y).astype(float), np.zeros(len(fh))
    if market == "CS":
        x, y = (int(v) for v in sel.split("-"))
        return ((fh == x) & (fa == y)).astype(float), np.zeros(len(fh))
    raise ValueError(market)


def _pnl(stake, odds, win, push):
    stake = np.asarray(stake, float)
    return stake * (win * (odds - 1) - (1 - win - push))


def _summary(pnl: np.ndarray, invested: np.ndarray, buckets: list[tuple[str, np.ndarray]]) -> dict:
    inv = float(np.mean(invested)) if np.mean(invested) > 0 else 1.0
    out = {"ev": round(float(pnl.mean()), 2), "ev_pct": round(float(pnl.mean()) / inv, 4),
           "p_profit": round(float((pnl > 0.005).mean()), 3), "p_loss": round(float((pnl < -0.005).mean()), 3),
           "worst": round(float(pnl.min()), 2), "best": round(float(pnl.max()), 2),
           "invested": round(inv, 2)}
    sc = []
    for name, mask in buckets:
        pr = float(mask.mean())
        if pr < 0.002:
            continue
        v = pnl[mask]
        lo, hi = float(np.min(v)), float(np.max(v))
        sc.append({"name": name, "prob": round(pr, 3), "pnl": round(float(v.mean()), 2),
                   "range": None if abs(hi - lo) < 0.01 else [round(lo, 2), round(hi, 2)]})
    out["scenarios"] = sc
    return out


def _verdict(s: dict) -> str:
    if s["ev_pct"] > 0.02:
        return "value"
    if s["ev_pct"] > -0.02:
        return "neutral"
    return "novalue"


def _fmt(o: float) -> str:
    return "–" if o is None or not np.isfinite(o) else f"{o:.2f}".replace(".", ",")


# ---------------------------------------------------------------------------
def build_plans(m: dict, priced: dict, sim: Sim | None = None) -> list[dict]:
    lam = priced.get("lambdas") or {}
    if not lam:
        return []
    sim = sim or Sim(lam["lh"], lam["la"])
    H, A, fh, fa, n = sim.H, sim.A, sim.fh, sim.fa, sim.n
    home, away = m["home_name"], m["away_name"]
    name = {"H": home, "A": away}
    S = max(1.0, round(settings.bankroll * 0.01))
    rows = {(r["market"], r["period"], r["selection"], r["line"]): r for r in priced["rows"]}

    def pre(market, sel, line=None):
        r = rows.get((market, "FT", sel, line))
        if not r:
            return None, None, None
        o = r["b365"] or r["best"]
        return o, ("bet365" if r["b365"] else r["best_book"]), r["fair"]

    res_buckets = [(f"Gana {home}", fh > fa), ("Empate", fh == fa), (f"Gana {away}", fh < fa)]
    plans = []

    # ---------------------------------------------------------- 1. ganador + seguro
    for T in ("H", "A"):
        o1, bk, fair1 = pre("1X2", T)
        if not o1:
            continue
        mine, opp = (H, A) if T == "H" else (A, H)
        led = np.zeros(n, bool)
        eq_min = np.full(n, -1)
        for t in range(1, T_END + 1):
            led |= mine[:, t] > opp[:, t]
            hit = led & (mine[:, t] == opp[:, t]) & (eq_min < 0) & (t <= 88)
            eq_min[hit] = t
        trig = eq_min >= 0
        for variant in ("same", "lock"):
            hs = np.zeros(n)
            ho = np.ones(n)
            w2 = np.zeros(n)
            p2 = np.zeros(n)
            mins = []
            for i in np.where(trig)[0]:
                t = int(eq_min[i])
                g = int(mine[i, t])
                hh, aa = (g, g)
                if variant == "same":
                    _, est = sim.live_odds(t, hh, aa, "DNB", T, 0.0)
                    hs[i] = S / max(est - 1, 0.05)
                else:
                    sel = "X2" if T == "H" else "1X"
                    _, est = sim.live_odds(t, hh, aa, "DC", sel)
                    hs[i] = S * o1 / est
                ho[i] = est
                mins.append(est)
            w1, p1 = _settle("1X2", T, None, fh, fa)
            if variant == "same":
                w2, p2 = _settle("DNB", T, 0.0, fh, fa, base=(fh * 0, fa * 0))   # desde empate: DNB = quién gana el final
            else:
                w2, p2 = _settle("DC", "X2" if T == "H" else "1X", None, fh, fa)
            pnl = _pnl(S, o1, w1, p1) + _pnl(hs, ho, w2, p2) * trig
            buckets = [(f"No le empatan y gana {name[T]}", ~trig & (w1 > 0)), (f"Nunca le empatan y no gana (no llega a ir por delante o pierde)", ~trig & (w1 == 0)),
                       (f"Le empatan y al final gana {name[T]}", trig & (w1 > 0)), ("Le empatan y acaba en empate", trig & (fh == fa)),
                       (f"Le empatan y gana el rival", trig & (w1 == 0) & (fh != fa))]
            s = _summary(pnl, S + hs * trig, buckets)
            avg_est = float(np.median(mins)) if mins else None
            if variant == "same":
                title = f"Gana {name[T]} y, si le empatan, hándicap 0,0 de {name[T]}"
                fam = "Tu estrategia"
                hedge_txt = (f"Apuesta a {name[T]} hándicap asiático 0,0 (si acaba en empate te devuelven). Stake = {S:g} € ÷ (cuota − 1)"
                             f"; a la cuota típica de ese momento ({_fmt(avg_est)}) son ≈ {S / max((avg_est or 2) - 1, 0.05):.0f} €.")
                summary = "Entras a la victoria; si te empatan doblas la apuesta al mismo equipo pero sin riesgo de empate. No es una cobertura: es recuperar si acaba ganando."
            else:
                title = f"Gana {name[T]} con cobertura total si le empatan"
                fam = "Cobertura"
                sel = "empate o " + name["A" if T == "H" else "H"]
                hedge_txt = (f"Apuesta a «{sel}» (doble oportunidad) con stake = {S:g} € × {_fmt(o1)} ÷ cuota. Así ganes o no, acabas igual: "
                             f"bloqueas el resultado. A la cuota típica ({_fmt(avg_est)}) son ≈ {S * o1 / (avg_est or 1.5):.0f} €.")
                summary = "Entras a la victoria; si te empatan te cubres con «el rival no pierde» y aseguras un resultado fijo."
            plans.append({"id": f"win_hedge_{variant}_{T}", "family": fam, "title": title, "summary": summary,
                          "steps": [{"when": "Antes del partido", "do": f"{S:g} € a Gana {name[T]} a {_fmt(o1)} ({bk}). Justa: {_fmt(fair1)}."},
                                    {"when": f"Si {name[T]} se pone por delante y le empatan antes del 88'", "do": hedge_txt},
                                    {"when": "Si no le empatan", "do": "No haces nada."}],
                          "p_trigger": round(float(trig.mean()), 3), **s, "verdict": _verdict(s), "stake": S})

    # ---------------------------------------------------------- 2. ganador a falta de 15'
    lead75 = (np.abs(H[:, 75] - A[:, 75]) == 1)
    if lead75.any():
        idx = np.where(lead75)[0]
        T_arr = np.where(H[:, 75] > A[:, 75], 0, 1)
        o_in = np.ones(n)
        hs = np.zeros(n)
        ho = np.ones(n)
        trig2 = np.zeros(n, bool)
        pnl = np.zeros(n)
        inv = np.zeros(n)
        entry_odds = []
        for i in idx:
            T = "H" if T_arr[i] == 0 else "A"
            hh, aa = int(H[i, 75]), int(A[i, 75])
            _, est = sim.live_odds(75, hh, aa, "1X2", T)
            o_in[i] = est
            entry_odds.append(est)
            mine, opp = (H[i], A[i]) if T == "H" else (A[i], H[i])
            eq = next((t for t in range(76, 89) if mine[t] == opp[t]), None)
            win_final = (fh[i] > fa[i]) if T == "H" else (fa[i] > fh[i])
            draw_final = fh[i] == fa[i]
            p = S * (o_in[i] - 1) if win_final else -S
            inv[i] = S
            if eq is not None:
                g = int(mine[eq])
                _, eo = sim.live_odds(eq, g, g, "DNB", T, 0.0)
                h_st = S / max(eo - 1, 0.05)
                trig2[i] = True
                p += h_st * (eo - 1) if win_final else (0 if draw_final else -h_st)
                inv[i] += h_st
            pnl[i] = p
        not_in = ~lead75
        buckets = [("No hay nadie ganando por 1 al 75' (no se entra)", not_in), ("Entras y aguanta la victoria", lead75 & ~trig2 & (pnl > 0)),
                   ("Entras y le empatan: acaba en empate", lead75 & trig2 & (fh == fa)), ("Entras, le empatan y vuelve a ganar", lead75 & trig2 & (pnl > 0)),
                   ("Entras y pierde (o empatan sin cobertura)", lead75 & (pnl < 0) & ~(trig2 & (fh == fa)))]
        s = _summary(pnl, inv, buckets)
        med = float(np.median(entry_odds))
        plans.append({"id": "live75", "family": "Directo", "title": "Ganador a falta de 15' + hándicap 0,0 si le empatan",
                      "summary": "Solo se activa si al 75' un equipo gana por un gol. Es la estrategia que me describiste, en vivo.",
                      "steps": [{"when": "Minuto 75, alguien gana por 1", "do": f"{S:g} € a que gana el que va por delante. Cuota típica ≈ {_fmt(med)}; entra solo si bet365 paga al menos la justa + 3 % (la ves en Directo)."},
                                {"when": "Si le empatan antes del 88'", "do": f"Hándicap 0,0 del mismo equipo con stake = {S:g} € ÷ (cuota − 1)."},
                                {"when": "Si no", "do": "Nada; cobras o pierdes la primera."}],
                      "p_trigger": round(float(lead75.mean()), 3), **s, "verdict": _verdict(s), "stake": S})

    # ---------------------------------------------------------- 3. empates
    od, bkd, faird = pre("1X2", "D")
    if od:
        w, p_ = _settle("1X2", "D", None, fh, fa)
        pnl = _pnl(S, od, w, p_)
        s = _summary(pnl, np.full(n, S), res_buckets)
        plans.append({"id": "draw_pre", "family": "Empate", "title": "Empate antes del partido",
                      "summary": "La apuesta más simple al empate. Tiene sentido cuando la cuota paga más que la justa.",
                      "steps": [{"when": "Antes del partido", "do": f"{S:g} € a Empate a {_fmt(od)} ({bkd}). Justa: {_fmt(faird)}."}],
                      "p_trigger": 1.0, **s, "verdict": _verdict(s), "stake": S})
        # empate + cubrir con el que marque primero
        first = np.full(n, -1)
        for t in range(1, 81):
            hit = (first < 0) & ((H[:, t] + A[:, t]) > 0)
            first[hit] = t
        trig = first >= 0
        hs = np.zeros(n); ho = np.ones(n); side = np.zeros(n, int)
        for i in np.where(trig)[0]:
            t = int(first[i]); hh, aa = int(H[i, t]), int(A[i, t])
            T = "H" if hh > aa else "A"
            side[i] = 0 if T == "H" else 1
            _, est = sim.live_odds(t, hh, aa, "1X2", T)
            ho[i] = est
            hs[i] = S * od / est
        wl = np.where(side == 0, (fh > fa), (fa > fh)).astype(float)
        pnl = _pnl(S, od, w, p_) + _pnl(hs, ho, wl, np.zeros(n)) * trig
        buckets = [("Nadie marca antes del 80'", ~trig), ("Marca alguien, te cubres y gana ese", trig & (wl > 0)),
                   ("Marca alguien, te cubres y acaba en empate", trig & (fh == fa)), ("Marca alguien y remonta el otro", trig & (wl == 0) & (fh != fa))]
        s = _summary(pnl, S + hs * trig, buckets)
        plans.append({"id": "draw_cover", "family": "Empate", "title": "Empate con cobertura al primer gol",
                      "summary": "Apuestas al empate; si alguien marca antes del 80' apuestas a que gana el que marcó para que, gane él o empaten, quedes parecido.",
                      "steps": [{"when": "Antes del partido", "do": f"{S:g} € a Empate a {_fmt(od)}."},
                                {"when": "Primer gol antes del 80'", "do": f"Apuesta a que gana el que marcó. Stake = {S:g} € × {_fmt(od)} ÷ su cuota en ese momento."},
                                {"when": "Si no hay gol", "do": "Nada: cobras el empate."}],
                      "p_trigger": round(float(trig.mean()), 3), **s, "verdict": _verdict(s), "stake": S})
    # empate al descanso 0-0 → empate en vivo
    ht00 = (H[:, 45] == 0) & (A[:, 45] == 0)
    if ht00.any():
        fair, est = sim.live_odds(46, 0, 0, "1X2", "D")
        w, p_ = _settle("1X2", "D", None, fh, fa)
        pnl = _pnl(S, est, w, p_) * ht00
        s = _summary(pnl, S * ht00, [("No va 0-0 al descanso (no se entra)", ~ht00), ("0-0 al descanso y acaba en empate", ht00 & (fh == fa)),
                                     ("0-0 al descanso y gana alguien", ht00 & (fh != fa))])
        plans.append({"id": "draw_ht00", "family": "Empate", "title": "0-0 al descanso: apostar al empate",
                      "summary": "En los partidos cerrados el empate baja de cuota con el reloj; entrar al descanso aprovecha que aún paga bien.",
                      "steps": [{"when": "Descanso con 0-0", "do": f"{S:g} € a Empate. Justa ≈ {_fmt(fair)}; entra solo si bet365 da ≥ {_fmt(fair * 1.03)}."}],
                      "p_trigger": round(float(ht00.mean()), 3), **s, "verdict": _verdict(s), "stake": S})
    tied70 = H[:, 70] == A[:, 70]
    if tied70.any():
        pnl = np.zeros(n)
        mins = []
        for (hh, aa) in {(int(x), int(y)) for x, y in zip(H[tied70, 70], A[tied70, 70])}:
            msk = tied70 & (H[:, 70] == hh) & (A[:, 70] == aa)
            fair, est = sim.live_odds(70, hh, aa, "1X2", "D")
            mins.append(fair)
            pnl[msk] = np.where(fh[msk] == fa[msk], S * (est - 1), -S)
        s = _summary(pnl, S * tied70, [("No va empatado al 70'", ~tied70), ("Empatado al 70' y acaba en empate", tied70 & (fh == fa)),
                                       ("Empatado al 70' y alguien lo gana", tied70 & (fh != fa))])
        plans.append({"id": "draw_70", "family": "Empate", "title": "Empatados al 70': apostar al empate",
                      "summary": "Si al 70' van empatados, el empate suele estar por encima del 50 %. Entra solo si la cuota supera la justa.",
                      "steps": [{"when": "Minuto 70 con empate", "do": f"{S:g} € a Empate. Justa típica ≈ {_fmt(float(np.median(mins)))} (con 0-0 suele ser algo menor)."}],
                      "p_trigger": round(float(tied70.mean()), 3), **s, "verdict": _verdict(s), "stake": S})

    # ---------------------------------------------------------- 4. goles en vivo
    if ht00.any():
        fair, est = sim.live_odds(46, 0, 0, "OU", "O", 0.5)
        w = ((fh + fa) > 0).astype(float)
        pnl = np.where(ht00, np.where(w > 0, S * (est - 1), -S), 0.0)
        s = _summary(pnl, S * ht00, [("No va 0-0 al descanso", ~ht00), ("0-0 al descanso y hay gol en la 2ª", ht00 & (w > 0)), ("Acaba 0-0", ht00 & (w == 0))])
        plans.append({"id": "over_ht00", "family": "Goles", "title": "0-0 al descanso: habrá gol en la segunda parte",
                      "summary": "La mayoría de partidos 0-0 al descanso tienen algún gol después. Entra si la cuota de «más de 0,5» está por encima de la justa.",
                      "steps": [{"when": "Descanso con 0-0", "do": f"{S:g} € a Más de 0,5 goles. Justa ≈ {_fmt(fair)}."}],
                      "p_trigger": round(float(ht00.mean()), 3), **s, "verdict": _verdict(s), "stake": S})
    low70 = (H[:, 70] + A[:, 70]) <= 1
    if low70.any():
        pnl = np.zeros(n); fairs = []
        for tot in (0, 1):
            for (hh, aa) in {(int(x), int(y)) for x, y in zip(H[low70, 70], A[low70, 70]) if x + y == tot}:
                msk = low70 & (H[:, 70] == hh) & (A[:, 70] == aa)
                fair, est = sim.live_odds(70, hh, aa, "OU", "O", tot + 0.5)
                fairs.append(fair)
                pnl[msk] = np.where(fh[msk] + fa[msk] > tot, S * (est - 1), -S)
        s = _summary(pnl, S * low70, [("Más de 1 gol al 70' (no se entra)", ~low70), ("Entra y llega otro gol", low70 & (fh + fa > H[:, 70] + A[:, 70])),
                                      ("Entra y no se marca más", low70 & (fh + fa == H[:, 70] + A[:, 70]))])
        plans.append({"id": "late_goal", "family": "Goles", "title": "Gol tardío: 0 o 1 goles al 70'",
                      "summary": "Apostar a que cae otro gol en los últimos 20' + añadido, cuando el marcador va corto.",
                      "steps": [{"when": "Minuto 70 con 0 o 1 goles", "do": f"{S:g} € a Más de (goles actuales + 0,5). Justa típica ≈ {_fmt(float(np.median(fairs)))}."}],
                      "p_trigger": round(float(low70.mean()), 3), **s, "verdict": _verdict(s), "stake": S})

    # ---------------------------------------------------------- 5. favorito perdiendo
    fav = "H" if lam["lh"] >= lam["la"] else "A"
    mineF, oppF = (H, A) if fav == "H" else (A, H)
    trail = (oppF[:, 60] - mineF[:, 60]) == 1
    if trail.any():
        pnl = np.zeros(n); fairs = []
        sel = "1X" if fav == "H" else "X2"
        for (hh, aa) in {(int(x), int(y)) for x, y in zip(H[trail, 60], A[trail, 60])}:
            msk = trail & (H[:, 60] == hh) & (A[:, 60] == aa)
            fair, est = sim.live_odds(60, hh, aa, "DC", sel)
            fairs.append(fair)
            ok = (fh[msk] >= fa[msk]) if fav == "H" else (fa[msk] >= fh[msk])
            pnl[msk] = np.where(ok, S * (est - 1), -S)
        ok_all = (fh >= fa) if fav == "H" else (fa >= fh)
        s = _summary(pnl, S * trail, [(f"{name[fav]} no pierde por 1 al 60'", ~trail), (f"{name[fav]} lo empata o lo gana", trail & ok_all),
                                      (f"{name[fav]} acaba perdiendo", trail & ~ok_all)])
        plans.append({"id": "fav_trailing", "family": "Directo", "title": f"{name[fav]} (favorito) pierde por 1 al 60': que no pierda",
                      "summary": "El favorito que va perdiendo aprieta: la doble oportunidad «empata o gana» suele pagar bien en ese momento.",
                      "steps": [{"when": f"Minuto 60, {name[fav]} pierde por 1", "do": f"{S:g} € a «{name[fav]} o empate» (doble oportunidad). Justa típica ≈ {_fmt(float(np.median(fairs)))}."}],
                      "p_trigger": round(float(trail.mean()), 3), **s, "verdict": _verdict(s), "stake": S})

    # ---------------------------------------------------------- 6. dutching de marcadores
    cs = [(r["selection"], r["b365"] or r["best"], r["fair"]) for r in priced["rows"] if r["market"] == "CS" and r["period"] == "FT" and (r["b365"] or r["best"])]
    if len(cs) >= 4:
        P = {}
        for sel, o, fair in cs:
            P[sel] = 1 / fair if fair else 0
        for k in (2, 3, 4):
            top = sorted(cs, key=lambda x: -P[x[0]])[:k]
            inv = sum(1 / o for _, o, _ in top)
            total = S * 2
            st = {sel: total / o / inv for sel, o, _ in top}
            ret = total / inv
            hit = np.zeros(n, bool)
            for sel, _, _ in top:
                x, y = (int(v) for v in sel.split("-"))
                hit |= (fh == x) & (fa == y)
            pnl = np.where(hit, ret - total, -total)
            s = _summary(pnl, np.full(n, total), [("Sale uno de tus marcadores", hit), ("Sale otro marcador", ~hit)])
            plans.append({"id": f"dutch_cs_{k}", "family": "Dutching", "title": f"Dutching de los {k} marcadores más probables",
                          "summary": "Repartes el dinero entre varios resultados exactos para que, salga cual salga de ellos, cobres lo mismo.",
                          "steps": [{"when": "Antes del partido", "do": " · ".join(f"{sel}: {st[sel]:.2f} € a {_fmt(o)}" for sel, o, _ in top) +
                                     f". Si sale cualquiera cobras {ret:.2f} € (inviertes {total:.0f} €)."}],
                          "p_trigger": 1.0, **s, "verdict": _verdict(s), "stake": total})

    # ---------------------------------------------------------- 7. doble oportunidad sintética
    for a, b, dc in (("H", "D", "1X"), ("D", "A", "X2"), ("H", "A", "12")):
        oa, _, _ = pre("1X2", a); ob, _, _ = pre("1X2", b); odc, bk, _ = pre("DC", dc)
        if not (oa and ob):
            continue
        syn = 1 / (1 / oa + 1 / ob)
        if odc and syn > odc * 1.01:
            total = S
            sa, sb = total / oa / (1 / oa + 1 / ob), total / ob / (1 / oa + 1 / ob)
            win = ((_settle("1X2", a, None, fh, fa)[0] + _settle("1X2", b, None, fh, fa)[0]) > 0)
            pnl = np.where(win, total * syn - total, -total)
            s = _summary(pnl, np.full(n, total), res_buckets)
            lbl = {"1X": f"{home} o empate", "X2": f"empate o {away}", "12": "que no haya empate"}[dc]
            plans.append({"id": f"syn_{dc}", "family": "Precio", "title": f"«{lbl}» más barato apostando a las dos por separado",
                          "summary": f"La doble oportunidad paga {_fmt(odc)} pero apostando a los dos resultados por separado sacas {_fmt(syn)}.",
                          "steps": [{"when": "Antes del partido", "do": f"{sa:.2f} € a {a} ({_fmt(oa)}) y {sb:.2f} € a {b} ({_fmt(ob)})."}],
                          "p_trigger": 1.0, **s, "verdict": _verdict(s), "stake": total})

    order = {"value": 0, "neutral": 1, "novalue": 2}
    plans.sort(key=lambda p: (order[p["verdict"]], -p["ev_pct"]))
    return plans


# ---------------------------------------------------------------------------
def combos(priced: dict) -> list[dict]:
    """Combinadas del mismo partido (crear apuesta) con su cuota justa desde la matriz de marcadores."""
    from .models.poisson import score_matrix
    lam = priced.get("lambdas") or {}
    if not lam:
        return []
    m = priced["match"]
    home, away = m["home_name"], m["away_name"]
    P = score_matrix(lam["lh"], lam["la"], lam["rho"])
    n = P.shape[0]
    I = np.arange(n)[:, None] + np.zeros((1, n), int)
    J = np.zeros((n, 1), int) + np.arange(n)[None, :]
    res = {"H": (I > J, f"Gana {home}"), "D": (I == J, "Empate"), "A": (I < J, f"Gana {away}"),
           "1X": (I >= J, f"{home} o empate"), "X2": (I <= J, f"Empate o {away}")}
    goals = {"O15": (I + J > 1.5, "más de 1,5 goles"), "O25": (I + J > 2.5, "más de 2,5 goles"), "U25": (I + J < 2.5, "menos de 2,5 goles"),
             "U35": (I + J < 3.5, "menos de 3,5 goles"), "BY": ((I > 0) & (J > 0), "marcan los dos"), "BN": (~((I > 0) & (J > 0)), "no marcan los dos")}
    out = []
    single = {k: float(P[v[0]].sum()) for k, v in {**res, **goals}.items()}
    for rk, (rm, rl) in res.items():
        for gk, (gm, gl) in goals.items():
            pr = float(P[rm & gm].sum())
            if pr < 0.03 or pr > 0.9:
                continue
            indep = single[rk] * single[gk]
            out.append({"label": f"{rl} y {gl}", "prob": round(pr, 4), "fair": round(1 / pr, 2),
                        "min_odds": round(1.05 / pr, 2), "corr": round(pr / indep - 1, 3) if indep else None})
    for gk1, gk2 in (("BY", "O25"), ("BN", "U25"), ("BY", "U35")):
        pr = float(P[goals[gk1][0] & goals[gk2][0]].sum())
        out.append({"label": f"{goals[gk1][1].capitalize()} y {goals[gk2][1]}", "prob": round(pr, 4), "fair": round(1 / pr, 2),
                    "min_odds": round(1.05 / pr, 2), "corr": round(pr / (single[gk1] * single[gk2]) - 1, 3)})
    out.sort(key=lambda c: -c["prob"])
    return out


# ---------------------------------------------------------------------------
_CAL: dict = {"t": 0.0, "v": {}}
MIN_TIP_ODDS = 1.50
MAX_TIP_ODDS = 8.0
MAX_PLAYER_ODDS = 5.0


def _band(odds) -> str:
    return "corta" if (odds or 0) < 4.0 else "larga"


def calib_factor(group: str | None, odds: float | None = None) -> float:
    """Corrección aprendida de los resultados reales de las recomendaciones de ese tipo de apuesta (1 = sin corrección)."""
    import time as _time
    if _time.time() - _CAL["t"] > 600:
        _CAL["v"] = (db.kv_get("tip_calib") or {}).get("groups", {})
        _CAL["t"] = _time.time()
        rc = db.kv_get("row_calib") or {}
        # con pocos partidos, una goleada mueve a la vez todas las líneas de hándicap de ese partido: no se aplica
        # hasta tener al menos 40 partidos comparados
        _CAL["rows"] = rc.get("groups", {}) if len(rc.get("matches") or []) >= 40 else {}
    # jugadores: lo aprendido se separa por cuota (las largas, tarjetas o goleador a 10+, fallan mucho más que
    # «1 tiro a 1,80»: mezclarlas castigaba todas por igual)
    g = (_CAL["v"].get(f"{group}@{_band(odds)}") if odds else None) or _CAL["v"].get(group or "")
    f = float(g["factor"]) if g else 1.0
    # lo aprendido comparando TODOS los mercados de cada partido (no solo lo apostado) en la zona de 30–85 %:
    # muchos más datos, pero dentro de un partido van relacionados, así que se encoge mucho (200 «filas» neutras)
    r = (_CAL.get("rows") or {}).get(group or "")
    if r and r.get("n_mid", 0) >= 300 and r.get("exp_mid"):
        k = 200.0
        avg = r["exp_mid"] / r["n_mid"]
        f *= min(1.12, max(0.88, (r["won_mid"] + k * avg) / (r["exp_mid"] + k * avg)))
    return min(1.15, max(0.8, f))


def tip_calibration(k: float = 25.0) -> dict:
    """Aprendizaje: compara cuántas recomendaciones de cada tipo esperábamos acertar con cuántas se acertaron de verdad.
    Si un tipo acierta menos de lo previsto, sus probabilidades se rebajan (y salen menos recomendaciones de ese tipo);
    si acierta más, se suben un poco. Con pocas apuestas apenas se corrige (se encoge hacia 1)."""
    import json as _json
    import time as _time
    rows = db.q("SELECT fair_prob, status, notes, odds FROM bets WHERE mode IN ('tip','scan') AND status NOT IN ('open','void') AND fair_prob IS NOT NULL")
    agg: dict = {}
    for r in rows:
        g = (_json.loads(r["notes"] or "{}")).get("group", "?")
        w = {"won": 1.0, "halfwon": 0.5, "halflost": 0.0, "lost": 0.0, "push": None}.get(r["status"])
        if w is None:
            continue
        for key in ([g, f"{g}@{_band(r['odds'])}"] if g == "jugadores" else [g]):
            a = agg.setdefault(key, {"n": 0, "expected": 0.0, "won": 0.0})
            a["n"] += 1
            a["expected"] += float(r["fair_prob"])
            a["won"] += w
    out = {}
    for g, a in agg.items():
        f = (a["won"] + k) / (a["expected"] + k)            # pocas apuestas → casi 1
        # subir probabilidades es peligroso (más «valor» en todas las líneas a la vez): solo con 30+ apuestas y poco
        up = 1.08 if a["n"] >= 30 else 1.0
        out[g] = {**a, "expected": round(a["expected"], 2), "factor": round(min(up, max(0.8, f)), 4)}
    res = {"groups": out, "n": len(rows), "ts": int(_time.time())}
    db.kv_set("tip_calib", res)
    _CAL["t"] = 0
    return res


def value_reason(x: dict) -> str | None:
    """None si se recomienda; si no, por qué no (en palabras): una sola regla para la portada y para «todas las apuestas»."""
    if x.get("ev") is None or not x.get("b365"):
        return "sin cuota de bet365"
    if x.get("warn"):
        return "cuota de bet365 sospechosa (compruébala)"
    if x.get("conf") in ("baja", "sin modelo"):
        return "el modelo y las casas no coinciden: poco fiable"
    # ya no se corrige a todo un tipo de apuesta por igual (eso castigaba a todos los jugadores por las tarjetas a 10):
    # lo aprendido va dentro del modelo, jugador a jugador y equipo a equipo (postmatch.entity_factor)
    f = 1.0
    ev = x["ev"]
    if x["b365"] < MIN_TIP_ODDS:
        return f"cuota menor de {MIN_TIP_ODDS:.2f}".replace(".", ",")
    mx = MAX_PLAYER_ODDS if x.get("group") == "jugadores" else MAX_TIP_ODDS
    if x["b365"] > mx:
        return f"cuota mayor de {mx:.0f}: el modelo falla más de lo que gana"
    need = 0.02
    if x.get("p_market") is None:
        need = 0.06
    if x.get("group") == "jugadores":
        need = 0.10 if (x.get("n_books") or 0) >= 2 else 0.12     # solo bet365: sin contraste, más margen
    if x["b365"] > 15:
        need = max(need, 0.12)
    if ev <= need:
        if f < 0.999 and x["ev"] > need:
            return f"con lo aprendido de los resultados ({'-' if f < 1 else '+'}{abs(round((f - 1) * 100))} %) ya no llega"
        return f"valor demasiado justo (pide +{round(need * 100)} %)"
    return None


def is_value(x: dict) -> bool:
    """¿Se recomienda? Más exigencia cuanto menos fiable es el precio, y corregido con lo aprendido de los resultados reales."""
    return value_reason(x) is None


_VAL_KEYS = ("market", "period", "selection", "line", "label", "group", "p", "fair", "b365", "best", "best_book", "ev", "stake",
             "conf", "explain", "p_market", "p_model", "player", "n_books", "kf", "meta", "team", "warn", "push")


# ---------------------------------------------------------------------------
# Créditos de apuesta de bet365: si aciertas cobras solo la ganancia (no te devuelven el crédito). Lo que vale un
# crédito en una apuesta es p × (cuota − 1): por eso convienen cuotas algo más altas que con dinero real (2,5–6),
# siempre que la probabilidad sea fiable.
CREDIT_MIN, CREDIT_MAX, CREDIT_MAX_PLAYER = 2.0, 7.0, 4.5


def _credit_why(x: dict, p_eff: float, conv: float) -> list[str]:
    o = x["b365"]
    out = [f"Cada 10 € de crédito valen aquí unos {conv * 10:.2f} € de dinero real: sale {round(p_eff * 100)} de cada 100 veces y, "
           f"como el crédito no vuelve, cobras {o - 1:.2f} por euro".replace(".", ",")]
    pm = x.get("p_market")
    if pm and x.get("n_books", 0) >= 3:
        out.append(f"Las demás casas, sin su margen, la ven al {round(pm * 100)} %: es una probabilidad contrastada, no solo del modelo")
    if x.get("ev") is not None and x["ev"] > 0.02:
        out.append(f"Además bet365 la paga por encima de lo justo (+{round(x['ev'] * 100)} % de valor)")
    if 2.5 <= o <= 6:
        out.append("Cuota en la franja donde mejor rinden los créditos (2,5–6): ni tan baja que no cobres casi nada, ni tan alta que casi nunca salga")
    return out


def credit_candidates(rows: list[dict], n: int = 6) -> list[dict]:
    out = []
    for x in rows:
        o, p = x.get("b365"), x.get("p")
        if not o or not p or x.get("warn") or x.get("conf") in ("baja", "sin modelo") or (x.get("period") or "FT") != "FT":
            continue
        if x.get("group") in ("exacto", "especiales") or (x.get("push") or 0) > 0.02:
            continue
        if not (CREDIT_MIN <= o <= (CREDIT_MAX_PLAYER if x.get("group") == "jugadores" else CREDIT_MAX)):
            continue
        # probabilidad prudente: con créditos se va a cuotas algo más largas, donde el modelo se equivoca más. Si las
        # casas (sin margen) dan menos que el modelo, se usa lo que dicen las casas; si dan más, el modelo un poco rebajado
        pm = x.get("p_market")
        p_eff = min(p, pm) if pm else p * 0.92
        conv = p_eff * (o - 1)                  # euros de dinero real que vale cada euro de crédito
        out.append({**{k: x.get(k) for k in _VAL_KEYS}, "conv": round(conv, 4), "p_eff": round(p_eff, 4),
                    "why": _credit_why(x, p_eff, conv)})
    out.sort(key=lambda c: -c["conv"])
    return out[:n]


def credit_plan(amount: float, hours: float = 48) -> dict:
    """Dónde meter los créditos: la mejor apuesta sola y, si compensa, repartidos en 2–3 partidos distintos."""
    import time as _time
    now = int(_time.time())
    vd = (db.kv_get("verdicts") or {}).get("v", {})
    cands = []
    for r in db.q("SELECT id, comp, kickoff, home_name, away_name FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? "
                  "ORDER BY kickoff", (now + 900, now + int(hours * 3600))):
        c = db.kv_get(f"analysis:{r['id']}") or {}
        for x in c.get("credit") or []:
            d = vd.get(vkey(r["id"], x["market"], x.get("period"), x["selection"], x.get("line")))
            if d and d.get("tone") == "bad":
                continue                         # el análisis completo la tumba
            cands.append({**x, "match": dict(r), "dossier": d})
    cands.sort(key=lambda c: -c["conv"])
    if not cands:
        return {"amount": amount, "single": None, "split": None, "cands": []}
    best = cands[0]
    single = {"bets": [{**best, "stake": round(amount, 2)}], "exp": round(amount * best["conv"], 2), "p_any": round(best.get("p_eff", best["p"]), 3)}
    picks, seen = [], set()
    for c in cands:
        if c["match"]["id"] in seen or c["conv"] < best["conv"] * 0.85:
            continue
        seen.add(c["match"]["id"])
        picks.append(c)
        if len(picks) == 3:
            break
    split = None
    if len(picks) >= 2 and amount >= 4:
        each = round(amount / len(picks), 2)
        p_none = 1.0
        for c in picks:
            p_none *= 1 - c.get("p_eff", c["p"])
        split = {"bets": [{**c, "stake": each} for c in picks], "exp": round(sum(each * c["conv"] for c in picks), 2),
                 "p_any": round(1 - p_none, 3)}
    # recomendación: repartir si casi no se pierde valor esperado y sube mucho la probabilidad de cobrar algo
    rec = "split" if split and split["exp"] >= single["exp"] * 0.93 and split["p_any"] >= single["p_any"] + 0.15 else "single"
    return {"amount": amount, "single": single, "split": split, "rec": rec, "cands": cands[:10]}


def analyze(mid: int, use_cache: bool = True) -> dict:
    """Todo lo de un partido: mercados con precio, planes, combinadas y surebets (con caché por versión de cuotas)."""
    import time as _time
    from . import pricing
    pr = pricing.price_match(mid)
    if not pr:
        return {}
    m = pr["match"]
    pred = db.q1("SELECT created FROM predictions WHERE match_id=? AND model='prematch'", (mid,))
    ver = f"{pr.get('odds_ts')}:{(pred or {}).get('created')}:{_time.strftime('%Y-%m-%d')}:v24"
    key = f"analysis:{mid}"
    if use_cache:
        c = db.kv_get(key)
        if c and c.get("ver") == ver:
            return {**pr, "plans": c["plans"], "combos": c["combos"]}
    pl = build_plans(m, pr) if m["status"] == "notstarted" else []
    cb = combos(pr)
    vals = [{k: x.get(k) for k in _VAL_KEYS} for x in pr["rows"] if is_value(x)]
    cred = credit_candidates(pr["rows"])
    db.kv_set(key, {"ver": ver, "plans": pl, "combos": cb, "values": vals, "surebets": pr["surebets"], "credit": cred,
                    "n_rows": len(pr["rows"]), "ts": int(_time.time())})
    return {**pr, "plans": pl, "combos": cb}


def vkey(mid, market, period, selection, line) -> str:
    return f"{mid}|{market}|{period or 'FT'}|{selection}|{'' if line is None else round(float(line), 2)}"


def refresh_verdicts(hours: float = 48, n: int = 30) -> int:
    """El análisis completo (señales, veredicto) de las mejores apuestas: así la portada enseña lo mismo que la ficha."""
    from .insights import bet_insight
    o = opportunities(hours)
    cand, per = [], {}
    for v in o["values"]:
        mid = v["match"]["id"]
        if per.get(mid, 0) >= 3:
            continue
        per[mid] = per.get(mid, 0) + 1
        cand.append(v)
        if len(cand) >= n:
            break
    out = {}
    skip = {"precio", "consenso", "simulacion", "racha10"}
    for v in cand:
        try:
            f = bet_insight(v["match"]["id"], v["market"], v.get("period") or "FT", v["selection"], v.get("line"))
        except Exception:
            f = None
        if not f:
            continue
        try:      # el análisis entero queda guardado: abrir «por qué apostar» es instantáneo aunque el servidor sea lento
            import time as _tt
            db.kv_set("betins:" + vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line")),
                      {"ts": int(_tt.time()), "r": f})
        except Exception:
            pass
        pro = sorted([x for x in f["signals"] if x["dir"] > 0 and x["key"] not in skip], key=lambda x: -x["w"])
        con = sorted([x for x in f["signals"] if x["dir"] < 0], key=lambda x: -x["w"])
        out[vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line"))] = {
            "verdict": f["verdict"], "tone": f["tone"], "score": f["score"], "n_pro": f["n_pro"], "n_con": f["n_con"],
            "pro": [x["text"] for x in pro[:3]], "con": [x["text"] for x in con[:2]]}
    import time as _t
    db.kv_set("verdicts", {"ts": int(_t.time()), "v": out})
    return len(out)


def opportunities(hours: float = 72, limit: int = 150) -> dict:
    """Lo mejor de todos los próximos partidos: apuestas con valor, planes con valor, surebets y combinada del día.
    Lee lo que el planificador ya calculó (analysis:<id>); solo calcula al momento lo que falte."""
    import time as _time
    now = int(_time.time())
    ms = db.q("SELECT id, comp, kickoff, home_name, away_name FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? "
              "ORDER BY kickoff LIMIT ?", (now, now + int(hours * 3600), limit))
    values, plans, sures, matches = [], [], [], []
    n_rows = 0
    pending = 0
    for r in ms:
        c = db.kv_get(f"analysis:{r['id']}")
        if not c or "values" not in c:
            pending += 1          # lo calcula el planificador en segundo plano; la web no se bloquea esperando
            continue
        if not str(c.get("ver", "")).endswith(":v24"):
            pending += 1          # versión anterior: se enseña mientras se recalcula
            for v in c.get("values", []):
                if "kf" not in v and v.get("b365") and v["b365"] > 1:
                    k = max(0.0, v.get("ev", 0) / (v["b365"] - 1))
                    v["kf"] = round(min(k * 0.125, 0.01) if v.get("group") == "jugadores" else min(k * 0.25, 0.05), 4)
        mi = dict(r)
        matches.append(mi)
        n_rows += c.get("n_rows", 0)
        out_ids = {p["id"] for p in ((db.kv_get(f"missing:{r['id']}") or {}).get("players") or []) if p.get("status") == "baja"}
        out_names = {p["name"] for p in ((db.kv_get(f"missing:{r['id']}") or {}).get("players") or []) if p.get("status") == "baja"}
        def plays(v):
            if v.get("group") != "jugadores":
                return True
            pid = (v.get("meta") or {}).get("pid")
            return not ((pid and int(pid) in out_ids) or (v.get("player") in out_names))
        values += [{**v, "match": mi} for v in c.get("values", []) if (v.get("b365") or 0) >= MIN_TIP_ODDS and not v.get("warn") and plays(v)]
        if str(c.get("ver", "")).endswith(":v24"):
            sures += [{**s, "match": mi} for s in c.get("surebets", [])]
        plans += [{**p, "match": mi} for p in c.get("plans", []) if p["verdict"] == "value"]
    # valor falso por noticias: si bet365 ha ALARGADO mucho la cuota en las últimas horas (señal de que ha pasado algo:
    # una baja, una rotación) y nuestro modelo es de antes, esa «ventaja» casi seguro es que aún no lo sabemos
    for mid_ in {v["match"]["id"] for v in values}:
        try:
            dr = b365_drift(mid_)
        except Exception:
            dr = {}
        if not dr:
            continue
        pc = db.q1("SELECT created FROM predictions WHERE match_id=? AND model='prematch'", (mid_,))
        stale = not pc or pc["created"] < _time.time() - 3 * 3600
        ab = db.kv_get(f"absr:{mid_}") or {}
        fresh_ab = ab and _time.time() - ab.get("ts", 0) < 1800
        for v in values:
            if v["match"]["id"] != mid_:
                continue
            k = (v["market"], v.get("period") or "FT", v["selection"], None if v.get("line") is None else round(float(v["line"]), 2))
            r = dr.get(k)
            if r and r >= 1.12 and stale and not fresh_ab:
                v["conf"] = "baja"
                v["move_warn"] = (f"bet365 ha pasado de {v['b365'] / r:.2f} a {v['b365']:.2f} en pocas horas: puede haber una baja o "
                                  f"noticia que el modelo aún no tiene. Mejor esperar al once.")
    vd = (db.kv_get("verdicts") or {}).get("v", {})
    for v in values:
        d = vd.get(vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line")))
        if d:
            v["dossier"] = d
    # orden: lo que ganas por euro arriesgado (Kelly) pesa más que el valor bruto, para no subir cuotas de 50 a lo más alto
    values.sort(key=lambda v: -((v["ev"] / max(v["b365"] - 1, 0.2)) * (1.0 if v["conf"] == "alta" else 0.75)
                                * (0.8 if v["group"] == "jugadores" else 1.0)))
    plans.sort(key=lambda p: -p["ev_pct"])
    seen_sb, uniq = set(), []          # hándicap 0 y «empate devuelve» son la misma apuesta: una sola vez
    for sb in sorted(sures, key=lambda x: -x["profit_pct"]):
        k = (sb["match"]["id"], tuple(sorted((lg["book"], lg["odds"]) for lg in sb["legs"])))
        if k not in seen_sb:
            seen_sb.add(k)
            uniq.append(sb)
    sures = uniq
    # combinada del día: mejores apuestas de partidos distintos, mercados principales, cuota 1,5-3,5
    acc, used = [], set()
    for v in values:
        if v["match"]["id"] in used or v["group"] not in ("resultado", "goles", "handicap") or not (1.45 <= (v["b365"] or 0) <= 3.6):
            continue
        acc.append(v)
        used.add(v["match"]["id"])
        if len(acc) == 3:
            break
    accum = None
    if len(acc) >= 2:
        po = float(np.prod([a["b365"] for a in acc]))
        pp = float(np.prod([a["p"] for a in acc]))
        ev = float(np.prod([1 + a["ev"] for a in acc])) - 1      # partidos distintos: el valor se multiplica (con devoluciones incluidas)
        accum = {"legs": acc, "odds": round(po, 2), "prob": round(pp, 4), "ev": round(ev, 4)}
    return {"values": values[:300], "plans": plans[:60], "surebets": sures[:40], "accumulator": accum,
            "n_matches": len(matches), "n_rows": n_rows, "pending": pending, "ts": now}



# ---------------------------------------------------------------------------
# Historial de recomendaciones: cada recomendación queda apuntada (apuesta plana de 10 €)
# y se liquida sola. Es la prueba de si la web gana o no.
# ---------------------------------------------------------------------------
TIP_STAKE = 10.0


def record_tips(mid: int) -> int:
    import json as _json
    import time as _time
    c = db.kv_get(f"analysis:{mid}") or {}
    m = db.q1("SELECT kickoff, status FROM matches WHERE id=?", (mid,))
    if not m or m["status"] != "notstarted" or m["kickoff"] < _time.time() + 600:
        return 0
    n = 0
    for v in c.get("values", []):
        if v.get("conf") not in ("media", "alta") or not v.get("b365"):
            continue
        ex = db.q1("SELECT id FROM bets WHERE mode='scan' AND match_id=? AND market=? AND selection=? AND COALESCE(line,-999)=COALESCE(?,-999) "
                   "AND COALESCE(period,'FT')=?", (mid, v["market"], v["selection"], v["line"], v.get("period") or "FT"))
        if ex:
            continue
        db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,odds,odds_source,fair_prob,edge,"
                   "stake,status,notes) VALUES(NULL,NULL,?,?,'scan',?,?,?,?,?,'bet365',?,?,?,'open',?)",
                   (mid, int(_time.time()), v["market"], v["selection"], v["line"], v.get("period") or "FT", v["b365"], v.get("p"),
                    v.get("ev"), TIP_STAKE, _json.dumps({"label": v["label"], "group": v["group"], "conf": v["conf"], "kf": v.get("kf")})))
        n += 1
    return n


def record_opening(mid: int, vals: list[dict]) -> int:
    """Apuestas con valor cazadas nada más abrir su mercado, a la cuota de ese momento (10 € planos). Se resuelven
    solas y se comparan con la cuota de cierre: así se sabe si entrar en las aperturas sale rentable de verdad."""
    import json as _json
    import time as _time
    n = 0
    for v in vals:
        if not v.get("b365"):
            continue
        ex = db.q1("SELECT id FROM bets WHERE mode='opening' AND match_id=? AND market=? AND selection=? AND COALESCE(line,-999)=COALESCE(?,-999) "
                   "AND COALESCE(period,'FT')=?", (mid, v["market"], v["selection"], v.get("line"), v.get("period") or "FT"))
        if ex:
            continue
        db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,odds,odds_source,fair_prob,edge,"
                   "stake,status,notes) VALUES(NULL,NULL,?,?,'opening',?,?,?,?,?,'bet365',?,?,?,'open',?)",
                   (mid, int(_time.time()), v["market"], v["selection"], v.get("line"), v.get("period") or "FT", v["b365"], v.get("p"),
                    v.get("ev"), TIP_STAKE, _json.dumps({"label": v.get("label"), "group": v.get("group"), "conf": v.get("conf"), "opening": True})))
        n += 1
    return n


def pick_score(v: dict) -> float:
    """Lo favorable que es una apuesta mirando todo: valor por euro arriesgado, probabilidad, fiabilidad del precio y
    lo que dice el análisis completo (señales a favor y en contra)."""
    d = v.get("dossier") or {}
    if d.get("tone") == "bad":
        return -1.0
    kelly = max(0.0, v["ev"]) / max((v["b365"] or 2) - 1, 0.2)
    sc = d.get("score", 0.6)
    conf = 1.0 if v.get("conf") == "alta" else 0.85
    tone = 1.0 if d.get("tone") == "good" else 0.7 if d.get("tone") == "warn" else 0.85
    return kelly * (0.4 + sc) * conf * tone * (0.6 + min(v["p"], 0.8) / 2)


def _family(v: dict) -> str:
    """Apuestas que en el fondo son la misma (misma dirección del mismo mercado): «Más de 2,5» y «Más de 3,5», o dos de
    tiros del mismo jugador. Solo se recomienda la mejor de cada familia."""
    if v.get("group") == "jugadores" or v.get("player"):
        return f"pl:{(v.get('player') or v.get('label', '')).lower()}"
    sel = str(v.get("selection") or "")
    d = "O" if sel.startswith(("O", "Over")) else "U" if sel.startswith(("U", "Under")) else sel
    return f"{v.get('market')}|{v.get('period') or 'FT'}|{d}"


def official_picks(hours: float = 36, n: int = 10) -> list[dict]:
    """Lo mismo que enseña «Qué apostar hoy» día a día (misma regla que la web):
    - fuera las que el análisis completo desaconseja; primero las que respalda y las de confianza alta;
    - tus ajustes (confianza mínima, ver jugadores);
    - como mucho 3 por partido y nada repetido de la misma familia (ni dos del mismo jugador);
    - como mucho 3 de jugador al día;
    - por día, hasta que lo recomendado sume el 15 % de tu banca (y nunca más de 10)."""
    import time as _time
    from .followup import page_stake
    o = opportunities(hours)
    u = db.kv_get("user_settings") or {}
    min_conf, show_pl = u.get("min_conf", "media"), u.get("show_players", True)
    bank = float(u.get("bankroll") or settings.bankroll or 0) * float(u.get("risk") or 1)
    ok_conf = ("alta",) if min_conf == "alta" else ("alta", "media")
    vs = [v for v in o["values"] if v.get("conf") in ok_conf and (show_pl or v.get("group") != "jugadores")
          and ((v.get("dossier") or {}).get("tone") != "bad")]
    def rank(v):
        t = (v.get("dossier") or {}).get("tone")
        return (0 if t == "good" else 2 if t == "warn" else 1) + (0 if v.get("conf") == "alta" else 0.5)
    vs = sorted(vs, key=rank)              # estable: dentro de cada grupo, el orden de la web
    out, per_day, per_match, fams = [], {}, {}, set()
    for v in vs:
        mid = v["match"]["id"]
        fam = (mid, _family(v))
        if per_match.get(mid, 0) >= 3 or fam in fams:
            continue
        day = _time.strftime("%Y-%m-%d", _time.localtime(v["match"]["kickoff"]))
        d = per_day.setdefault(day, {"n": 0, "pl": 0, "eur": 0.0})
        st = page_stake(v.get("p"), v.get("b365"), v.get("group") == "jugadores", v.get("kf"))
        if d["n"] >= n or (v.get("group") == "jugadores" and d["pl"] >= 3) or (bank > 0 and d["n"] and d["eur"] + st > 0.15 * bank):
            continue
        out.append(v)
        fams.add(fam)
        per_match[mid] = per_match.get(mid, 0) + 1
        d["n"] += 1
        d["pl"] += v.get("group") == "jugadores"
        d["eur"] += st
    out.sort(key=lambda v: -pick_score(v))
    return out


def record_official(hours: float = 36) -> int:
    """Apunta las recomendaciones oficiales (10 € fijos, cuota del momento) para medir si la web acierta."""
    import json as _json
    import time as _time
    n = 0
    for v in official_picks(hours):
        if v["match"]["kickoff"] < _time.time() + 600:
            continue
        ex = db.q1("SELECT id FROM bets WHERE mode='tip' AND match_id=? AND market=? AND selection=? AND COALESCE(line,-999)=COALESCE(?,-999) "
                   "AND COALESCE(period,'FT')=?", (v["match"]["id"], v["market"], v["selection"], v.get("line"), v.get("period") or "FT"))
        if ex:
            continue
        db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,odds,odds_source,fair_prob,edge,"
                   "stake,status,notes) VALUES(NULL,NULL,?,?,'tip',?,?,?,?,?,'bet365',?,?,?,'open',?)",
                   (v["match"]["id"], int(_time.time()), v["market"], v["selection"], v.get("line"), v.get("period") or "FT", v["b365"],
                    v.get("p"), v.get("ev"), TIP_STAKE, _json.dumps({"label": v["label"], "group": v["group"], "conf": v["conf"],
                                                                     "kf": v.get("kf"), "verdict": (v.get("dossier") or {}).get("verdict")})))
        n += 1
    return n


def migrate_tips() -> None:
    """Una vez: lo que se apuntaba antes como «recomendación» eran todas las apuestas con valor → pasan al escáner."""
    if db.kv_get("tips_migrated"):
        return
    db.execute("UPDATE bets SET mode='scan' WHERE mode='tip'")
    db.kv_set("tips_migrated", 1)


def settle_raw_bets() -> int:
    """Liquida apuestas de mercados de jugador, estadísticas y especiales con lo que pasó de verdad."""
    import time as _time
    from .core.markets import status_from_fracs
    from .markets_all import settle_raw
    rows = db.q("SELECT b.*, m.id AS mid FROM bets b JOIN matches m ON m.id=b.match_id WHERE b.status='open' AND b.market LIKE 'RAW:%' "
                "AND m.status='finished' AND m.hg IS NOT NULL")
    n = 0
    cache = {}
    for b in rows:
        m = cache.get(b["mid"]) or db.q1("SELECT * FROM matches WHERE id=?", (b["mid"],))
        cache[b["mid"]] = m
        try:
            r = settle_raw(m, b["market"], b["period"], b["selection"], b["line"])
        except Exception:
            r = None
        if r is None:
            if m["kickoff"] < _time.time() - 7 * 86400:      # una semana sin datos para liquidar: se anula
                db.execute("UPDATE bets SET status='void', pnl=0, settled=? WHERE id=?", (int(_time.time()), b["id"]))
            continue
        w, p = r
        pnl = b["stake"] * (w * (b["odds"] - 1) - (1 - w - p))
        db.execute("UPDATE bets SET status=?, pnl=?, settled=? WHERE id=?", (status_from_fracs(w, p), round(pnl, 2), int(_time.time()), b["id"]))
        n += 1
    return n


def tips_clv() -> int:
    """CLV de las recomendaciones: cuota cogida frente a la última de bet365 antes del partido."""
    rows = db.q("SELECT b.id, b.odds, b.match_id, b.market, b.selection, b.line, b.period FROM bets b JOIN matches m ON m.id=b.match_id "
                "WHERE b.mode IN ('tip','scan','opening') AND b.clv IS NULL AND m.status!='notstarted'")
    n = 0
    for b in rows:
        r = db.q1("SELECT odds FROM odds_now WHERE match_id=? AND bookmaker='Bet365' AND market=? AND selection=? AND line_k=? AND period=?",
                  (b["match_id"], b["market"], b["selection"], -999.0 if b["line"] is None else round(float(b["line"]), 2), b["period"] or "FT"))
        if r and r["odds"] > 1:
            db.execute("UPDATE bets SET clv=? WHERE id=?", (round(b["odds"] / r["odds"] - 1, 4), b["id"]))
            n += 1
    return n


def track_record(days: int = 90, mode: str = "tip") -> dict:
    import json as _json
    import time as _time
    since = int(_time.time()) - days * 86400
    rows = db.q("SELECT b.*, m.home_name, m.away_name, m.kickoff, m.comp FROM bets b JOIN matches m ON m.id=b.match_id "
                "WHERE b.mode=? AND b.created>=? ORDER BY m.kickoff", (mode, since))
    done = [r for r in rows if r["status"] not in ("open", "void")]
    def summ(rs):
        st = sum(r["stake"] for r in rs)
        pnl = sum(r["pnl"] or 0 for r in rs)
        cl = [r["clv"] for r in rs if r["clv"] is not None]
        return {"n": len(rs), "won": sum(1 for r in rs if r["status"] in ("won", "halfwon")), "profit": round(pnl, 2),
                "yield": round(pnl / st, 4) if st else None, "avg_odds": round(sum(r["odds"] for r in rs) / len(rs), 2) if rs else None,
                "clv": round(sum(cl) / len(cl), 4) if cl else None, "beat_close": round(sum(1 for x in cl if x > 0) / len(cl), 3) if cl else None}
    by_group, by_conf, by_band = {}, {}, {}
    for r in done:
        n = _json.loads(r["notes"] or "{}")
        by_group.setdefault(n.get("group", "?"), []).append(r)
        by_conf.setdefault(n.get("conf", "?"), []).append(r)
        o = r["odds"]
        band = "1,0-1,6" if o < 1.6 else "1,6-2,2" if o < 2.2 else "2,2-3,5" if o < 3.5 else "3,5-8" if o < 8 else "8+"
        by_band.setdefault(band, []).append(r)
    curve, acc = [], 0.0
    for r in sorted(done, key=lambda r: r["settled"] or 0):
        acc += r["pnl"] or 0
        curve.append([r["settled"], round(acc, 2)])
    recent = []
    for r in sorted(rows, key=lambda r: -(r["kickoff"] or 0))[:60]:
        n = _json.loads(r["notes"] or "{}")
        recent.append({"label": n.get("label"), "group": n.get("group"), "match": f"{r['home_name']} – {r['away_name']}", "kickoff": r["kickoff"],
                       "match_id": r["match_id"], "odds": r["odds"], "status": r["status"], "pnl": r["pnl"], "clv": r["clv"], "ev": r["edge"]})
    return {"all": summ(done), "open": sum(1 for r in rows if r["status"] == "open"), "learning": db.kv_get("tip_calib") or {},
            "by_group": {k: summ(v) for k, v in by_group.items()}, "by_conf": {k: summ(v) for k, v in by_conf.items()},
            "by_band": {k: summ(v) for k, v in by_band.items()}, "curve": curve, "recent": recent, "since": since}


# ---------------------------------------------------------------------------
# Escalera: tres «Crear apuesta» del mismo partido que van subiendo de dificultad y bajando de stake (1 · ½ · ¼).
#   peldaño 1: casi seguro (≈ 85–92 %): dos o tres cosas muy probables, de partido, de equipo o de jugador
#              (más de 0,5 goles + más de 5,5 córners + 1 entrada de X…)
#   peldaño 2: muy probable (≈ 65–78 %): se sube alguna de esas a la línea siguiente y/o se añade otra probable
#   peldaño 3: algo menos (≈ 45–58 %), sin complicarse: otro escalón más
# Las probabilidades salen del modelo (combinado con las casas). bet365 rebaja la cuota de las combinadas del mismo
# partido, así que se da la cuota mínima a la que merece la pena cada peldaño.
LADDER_STAKES = (1.0, 0.5, 0.25)
LADDER_ODDS = ((1.70, 2.10), (4.0, 6.0), (12.0, 30.0))      # cuota de cada peldaño: base ≥1,70 lo más segura posible, luego 4–6 y 12–30
LADDER_MAX_LEGS = (4, 6, 9)
BUILDER_DISCOUNT = 0.90     # bet365 rebaja la cuota de la combinada del mismo partido frente al producto
_FAM_CORE = {"OU": "goles", "CORNERS_OU": "corners", "CARDS_OU": "tarjetas", "SHOTS_OU": "tiros", "SOT_OU": "tiros_puerta",
             "BTTS": "ambos", "DC": "resultado"}
_PLAYER_KINDS = ("shots", "sot", "fouls", "fouled", "tackles")


def _leg_of(r: dict) -> dict | None:
    """Fila de precios → pata de «crear apuesta» (familia para no repetir, dirección y dificultad)."""
    if (r.get("period") or "FT") != "FT" or not r.get("p") or not r.get("b365") or r.get("warn") or r["b365"] <= 1.0:
        return None
    mk, sel, line = r["market"], str(r["selection"]), r.get("line")
    if line is not None and abs(float(line) * 2 - round(float(line) * 2)) > 1e-6:
        return None                                    # líneas asiáticas (,25 / ,75): no están en «crear apuesta»
    mt = r.get("meta") or {}
    fam = dirn = None
    if mk in _FAM_CORE:
        fam = _FAM_CORE[mk]
        if mk == "DC":
            dirn = sel
        elif mk == "BTTS":
            dirn = sel
        else:
            dirn = "O" if sel.startswith("O") else "U"
    elif mk == "TEAM_OU":
        fam, dirn = f"goles_{sel[0]}", sel[1:]
    elif mk.startswith("RAW:") and mt.get("t") == "player" and mt.get("kind") in _PLAYER_KINDS and mt.get("pid") and not mt.get("extra"):
        fam, dirn = f"p{mt['pid']}", f"{mt['kind']}:{'O' if mt.get('over') else 'U'}"
    elif mk.startswith("RAW:") and mt.get("t") == "count" and mt.get("code") in ("CORNERS", "CARDS", "SHOTS", "SOT", "FOULS"):
        fam = {"CORNERS": "corners", "CARDS": "tarjetas", "SHOTS": "tiros", "SOT": "tiros_puerta", "FOULS": "faltas"}[mt["code"]]
        fam += f"_{mt['side']}" if mt.get("side") else ""
        dirn = "O" if mt.get("over") else "U"
    if not fam:
        return None
    return {"label": r["label"], "p": float(r["p"]), "b365": float(r["b365"]), "line": line, "fam": fam, "dir": dirn,
            "group": r.get("group"), "player": r.get("player"), "pid": mt.get("pid"), "kind": mt.get("kind"), "mu": mt.get("mu"), "p_sub": mt.get("p_sub"),
            "market": mk, "selection": sel, "src": r.get("b365_src")}


def _ladder_legs(mid: int) -> dict:
    from . import pricing
    pr = pricing.price_match(mid)
    if not pr:
        return {}
    missing = {p["id"] for p in ((db.kv_get(f"missing:{mid}") or {}).get("players") or []) if p.get("status") == "baja"}
    legs, seen = [], set()
    for r in pr["rows"]:
        lg = _leg_of(r)
        if not lg or (lg["pid"] and int(lg["pid"]) in missing):
            continue
        k = (lg["fam"], lg["dir"], lg["line"])
        if k in seen:                                   # mismo mercado por dos vías (CORNERS_OU y Total Corners): una vez
            continue
        seen.add(k)
        legs.append(lg)
    return {"match": pr["match"], "legs": legs}


def _combo(legs: list[dict]) -> dict:
    p = 1.0
    o = 1.0
    for lg in legs:
        p *= lg["p"]
        o *= lg["b365"]
    o_est = 1 + (o - 1) * BUILDER_DISCOUNT
    ps = 1.0
    for lg in legs:
        ps *= lg.get("p_sub") or lg["p"]           # con Sustituto+ (si te lo ofrece bet365 en esas patas)
    return {"legs": legs, "p": round(p, 4), "p_sub": round(ps, 4), "odds_product": round(o, 2), "odds_est": round(o_est, 2),
            "min_odds": round(1.03 / p, 2) if p > 0 else None, "ev": round(p * o_est - 1, 4)}


def _harder(leg: dict, pool: list[dict]) -> list[dict]:
    """La misma apuesta un escalón más difícil (misma familia y dirección; o de tiros a tiros a puerta)."""
    out = [x for x in pool if x["fam"] == leg["fam"] and x["dir"] == leg["dir"] and x["p"] < leg["p"] - 0.04]
    if leg.get("kind") == "shots":
        out += [x for x in pool if x["fam"] == leg["fam"] and x["dir"] == "sot:O" and x["p"] < leg["p"] - 0.04]
    return sorted(out, key=lambda x: -x["p"])[:3]


def _cluster(leg: dict) -> str:
    """Patas que van muy ligadas entre sí (goles totales, goles de cada equipo, ambos marcan, resultado) cuentan como
    una sola familia: multiplicarlas como si fueran independientes daría una cuota que bet365 nunca paga."""
    f = leg["fam"]
    if f.startswith("p"):
        return f                                   # cada jugador, la suya
    base = f.split("_")[0]
    if base in ("goles", "ambos", "resultado"):
        return "goles"
    if base in ("tiros",):
        return "tiros"
    return base


def _search_step(base: list[dict], pool: list[dict], odds_rng: tuple, max_legs: int) -> list[dict] | None:
    """Desde las patas del peldaño anterior, ir subiendo líneas o añadiendo patas (lo más probable primero) hasta que la
    cuota estimada de «crear apuesta» caiga en la franja. De todas las que caen, la que más sale (a igualdad, más valor)."""
    lo, hi = odds_rng
    def key(legs):
        return tuple(sorted(x["label"] for x in legs))
    beam = [list(base)]
    seen = {key(base)}
    best, best_sc = None, None
    for _ in range(12):
        nxt = []
        for legs in beam:
            fams = {_cluster(x) for x in legs}
            moves = []
            for i, lg in enumerate(legs):                        # subir una línea
                for h in _harder(lg, pool)[:2]:
                    moves.append(legs[:i] + [h] + legs[i + 1:])
            if len(legs) < max_legs:                             # añadir otra cosa probable
                for e in pool:
                    if _cluster(e) not in fams and e["p"] >= 0.50:
                        moves.append(legs + [e])
            for mv in moves:
                k = key(mv)
                if k in seen:
                    continue
                seen.add(k)
                c = _combo(mv)
                if c["odds_est"] > hi * 1.02:
                    continue
                if c["odds_est"] >= lo and c["legs"]:
                    sc = c["p"] * (1 + 0.15 * c["ev"])            # lo que más se cumple; el valor desempata
                    if best_sc is None or sc > best_sc:
                        best, best_sc = mv, sc
                else:
                    nxt.append(mv)
        if not nxt:
            break
        # seguir por los caminos que conservan más probabilidad por cada punto de cuota ganado
        nxt.sort(key=lambda L: -(math.log(max(_combo(L)["p"], 1e-9)) / max(math.log(max(_combo(L)["odds_est"], 1.0001)), 1e-6)))
        beam = nxt[:40]
    return best


_STAT_COL = {"shots": "shots", "sot": "sot", "fouls": "fouls", "fouled": "was_fouled", "tackles": "tackles"}


def _streak(pid: int, kind: str, line: float, before: int, n: int = 10) -> tuple[float, int] | None:
    """Racha como en Statshub: en sus últimos partidos jugados (45+ min), ¿cuántas veces superó la línea?"""
    from .insights import _player_rows
    rows = [r for r in _player_rows(int(pid), before, 25) if (r["minutes"] or 0) >= 45][:n]
    if len(rows) < 6:
        return None
    col = _STAT_COL[kind]
    hits = sum(1 for r in rows if float(r[col] or 0) > line + 1e-9)
    return hits / len(rows), len(rows)


def _chains(pool: list[dict], kickoff: int, min_streak: float = 0.75, min_main: float = 1.75, min_low: float = 1.20) -> list[dict]:
    """Escaleras como las haces tú en Statshub:
    1. racha buena en la línea de +1,5 o +2,5 (sale en el 75 % o más de sus últimos partidos) que paga 1,75 o más;
    2. peldaño 1 = esa misma estadística una línea más abajo, si paga 1,20 o más;
    3. peldaño 2 = la línea de la racha; peldaño 3 = una línea más arriba."""
    by: dict = {}
    for x in pool:
        if x.get("pid") and x.get("kind") in _PLAYER_KINDS and str(x["dir"]).endswith(":O") and x.get("line") is not None:
            by.setdefault((x["pid"], x["kind"]), {})[float(x["line"])] = x
    out = []
    for (pid, kind), lines in by.items():
        best = None
        for Lm in (1.5, 2.5):
            main, low, up = lines.get(Lm), lines.get(Lm - 1), lines.get(Lm + 1)
            if not main or not low or main["b365"] < min_main or low["b365"] < min_low:
                continue
            st = _streak(pid, kind, Lm, kickoff)
            if not st or st[0] < min_streak:
                continue
            st_low = _streak(pid, kind, Lm - 1, kickoff)
            if not up and kind == "shots":
                up = next((y for y in pool if y.get("pid") == pid and y.get("kind") == "sot" and str(y["dir"]).endswith(":O")
                           and y["p"] < main["p"] and y["b365"] > main["b365"]), None)
            c = {"pid": pid, "kind": kind, "mu": low.get("mu"), "line": Lm, "streak": round(st[0], 2), "n": st[1],
                 "streak_low": round(st_low[0], 2) if st_low else None, "rungs": [low, main, up or main],
                 "edge": st[0] * main["b365"] - 1}
            c["has_up"] = up is not None
            if best is None or (c["has_up"], c["edge"]) > (best["has_up"], best["edge"]):
                best = c
        if best:
            out.append(best)
    return out


def ladder(mid: int) -> dict | None:
    import itertools
    d = _ladder_legs(mid)
    pool = [x for x in ((d or {}).get("legs") or []) if 1.05 <= x["b365"] <= 15]
    ko = int((d or {}).get("match", {}).get("kickoff") or 0) or int(__import__("time").time())
    # primero tu criterio exacto (racha 75 %+ en +1,5/+2,5 que paga 1,75+, y la línea de abajo a 1,20+); si en el
    # partido no hay ninguna así, uno algo más suave (70 %, 1,50 y 1,12), avisándolo
    level = "estricta"
    chains = _chains(pool, ko)          # sin escalera que cumpla tu criterio, no hay escalera
    if not chains:
        return None
    # las mejores rachas primero (las que más se cumplen y mejor pagan en la línea de la racha)
    chains = sorted(chains, key=lambda c: -(c["streak"] + 0.3 * c["edge"]))[:9]
    best, best_sc = None, None
    for n in (1, 2, 3):                 # como mucho 3 selecciones en la base
        for cs in itertools.combinations(chains, n):
            if len({c["pid"] for c in cs}) < n:
                continue                                    # un jugador, una cadena
            steps = [_combo([c["rungs"][i] for c in cs]) for i in range(3)]
            if steps[2]["odds_est"] <= steps[1]["odds_est"] + 0.01:
                continue                                    # el tercer peldaño tiene que subir algo
            o1, o2, o3 = (s_["odds_est"] for s_ in steps)
            if o1 < LADDER_ODDS[0][0]:
                continue
            # cerca de lo que buscas: base ≥1,70 (lo más segura posible), 2.º 4–6, 3.º 12–30
            pen = 0.0
            pen += max(0, o1 - LADDER_ODDS[0][1]) * 0.6
            pen += 0.15 * (max(0, LADDER_ODDS[1][0] - o2) + max(0, o2 - LADDER_ODDS[1][1]))
            pen += 0.02 * (max(0, LADDER_ODDS[2][0] - o3) + max(0, o3 - LADDER_ODDS[2][1]))
            racha = sum(c["streak"] for c in cs) / n
            sc = steps[0]["p"] + 0.3 * steps[1]["p"] + 0.5 * racha + 0.3 * steps[1]["ev"] - pen
            if best_sc is None or sc > best_sc:
                best, best_sc = (cs, steps), sc
    if not best:
        return None
    cs, steps = best
    for st_, stake in zip(steps, LADDER_STAKES):
        st_["stake_u"] = stake
    tot_ev = sum(st_["ev"] * st_["stake_u"] for st_ in steps)
    m = d["match"]
    return {"match": {k: m.get(k) for k in ("id", "comp", "kickoff", "home_name", "away_name")}, "steps": steps,
            "chains": [{"player": c["rungs"][0].get("player"), "kind": c["kind"], "mu": c["mu"], "line": c["line"],
                        "streak": c["streak"], "n": c["n"], "streak_low": c["streak_low"]} for c in cs],
            "ev_units": round(tot_ev, 3), "stake_units": sum(LADDER_STAKES), "level": level}


def refresh_ladders(hours: float = 48) -> int:
    import time as _t
    now = int(_t.time())
    out = []
    for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now + 600, now + int(hours * 3600))):
        try:
            lad = ladder(r["id"])
        except Exception:
            lad = None
        if lad:
            out.append(lad)
    out.sort(key=lambda x: -(x["steps"][0]["p"] + 0.5 * x["ev_units"]))     # primero, la base que más se cumple
    db.kv_set("ladders", {"ts": now, "items": out[:15]})
    return len(out)


def b365_drift(mid: int, hours: float = 8) -> dict:
    """Cuota de bet365 ahora / la primera de las últimas `hours` horas, por selección."""
    import time as _time
    rows = db.q("SELECT market, period, selection, line, odds, ts FROM odds WHERE match_id=? AND bookmaker='Bet365' "
                "AND phase IN ('snap','open') AND ts>=? ORDER BY ts", (mid, int(_time.time() - hours * 3600)))
    first, last = {}, {}
    for r in rows:
        k = (r["market"], r["period"] or "FT", r["selection"], None if r["line"] is None else round(float(r["line"]), 2))
        first.setdefault(k, r["odds"])
        last[k] = r["odds"]
    return {k: last[k] / first[k] for k in last if first.get(k)}



def retract_reason(s: dict, k: str, values: list[dict]) -> list[str]:
    """Por qué deja de recomendarse una apuesta ya enviada: cuota, probabilidad, valor, aviso de cuota disparada,
    fiabilidad y las señales en contra que han aparecido en el análisis."""
    from html import escape
    v = next((x for x in values if vkey(x["match"]["id"], x["market"], x.get("period"), x["selection"], x.get("line")) == k), None)
    out: list[str] = []
    if not v:
        from .insights import _row_for
        from .pricing import price_match
        pr = price_match(s["mid"])
        row = _row_for(pr["rows"], s["market"], s.get("period") or "FT", s["selection"], s.get("line")) if pr else None
        if not row or not row.get("b365"):
            return ["bet365 ya no ofrece esta apuesta (ha quitado el mercado o esa línea), así que no se puede recomendar."]
        v = {**row, "match": {"id": s["mid"]}}
    b, p, p0 = v.get("b365"), v.get("p"), s.get("p")
    if b and b < s["odds"] * 0.97:
        out.append(f"La cuota ha bajado de {s['odds']:.2f} a <b>{b:.2f}</b>: el mercado ya ha corregido el desfase.")
    if p and p0 and p < p0 - 0.02:
        out.append(f"Nuestra probabilidad ha bajado del {round(p0 * 100)} % al <b>{round(p * 100)} %</b>.")
    if b and p:
        ev = p * b - 1
        if ev <= 0.02:
            out.append(f"A {b:.2f} ya no compensa: con un {round(p * 100)} % debería pagar al menos <b>{1 / p * 1.02:.2f}</b>.")
    if v.get("move_warn"):
        out.append(escape(v["move_warn"]))
    elif v.get("conf") == "baja":
        out.append("El precio ya no es fiable (pocas casas lo ofrecen o los datos no cuadran).")
    try:
        from .insights import bet_insight
        r = bet_insight(s["mid"], s["market"], s.get("period") or "FT", s["selection"], s.get("line")) or {}
    except Exception:
        r = {}
    skip = {"precio", "consenso", "simulacion", "racha10"}
    con = sorted([x for x in r.get("signals") or [] if x.get("dir", 0) < 0 and x.get("key") not in skip], key=lambda x: -x.get("w", 0))
    for x in con[:3]:
        out.append("En contra: " + escape(x["text"]))
    if r.get("verdict") and r.get("tone") in ("bad", "warn"):
        out.append(f"Veredicto del análisis ahora: <b>{escape(r['verdict'])}</b>.")
    return out

def tip_alerts(hours: float = 36, fast: set | None = None) -> int:
    """Telegram: las recomendaciones OFICIALES (las de la portada, una por partido), no todo lo que tiene algo de valor.
    - Nueva recomendación → se envía con la apuesta que recomienda la web.
    - Si su cuota sube un 5 % o más → aviso «ahora paga más».
    - Si deja de tener valor antes del partido (dos revisiones seguidas) → aviso «ya no la recomendamos».
    Lo enviado se guarda (kv tips_sent) para el repaso al acabar el partido."""
    import time as _time
    from .followup import page_stake
    from .notify import alert
    now = _time.time()
    sent = db.kv_get("tips_sent") or {}
    picks = [v for v in official_picks(hours) if v["match"]["kickoff"] > now + 600 and v.get("b365")]
    o = opportunities(hours)
    alive = {vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line")): v for v in o["values"]
             if v.get("conf") in ("alta", "media") and pick_score(v) > 0}
    n = 0
    for v in picks:
        k = vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line"))
        mt = v["match"]
        s = sent.get(k)
        if s and s.get("state") == "active" and not s.get("sent"):
            s = None                 # se generó pero Telegram no la entregó: se vuelve a enviar
        if s and s.get("state") == "active":
            s["miss"] = 0
            if v["b365"] >= s["odds"] * 1.05:
                alert("tip_up", "📈 SUBE LA CUOTA",
                      f"⚽ {mt['home_name']} – {mt['away_name']}\n✅ <b>{v['label']}</b>\n"
                      f"💶 Ahora <b>{v['b365']:.2f}</b> (antes {s['odds']:.2f}) · sale el {round(v['p'] * 100)} %\n"
                      f"👉 Si aún no la tienes, ahora paga más por lo mismo: mejor momento para entrar.",
                      match_id=mt["id"], payload={"key": k}, dedupe=f"tipup:{k}:{v['b365']:.2f}")
                s["odds"] = round(float(v["b365"]), 3)
                s["stake"] = page_stake(v.get("p"), v["b365"], v.get("group") == "jugadores", v.get("kf"))
            continue
        stake = page_stake(v.get("p"), v["b365"], v.get("group") == "jugadores", v.get("kf"))
        from .tipster import tip_message
        aid = alert("tip", "🎯 APUESTA RECOMENDADA", tip_message(v, stake),
                    match_id=mt["id"], payload={"key": k}, dedupe=f"tip:{k}:{int(now)}" if (k in sent) else f"tip:{k}")
        sent[k] = {"mid": mt["id"], "label": v["label"], "market": v["market"], "selection": v["selection"], "line": v.get("line"),
                   "period": v.get("period") or "FT", "odds": round(float(v["b365"]), 3), "stake": stake, "p": v.get("p"),
                   "kickoff": mt["kickoff"], "state": "active", "miss": 0, "ts": int(now),
                   "sent": bool(aid and (db.q1("SELECT sent FROM alerts WHERE id=?", (aid,)) or {}).get("sent"))}
        n += 1
    # las enviadas que ya no tienen valor (antes del partido): aviso tras dos revisiones seguidas, para no ir y venir
    for k, s in sent.items():
        if s.get("state") != "active" or s["kickoff"] <= now + 300 or k in alive:
            if k in alive and s.get("state") == "active":
                s["miss"] = 0
            continue
        s["miss"] = s.get("miss", 0) + 1
        if s["miss"] >= (1 if s["mid"] in (fast or set()) else 2):      # si cambió el once o una baja: al momento
            m = db.q1("SELECT home_name, away_name FROM matches WHERE id=?", (s["mid"],)) or {}
            try:
                why = retract_reason(s, k, o["values"])
            except Exception as e:
                db.log_job("tips", "error", f"motivo {k}: {e}")
                why = []
            why_txt = ("\n".join(f"• {w}" for w in why[:5]) if why else
                       "La cuota ha bajado o ha cambiado algo (alineación, noticias, el mercado) y ya no tiene valor.")
            alert("tip_off", "✋ YA NO LA RECOMENDAMOS",
                  f"⚽ <b>{m.get('home_name')} – {m.get('away_name')}</b>\n\n"
                  f"<blockquote>❌ <b>{s['label']}</b>\n💶 te la enviamos a {s['odds']:.2f}</blockquote>"
                  f"<b>🔎 Por qué</b>\n{why_txt}\n\n"
                  f"👉 Si aún no la has hecho, no la hagas. Si ya la tienes, no pasa nada: el repaso la contará.",
                  match_id=s["mid"], payload={"key": k}, dedupe=f"tipoff:{k}:{s['ts']}")
            s["state"] = "retracted"
    db.kv_set("tips_sent", {k: s for k, s in sent.items() if s["kickoff"] > now - 4 * 86400})
    return n
