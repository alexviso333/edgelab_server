"""Lenguaje de estrategias (plantilla de 7 piezas).

Toda estrategia, conocida o inventada, es un JSON con:
  context      filtros pre-partido (competición, favorito, bandas de cuota, variables)
  trigger      solo directo: minuto, marcador, quién gana, rojas, xG, goles recientes...
  market       qué se apuesta (con roles relativos: fav/dog/leading/trailing/home/away)
  price        de dónde sale la cuota y cuota mínima/máxima
  exit         mantener, o cubrir ante un evento (gol, empate, minuto) con un tamaño
  staking      plano o Kelly fraccional con tope
  model_filter ventaja mínima frente a la probabilidad del modelo

Así el motor puede mutar y cruzar piezas sin programar cada estrategia a mano.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

FEATURE_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
               "<": lambda a, b: a < b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


@dataclass
class Strategy:
    name: str
    phase: str                      # pre | live
    family: str = ""
    description: str = ""
    hypothesis: str = ""
    context: dict = field(default_factory=dict)
    trigger: dict = field(default_factory=dict)
    market: dict = field(default_factory=dict)
    price: dict = field(default_factory=dict)
    exit: dict = field(default_factory=lambda: {"type": "hold"})
    staking: dict = field(default_factory=lambda: {"type": "flat", "units": 1.0})
    model_filter: dict = field(default_factory=dict)
    data_needs: str = "G"           # G gratis desde ya | R grabador de cuotas | A API de pago
    control: bool = False           # control negativo (no debería tener ventaja)
    origin: str = "catalog"
    parent: str | None = None

    def spec(self) -> dict:
        return {"phase": self.phase, "context": self.context, "trigger": self.trigger, "market": self.market,
                "price": self.price, "exit": self.exit, "staking": self.staking, "model_filter": self.model_filter}

    @property
    def id(self) -> str:
        raw = json.dumps(self.spec(), sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["id"] = self.id
        return d

    @staticmethod
    def from_dict(d: dict) -> "Strategy":
        d = {k: v for k, v in d.items() if k in Strategy.__dataclass_fields__}
        return Strategy(**copy.deepcopy(d))

    def clone(self, **changes: Any) -> "Strategy":
        s = Strategy.from_dict(self.to_dict())
        for k, v in changes.items():
            setattr(s, k, v)
        return s

    def describe(self) -> str:
        """Descripción legible en castellano de la regla."""
        parts = []
        c = self.context
        if c.get("comps"):
            parts.append("en " + ", ".join(c["comps"]))
        if c.get("fav_prob"):
            lo, hi = c["fav_prob"]
            parts.append(f"favorito con prob. {lo:.0%}-{hi:.0%}")
        if c.get("fav_side"):
            parts.append({"home": "favorito local", "away": "favorito visitante"}[c["fav_side"]])
        for f in c.get("filters", []):
            parts.append(f"{f['f']} {f['op']} {f['v']}")
        t = self.trigger
        if self.phase == "live":
            if t.get("minute"):
                parts.append(f"minuto {t['minute'][0]}-{t['minute'][1]}")
            if t.get("score_state"):
                parts.append(f"marcador: {t['score_state']}")
            if t.get("leader"):
                parts.append(f"va ganando el {t['leader']}")
            if t.get("red"):
                parts.append(f"roja: {t['red']}")
            if t.get("xg_gap_min") is not None:
                parts.append(f"xG del {t.get('xg_team', 'dominante')} supera al rival en ≥{t['xg_gap_min']}")
            if t.get("goals_last10_min"):
                parts.append(f"≥{t['goals_last10_min']} goles en los últimos 10'")
        m = self.market
        parts.append(f"apostar {m.get('market')} {m.get('selection')}" + (f" {m['line']:+g}" if isinstance(m.get("line"), (int, float)) else (" línea principal" if m.get("line") == "main" else "")))
        p = self.price
        if p.get("min_odds"):
            parts.append(f"cuota ≥ {p['min_odds']}")
        if p.get("max_odds"):
            parts.append(f"cuota ≤ {p['max_odds']}")
        if self.model_filter.get("min_edge") is not None:
            parts.append(f"ventaja modelo ≥ {self.model_filter['min_edge']:.0%}")
        e = self.exit
        if e.get("type") == "hedge":
            on = {"equalizer": "empate", "goal": "gol", "goal_for": "gol a favor"}.get(e.get("on"), e.get("on"))
            parts.append(f"cubrir con {e['hedge']['market']} {e['hedge'].get('line', '')} del {e['hedge']['selection']} al {on} ({e.get('size')})")
        elif e.get("type") == "greenup":
            parts.append(f"cerrar (green-up) si la cuota cae ≥{e.get('drop', 0.3):.0%}")
        return "; ".join(parts)
