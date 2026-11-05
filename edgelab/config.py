"""Configuración central de edgelab.

Todo se lee de variables de entorno (fichero .env en la raíz) para que el mismo
código funcione en local y en la VM sin tocar nada.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# --------------------------------------------------------------------------
# Competiciones objetivo. Los IDs son los "uniqueTournamentId" de Statshub,
# que coinciden con los de Sofascore.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Competition:
    key: str
    name: str
    unique_id: int
    kind: str  # league | europe | cup
    football_data_code: str | None = None  # código de football-data.co.uk
    understat_code: str | None = None
    country: str = ""


COMPETITIONS: dict[str, Competition] = {
    "EPL": Competition("EPL", "Premier League", 17, "league", "E0", "EPL", "England"),
    "LALIGA": Competition("LALIGA", "LaLiga", 8, "league", "SP1", "La_liga", "Spain"),
    "SERIEA": Competition("SERIEA", "Serie A", 23, "league", "I1", "Serie_A", "Italy"),
    "BUNDES": Competition("BUNDES", "Bundesliga", 35, "league", "D1", "Bundesliga", "Germany"),
    "LIGUE1": Competition("LIGUE1", "Ligue 1", 34, "league", "F1", "Ligue_1", "France"),
    "UCL": Competition("UCL", "Champions League", 7, "europe"),
    "UEL": Competition("UEL", "Europa League", 679, "europe"),
    "UECL": Competition("UECL", "Conference League", 17015, "europe"),
    "CDR": Competition("CDR", "Copa del Rey", 329, "cup", country="Spain"),
}
UNIQUE_TO_KEY = {c.unique_id: c.key for c in COMPETITIONS.values()}


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("EDGELAB_DB", str(ROOT / "data" / "edgelab.sqlite"))))
    data_dir: Path = field(default_factory=lambda: Path(_env("EDGELAB_DATA", str(ROOT / "data"))))

    # Statshub
    statshub_base: str = field(default_factory=lambda: _env("STATSHUB_BASE", "https://www.statshub.com"))
    statshub_relay: str = field(default_factory=lambda: _env("STATSHUB_RELAY", ""))  # Worker de Cloudflare opcional
    statshub_cookie: str = field(default_factory=lambda: _env("STATSHUB_COOKIE", ""))
    statshub_relay_key: str = field(default_factory=lambda: _env("RELAY_KEY", ""))
    statshub_min_interval: float = field(default_factory=lambda: float(_env("STATSHUB_MIN_INTERVAL", "2.5")))

    # Sofascore (opcional, para incidencias y estadísticas en vivo)
    sofascore_enabled: bool = field(default_factory=lambda: _env("SOFASCORE_ENABLED", "1") == "1")

    # Betfair (opcional: app key "delayed" gratuita)
    betfair_app_key: str = field(default_factory=lambda: _env("BETFAIR_APP_KEY", ""))
    betfair_username: str = field(default_factory=lambda: _env("BETFAIR_USERNAME", ""))
    betfair_password: str = field(default_factory=lambda: _env("BETFAIR_PASSWORD", ""))

    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

    # Web
    web_password: str = field(default_factory=lambda: _env("EDGELAB_PASSWORD", ""))
    public_url: str = field(default_factory=lambda: _env("EDGELAB_PUBLIC_URL", ""))

    # Banca y staking
    bankroll: float = field(default_factory=lambda: float(_env("EDGELAB_BANKROLL", "1000")))
    kelly_fraction: float = field(default_factory=lambda: float(_env("EDGELAB_KELLY", "0.25")))
    max_stake_pct: float = field(default_factory=lambda: float(_env("EDGELAB_MAX_STAKE_PCT", "0.03")))

    # Margen supuesto de bet365 en directo cuando no hay cuotas reales grabadas
    assumed_inplay_margin: float = field(default_factory=lambda: float(_env("EDGELAB_INPLAY_MARGIN", "0.07")))

    competitions: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in _env("EDGELAB_COMPETITIONS", ",".join(COMPETITIONS)).split(",") if x)
    )


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
