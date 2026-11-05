"""Definición y liquidación de mercados.

Una `Selection` describe exactamente qué se apuesta. La liquidación devuelve
qué fracción del stake gana (cobrando la cuota) y qué fracción se devuelve
(push), lo que cubre todas las líneas asiáticas (.0, .25, .5, .75).

Convenciones:
- `line` del hándicap asiático va SIEMPRE desde el punto de vista de la
  selección, como lo muestra bet365 (p. ej. A +0.25).
- En directo, bet365 liquida el AH solo con los goles marcados DESPUÉS de
  apostar: por eso la selección guarda `base_h`/`base_a` (marcador al apostar).
  Los totales (Over/Under) cuentan todos los goles del partido.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable

import numpy as np

MARKETS = {
    "1X2": "Resultado final",
    "DC": "Doble oportunidad",
    "DNB": "Empate no válido",
    "AH": "Hándicap asiático",
    "EH": "Hándicap europeo",
    "OU": "Más/Menos goles",
    "TEAM_OU": "Goles de un equipo",
    "BTTS": "Ambos marcan",
    "CS": "Resultado exacto",
    "WIN_TO_NIL": "Gana a cero",
    "NEXT_GOAL": "Próximo gol",
    "ODD_EVEN": "Goles par/impar",
    "CORNERS_OU": "Córners más/menos",
    "CORNERS_AH": "Hándicap de córners",
    "CARDS_OU": "Tarjetas más/menos",
    "CARDS_AH": "Hándicap de tarjetas",
    "SHOTS_OU": "Tiros más/menos",
    "SOT_OU": "Tiros a puerta más/menos",
}


@dataclass(frozen=True)
class Selection:
    market: str
    selection: str           # H D A | 1X 12 X2 | O U | Y N | "2-1" | HO HU AO AU | NONE
    line: float | None = None
    period: str = "FT"       # FT | HT | 2H
    base_h: int = 0          # marcador al apostar (para AH/DNB/NEXT_GOAL en directo)
    base_a: int = 0

    def label(self, home: str = "Local", away: str = "Visitante") -> str:
        name = {"H": home, "A": away, "D": "Empate", "O": "Más de", "U": "Menos de", "Y": "Sí", "N": "No",
                "1X": f"{home} o empate", "X2": f"Empate o {away}", "12": f"{home} o {away}"}.get(self.selection, self.selection)
        p = "" if self.period == "FT" else f" ({self.period})"
        base = f" [desde {self.base_h}-{self.base_a}]" if (self.base_h or self.base_a) and self.market in ("AH", "DNB", "NEXT_GOAL", "EH") else ""
        if self.market in ("AH", "EH", "CORNERS_AH", "CARDS_AH"):
            return f"{MARKETS[self.market]} {name} {self.line:+g}{p}{base}"
        if self.market in ("OU", "CORNERS_OU", "CARDS_OU", "SHOTS_OU", "SOT_OU"):
            return f"{MARKETS[self.market]}: {name} {self.line:g}{p}"
        if self.market == "TEAM_OU":
            team = home if self.selection.startswith("H") else away
            side = "Más de" if self.selection.endswith("O") else "Menos de"
            return f"{team} {side} {self.line:g} goles{p}"
        return f"{MARKETS.get(self.market, self.market)}: {name}{p}{base}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Selection":
        return Selection(**{k: d[k] for k in ("market", "selection", "line", "period", "base_h", "base_a") if k in d})


@dataclass
class Outcome:
    """Resultado final de un partido (todo lo que hace falta para liquidar)."""
    hg: int
    ag: int
    hg_ht: int | None = None
    ag_ht: int | None = None
    corners_h: int | None = None
    corners_a: int | None = None
    cards_h: float | None = None     # tarjetas (amarilla=1, roja=1) o puntos según mercado
    cards_a: float | None = None
    shots_h: int | None = None
    shots_a: int | None = None
    sot_h: int | None = None
    sot_a: int | None = None
    next_goal: str | None = None     # para NEXT_GOAL: H | A | NONE (calculado por el liquidador con la cronología)
    extra: dict = field(default_factory=dict)


def _split_line(line: float) -> list[float]:
    """Divide una línea de cuarto en sus dos medias apuestas."""
    frac = round((line * 4) % 2, 6)
    if abs(frac - 1) < 1e-6:  # .25 o .75
        return [line - 0.25, line + 0.25]
    return [line]


def _asian(margin_plus_line_parts: Iterable[float]) -> tuple[float, float]:
    parts = list(margin_plus_line_parts)
    w = p = 0.0
    for v in parts:
        if v > 1e-9:
            w += 1
        elif abs(v) <= 1e-9:
            p += 1
    n = len(parts)
    return w / n, p / n


def _period_goals(sel: Selection, o: Outcome) -> tuple[int, int]:
    if sel.period == "HT":
        if o.hg_ht is None:
            raise ValueError("falta marcador al descanso")
        return o.hg_ht, o.ag_ht
    if sel.period == "2H":
        if o.hg_ht is None:
            raise ValueError("falta marcador al descanso")
        return o.hg - o.hg_ht, o.ag - o.ag_ht
    return o.hg, o.ag


def settle(sel: Selection, o: Outcome) -> tuple[float, float]:
    """Devuelve (fracción ganada, fracción devuelta). El resto se pierde."""
    m = sel.market
    s = sel.selection
    if m in ("CORNERS_OU", "CARDS_OU", "SHOTS_OU", "SOT_OU", "CORNERS_AH", "CARDS_AH"):
        key = {"CORNERS": ("corners_h", "corners_a"), "CARDS": ("cards_h", "cards_a"),
               "SHOTS": ("shots_h", "shots_a"), "SOT": ("sot_h", "sot_a")}[m.split("_")[0]]
        vh, va = getattr(o, key[0]), getattr(o, key[1])
        if vh is None or va is None:
            raise ValueError(f"faltan datos para {m}")
        if m.endswith("_OU"):
            if s.startswith(("H", "A")) and len(s) == 2:  # total de un equipo
                tot = vh if s[0] == "H" else va
                s2 = s[1]
            else:
                tot, s2 = vh + va, s
            return _asian([(tot - L) if s2 == "O" else (L - tot) for L in _split_line(sel.line)])
        diff = (vh - va) if s == "H" else (va - vh)
        return _asian([diff + L for L in _split_line(sel.line)])

    h, a = _period_goals(sel, o)
    if m == "1X2":
        res = "H" if h > a else ("A" if a > h else "D")
        return (1.0 if res == s else 0.0), 0.0
    if m == "DC":
        res = "H" if h > a else ("A" if a > h else "D")
        ok = {"1X": res in ("H", "D"), "X2": res in ("A", "D"), "12": res in ("H", "A")}[s]
        return (1.0 if ok else 0.0), 0.0
    if m in ("AH", "DNB"):
        line = 0.0 if m == "DNB" else float(sel.line)
        rh, ra = h - sel.base_h, a - sel.base_a
        diff = (rh - ra) if s == "H" else (ra - rh)
        return _asian([diff + L for L in _split_line(line)])
    if m == "EH":  # hándicap europeo 3 vías: line entera aplicada al local, selección H/D/A
        d = (h - a) + float(sel.line)
        res = "H" if d > 0 else ("A" if d < 0 else "D")
        return (1.0 if res == s else 0.0), 0.0
    if m == "OU":
        tot = h + a
        return _asian([(tot - L) if s == "O" else (L - tot) for L in _split_line(float(sel.line))])
    if m == "TEAM_OU":
        tot = h if s[0] == "H" else a
        return _asian([(tot - L) if s[1] == "O" else (L - tot) for L in _split_line(float(sel.line))])
    if m == "BTTS":
        yes = h > 0 and a > 0
        return (1.0 if yes == (s == "Y") else 0.0), 0.0
    if m == "CS":
        x, y = (int(v) for v in s.split("-"))
        return (1.0 if (h, a) == (x, y) else 0.0), 0.0
    if m == "WIN_TO_NIL":
        ok = (h > a and a == 0) if s == "H" else (a > h and h == 0)
        return (1.0 if ok else 0.0), 0.0
    if m == "ODD_EVEN":
        return (1.0 if ((h + a) % 2 == 1) == (s == "ODD") else 0.0), 0.0
    if m == "NEXT_GOAL":
        if o.next_goal is None:
            raise ValueError("NEXT_GOAL necesita la cronología del partido")
        return (1.0 if o.next_goal == s else 0.0), 0.0
    raise ValueError(f"mercado desconocido {m}")


def pnl(sel: Selection, odds: float, stake: float, o: Outcome) -> float:
    w, p = settle(sel, o)
    lose = 1.0 - w - p
    return stake * (w * (odds - 1.0) - lose)


def status_from_fracs(w: float, p: float) -> str:
    if w >= 0.999:
        return "won"
    if p >= 0.999:
        return "push"
    if w >= 0.499 and p >= 0.499:
        return "halfwon"
    if p >= 0.499 and w < 1e-9:
        return "halflost"
    if w < 1e-9 and p < 1e-9:
        return "lost"
    return "mixed"


# ---------------------------------------------------------------------------
# Probabilidades a partir de una matriz de marcadores
# ---------------------------------------------------------------------------
def selection_probs(sel: Selection, score_probs: np.ndarray, base: tuple[int, int] = (0, 0),
                    ht: tuple[int, int] | None = None) -> tuple[float, float, float]:
    """(P ganar, P push, P perder) para mercados de goles.

    `score_probs[i, j]` = probabilidad de que el marcador FINAL sea i-j. Para
    selecciones de AH en directo el `base_h/base_a` de la selección marca desde
    dónde cuenta.
    """
    if sel.market in ("NEXT_GOAL",) or sel.period != "FT":
        raise ValueError("selection_probs solo admite mercados FT de goles; NEXT_GOAL se calcula aparte")
    w = p = 0.0
    n_h, n_a = score_probs.shape
    for i in range(n_h):
        for j in range(n_a):
            pr = score_probs[i, j]
            if pr <= 0:
                continue
            fw, fp = settle(sel, Outcome(i, j))
            w += pr * fw
            p += pr * fp
    total = score_probs.sum()
    w, p = w / total, p / total
    return w, p, max(0.0, 1.0 - w - p)


def fair_odds(w: float, p: float, l: float) -> float:
    """Cuota a la que el valor esperado es 0: w·(c−1) = l."""
    if w <= 1e-12:
        return math.inf
    return 1.0 + l / w


def ev_per_unit(odds: float, w: float, p: float, l: float) -> float:
    return w * (odds - 1.0) - l


def devig(odds: list[float], method: str = "power") -> list[float]:
    """Quita el margen de un mercado de N vías.

    - "mult": normaliza 1/c (sesga hacia el favorito-longshot).
    - "power": busca k con sum((1/c)^k)=1 (mejor con el sesgo favorito-longshot).
    - "shin": modelo de Shin (información privilegiada).
    """
    inv = np.array([1.0 / o for o in odds], dtype=float)
    if method == "mult":
        return list(inv / inv.sum())
    if method == "power":
        lo, hi = 0.5, 3.0
        for _ in range(80):
            k = (lo + hi) / 2
            s = (inv ** k).sum()
            if s > 1:
                lo = k
            else:
                hi = k
        pr = inv ** ((lo + hi) / 2)
        return list(pr / pr.sum())
    if method == "shin":
        b = inv.sum()
        n = len(odds)
        lo, hi = 0.0, 0.4
        for _ in range(80):
            z = (lo + hi) / 2
            pr = (np.sqrt(z * z + 4 * (1 - z) * inv * inv / b) - z) / (2 * (1 - z))
            if pr.sum() > 1:
                lo = z
            else:
                hi = z
        return list(pr / pr.sum())
    raise ValueError(method)


def overround(odds: Iterable[float]) -> float:
    return sum(1.0 / o for o in odds) - 1.0


def apply_margin(fair_probs: list[float], margin: float) -> list[float]:
    """Convierte probabilidades justas en cuotas con margen (método potencia inverso)."""
    p = np.clip(np.array(fair_probs, dtype=float), 1e-9, 1)
    target = 1.0 + margin
    lo, hi = 0.3, 1.0
    for _ in range(80):
        k = (lo + hi) / 2
        s = (p ** k).sum()
        if s > target:
            lo = k
        else:
            hi = k
    q = p ** ((lo + hi) / 2)
    return list(1.0 / q)


def devig_power_vec(odds: np.ndarray) -> np.ndarray:
    """devig 'power' vectorizado: odds (N, k) → probabilidades (N, k)."""
    inv = 1.0 / np.asarray(odds, float)
    lo = np.full(len(inv), 0.5); hi = np.full(len(inv), 3.0)
    for _ in range(60):
        k = (lo + hi) / 2
        s = (inv ** k[:, None]).sum(axis=1)
        big = s > 1
        lo = np.where(big, k, lo)
        hi = np.where(big, hi, k)
    p = inv ** ((lo + hi) / 2)[:, None]
    return p / p.sum(axis=1, keepdims=True)
