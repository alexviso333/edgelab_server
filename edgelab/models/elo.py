"""Elo de fútbol con margen de goles y ventaja de campo (estilo clubelo/eloratings).

Una única escala para todas las competiciones: los partidos europeos conectan
las ligas entre sí. Se actualiza después de cada partido (aprendizaje continuo).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Elo:
    k: float = 20.0
    home_adv: float = 65.0
    init: float = 1500.0
    regress: float = 0.2            # regresión a la media de su liga entre temporadas
    ratings: dict[int, float] = field(default_factory=dict)
    last_season: dict[int, str] = field(default_factory=dict)

    def get(self, team: int) -> float:
        return self.ratings.get(team, self.init)

    def expected(self, h: int, a: int, neutral: bool = False) -> float:
        d = self.get(h) - self.get(a) + (0 if neutral else self.home_adv)
        return 1.0 / (1.0 + 10 ** (-d / 400))

    def diff(self, h: int, a: int, neutral: bool = False) -> float:
        return self.get(h) - self.get(a) + (0 if neutral else self.home_adv)

    def _season_reset(self, team: int, season: str, league_mean: float | None) -> None:
        if team in self.last_season and self.last_season[team] != season:
            mean = league_mean if league_mean is not None else self.init
            self.ratings[team] = self.get(team) * (1 - self.regress) + mean * self.regress
        self.last_season[team] = season

    def update(self, h: int, a: int, hg: int, ag: int, season: str = "", weight: float = 1.0,
               neutral: bool = False) -> tuple[float, float]:
        self._season_reset(h, season, None)
        self._season_reset(a, season, None)
        e = self.expected(h, a, neutral)
        s = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        gd = abs(hg - ag)
        mult = 1.0 if gd <= 1 else (1.5 if gd == 2 else (11 + gd) / 8)
        delta = self.k * weight * mult * (s - e)
        self.ratings[h] = self.get(h) + delta
        self.ratings[a] = self.get(a) - delta
        return self.ratings[h], self.ratings[a]

    def to_dict(self) -> dict:
        return {"k": self.k, "home_adv": self.home_adv, "ratings": {str(k): v for k, v in self.ratings.items()},
                "last_season": {str(k): v for k, v in self.last_season.items()}}

    @staticmethod
    def from_dict(d: dict) -> "Elo":
        e = Elo(k=d.get("k", 20.0), home_adv=d.get("home_adv", 65.0))
        e.ratings = {int(k): v for k, v in d.get("ratings", {}).items()}
        e.last_season = {int(k): v for k, v in d.get("last_season", {}).items()}
        return e


def comp_weight(comp: str) -> float:
    return {"UCL": 1.2, "UEL": 1.1, "UECL": 1.0, "CDR": 0.7}.get(comp, 1.0)
