"""365Scores: lo que no da ninguna otra fuente gratis — el TIEMPO NETO jugado (minutos con el balón en juego) en
directo. Un partido muy cortado (faltas, VAR, lesiones) deja menos tiempo real para marcar y suele acabar con
más añadido. Su API web no tiene robots.txt que lo impida; una petición por minuto como mucho."""
from __future__ import annotations

import json
import re
import time
import urllib.request

from ..core import db
from .espn import _sim

BASE = "https://webws.365scores.com/web/"
COMPS = "7,11,17,25,35,572,573,7685,13"          # Premier, LaLiga, Serie A, Bundesliga, Ligue 1, UCL, UEL, UECL, Copa del Rey
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_C: dict = {}
_FAIL = {"until": 0.0}


def _get(path: str) -> dict | None:
    if time.time() < _FAIL["until"]:
        return None
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_UA), timeout=12).read())
        _FAIL["since"] = 0
        return d
    except Exception as e:
        _FAIL["until"] = time.time() + 300
        _FAIL["since"] = _FAIL.get("since") or time.time()
        db.log_job("365scores", "error", f"{e}")
        return None


def live_games() -> list[dict]:
    c = _C.get("live")
    if c and time.time() - c[0] < 60:
        return c[1]
    d = _get(f"games/current/?appTypeId=5&langId=1&timezoneName=UTC&userCountryId=2&competitions={COMPS}")
    games = [{"id": g.get("id"), "home": (g.get("homeCompetitor") or {}).get("name") or "",
              "away": (g.get("awayCompetitor") or {}).get("name") or "", "start": g.get("startTime")}
             for g in (d or {}).get("games") or []]
    if d is not None:
        _C["live"] = (time.time(), games)
    return games


def find_game(m: dict) -> int | None:
    best, bs = None, 0.0
    for g in live_games():
        sc = (_sim(m["home_name"], g["home"]) + _sim(m["away_name"], g["away"])) / 2
        if sc > bs:
            best, bs = g, sc
    return best["id"] if best and bs >= 0.62 else None


def _mmss(s: str) -> float | None:
    mm = re.search(r"(\d+):(\d+)", s or "")
    return int(mm.group(1)) + int(mm.group(2)) / 60 if mm else None


def net_time(m: dict) -> dict | None:
    """{'net': 43.7, 'total': 67.25, 'ratio': 0.65} — minutos netos con balón en juego frente al tiempo total."""
    key = ("net", m["id"])
    c = _C.get(key)
    if c and time.time() - c[0] < 110:
        return c[1]
    gid = find_game(m)
    if not gid:
        return None
    d = _get(f"game/?appTypeId=5&langId=1&timezoneName=UTC&userCountryId=2&gameId={gid}")
    apt = ((d or {}).get("game") or {}).get("actualPlayTime") or {}
    net = _mmss((apt.get("actualTime") or {}).get("name") or "")
    tot = _mmss((apt.get("totalTime") or {}).get("name") or "")
    out = {"net": round(net, 1), "total": round(tot, 1), "ratio": round(net / tot, 2)} if net and tot else None
    _C[key] = (time.time(), out)
    return out
