"""Understat: tiros con minuto y xG de las 5 grandes ligas desde 2014 (gratis).

De aquí sale la cronología de cada partido (minuto de cada gol y el xG
acumulado minuto a minuto), que es lo que necesitan los backtests de
estrategias en directo (p. ej. "B perdiendo al 75' + cobertura") y el
calibrado del modelo en vivo.

Understat cambió en 2025 a cargar los datos por AJAX; se prueban las dos
formas (endpoint JSON y JSON embebido en el HTML).
"""
from __future__ import annotations

import codecs
import json
import re
from datetime import datetime, timezone

from ..config import COMPETITIONS
from ..core import db
from . import teams
from .http import PoliteClient

BASE = "https://understat.com"
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}


def _embedded(html: str, var: str):
    m = re.search(rf"var\s+{var}\s*=\s*JSON\.parse\('(.+?)'\)", html)
    if not m:
        return None
    raw = codecs.decode(m.group(1), "unicode_escape")
    return json.loads(raw)


class Understat:
    def __init__(self, client: PoliteClient | None = None):
        self.c = client or PoliteClient(min_interval=1.5, headers={"Referer": BASE + "/"})

    def league_matches(self, league: str, season_start_year: int) -> list[dict]:
        try:
            d = self.c.get_json(f"{BASE}/getLeagueData/{league}/{season_start_year}", headers=AJAX, ttl=3600 * 6)
            return d.get("dates") or d.get("datesData") or []
        except Exception:
            html = self.c.get_text(f"{BASE}/league/{league}/{season_start_year}", ttl=3600 * 6)
            return _embedded(html, "datesData") or []

    def match_shots(self, match_id: int) -> dict:
        try:
            d = self.c.get_json(f"{BASE}/getMatchData/{match_id}", headers=AJAX, ttl=86400 * 30)
            shots = d.get("shots") or d.get("shotsData")
            if shots:
                return shots
        except Exception:
            pass
        html = self.c.get_text(f"{BASE}/match/{match_id}", ttl=86400 * 30)
        return _embedded(html, "shotsData") or {"h": [], "a": []}


def build_timeline(shots: dict, hg: int | None = None, ag: int | None = None) -> list[dict]:
    """Tiros → eventos (gol / tiro) con lado y xG. Corrige el lado de los autogoles
    si el recuento no cuadra con el marcador final."""
    ev = []
    for side_key, side in (("h", "H"), ("a", "A")):
        for s in shots.get(side_key, []) or []:
            minute = float(s.get("minute", 0))
            res = s.get("result", "")
            xg = float(s.get("xG", 0) or 0)
            if res == "OwnGoal":
                ev.append({"minute": minute, "kind": "goal", "side": side, "xg": 0.0, "player": s.get("player"), "own": True})
            else:
                ev.append({"minute": minute, "kind": "shot", "side": side, "xg": xg, "player": s.get("player")})
                if res == "Goal":
                    ev.append({"minute": minute, "kind": "goal", "side": side, "xg": xg, "player": s.get("player")})
    if hg is not None and ag is not None:
        gh = sum(1 for e in ev if e["kind"] == "goal" and e["side"] == "H")
        ga = sum(1 for e in ev if e["kind"] == "goal" and e["side"] == "A")
        if (gh, ga) != (hg, ag):
            for e in ev:
                if e.get("own"):
                    e["side"] = "A" if e["side"] == "H" else "H"
    ev.sort(key=lambda e: (e["minute"], e["kind"] != "shot"))
    return ev


def store_timeline(match_id: int, events: list[dict], source: str = "understat") -> None:
    db.execute("DELETE FROM timeline WHERE match_id=? AND source=?", (match_id, source))
    db.executemany("INSERT INTO timeline(match_id,minute,kind,side,xg,player,source) VALUES(?,?,?,?,?,?,?)",
                   [(match_id, e["minute"], e["kind"], e["side"], e.get("xg"), e.get("player"), source) for e in events])


def backfill(comp: str, season_start_year: int, us: Understat | None = None, with_shots: bool = True) -> int:
    us = us or Understat()
    league = COMPETITIONS[comp].understat_code
    if not league:
        return 0
    country = COMPETITIONS[comp].country.lower()
    n = 0
    for m in us.league_matches(league, season_start_year):
        if not m.get("isResult"):
            continue
        dt = datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        hid = teams.resolve(m["h"]["title"], "understat", country)
        aid = teams.resolve(m["a"]["title"], "understat", country)
        hg, ag = int(m["goals"]["h"]), int(m["goals"]["a"])
        row = db.find_match(hid, aid, int(dt.timestamp()))
        season = f"{season_start_year % 100:02d}{(season_start_year + 1) % 100:02d}"
        if row is None:
            hn = db.q1("SELECT name FROM teams WHERE id=?", (hid,))["name"]
            an = db.q1("SELECT name FROM teams WHERE id=?", (aid,))["name"]
            mid = db.upsert_match({"id": None, "comp": comp, "season": season, "kickoff": int(dt.timestamp()),
                                   "home_id": hid, "away_id": aid, "home_name": hn, "away_name": an,
                                   "status": "finished", "hg": hg, "ag": ag, "source": "understat",
                                   "understat_id": int(m["id"])})
        else:
            mid = row["id"]
            db.execute("UPDATE matches SET understat_id=? WHERE id=?", (int(m["id"]), mid))
        # xG total del partido
        for side, k in (("H", "h"), ("A", "a")):
            ex = db.q1("SELECT stats FROM match_stats WHERE match_id=? AND side=?", (mid, side))
            st = json.loads(ex["stats"]) if ex else {}
            st["xg_understat"] = float(m["xG"][k] or 0)
            db.execute("INSERT INTO match_stats(match_id,side,stats,source) VALUES(?,?,?,?) "
                       "ON CONFLICT(match_id,side) DO UPDATE SET stats=excluded.stats", (mid, side, json.dumps(st), "mixed"))
        if with_shots and not db.q1("SELECT 1 FROM timeline WHERE match_id=? AND source='understat' LIMIT 1", (mid,)):
            try:
                store_timeline(mid, build_timeline(us.match_shots(int(m["id"])), hg, ag))
            except Exception:
                pass
        n += 1
    return n
