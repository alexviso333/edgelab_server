"""Open-Meteo (gratis, sin clave): tiempo en el estadio a la hora del partido.
Previsión para próximos partidos y archivo histórico para backtests."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .http import PoliteClient

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def at_kickoff(lat: float, lon: float, kickoff: int, client: PoliteClient | None = None) -> dict | None:
    c = client or PoliteClient(min_interval=0.5)
    dt = datetime.fromtimestamp(kickoff, tz=timezone.utc)
    day = dt.strftime("%Y-%m-%d")
    hist = (datetime.now(timezone.utc) - dt).days > 5
    url = ARCHIVE if hist else FORECAST
    try:
        d = c.get_json(url, {"latitude": lat, "longitude": lon, "start_date": day, "end_date": day,
                             "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m", "timezone": "UTC"},
                       ttl=3600 * 3 if not hist else 86400 * 365)
    except Exception:
        return None
    h = d.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    idx = min(range(len(times)), key=lambda i: abs(datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc).timestamp() - kickoff))
    # ventana de 2 h de partido
    sl = slice(idx, min(idx + 2, len(times)))
    return {"temp": (h["temperature_2m"][idx]), "rain_mm": sum(x or 0 for x in h["precipitation"][sl]),
            "wind": max(x or 0 for x in h["wind_speed_10m"][sl]), "gusts": max(x or 0 for x in h["wind_gusts_10m"][sl])}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
