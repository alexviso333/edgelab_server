"""football-data.co.uk: resultados + cuotas históricas gratis de las 5 grandes ligas.

Trae, por partido: goles FT/HT, tiros, córners, faltas, tarjetas, árbitro (Inglaterra),
y cuotas de apertura y cierre de bet365, Pinnacle, máximo y media del mercado
para 1X2, Over/Under 2.5 y hándicap asiático. Es la base para backtestear el
pre-partido con precios REALES y medir el CLV contra el cierre de Pinnacle.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import COMPETITIONS
from ..core import db
from . import teams
from .http import PoliteClient

BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
LONDON = ZoneInfo("Europe/London")

# (prefijo columna, casa, fase)
ODDS_1X2 = [("B365", "Bet365", "open"), ("PS", "Pinnacle", "open"), ("Max", "Max", "open"), ("Avg", "Avg", "open"),
            ("B365C", "Bet365", "close"), ("PSC", "Pinnacle", "close"), ("MaxC", "Max", "close"), ("AvgC", "Avg", "close"),
            ("Bb", "BbAvg", "open")]
ODDS_OU = [("B365", "Bet365", "open"), ("P", "Pinnacle", "open"), ("Max", "Max", "open"), ("Avg", "Avg", "open"),
           ("B365C", "Bet365", "close"), ("PC", "Pinnacle", "close"), ("MaxC", "Max", "close"), ("AvgC", "Avg", "close")]
ODDS_AH = [("B365", "Bet365", "open", "AHh"), ("P", "Pinnacle", "open", "AHh"), ("Max", "Max", "open", "AHh"),
           ("Avg", "Avg", "open", "AHh"), ("B365C", "Bet365", "close", "AHCh"), ("PC", "Pinnacle", "close", "AHCh"),
           ("MaxC", "Max", "close", "AHCh"), ("AvgC", "Avg", "close", "AHCh")]


def seasons_back(n: int, today: datetime | None = None) -> list[str]:
    today = today or datetime.utcnow()
    y = today.year if today.month >= 7 else today.year - 1
    return [f"{(y - k) % 100:02d}{(y - k + 1) % 100:02d}" for k in range(n)]


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _int(x):
    v = _num(x)
    return int(v) if v is not None else None


def parse_csv(text: str, comp: str, season: str) -> list[dict]:
    text = text.lstrip("﻿")
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        if not r.get("HomeTeam") or r.get("FTHG") in (None, ""):
            continue
        d = r.get("Date", "")
        fmt = "%d/%m/%Y" if len(d.split("/")[-1]) == 4 else "%d/%m/%y"
        try:
            day = datetime.strptime(d, fmt)
        except ValueError:
            continue
        t = (r.get("Time") or "15:00").strip() or "15:00"
        try:
            hh, mm = (int(x) for x in t.split(":")[:2])
        except ValueError:
            hh, mm = 15, 0
        ko = int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=LONDON).timestamp())
        odds = []
        for pre, bk, ph in ODDS_1X2:
            for sel in ("H", "D", "A"):
                v = _num(r.get(f"{pre}{sel}"))
                if v and v > 1:
                    odds.append((ph, bk, "1X2", sel, None, v))
        for pre, bk, ph in ODDS_OU:
            for sel, sym in (("O", ">"), ("U", "<")):
                v = _num(r.get(f"{pre}{sym}2.5"))
                if v and v > 1:
                    odds.append((ph, bk, "OU", sel, 2.5, v))
        for pre, bk, ph, linecol in ODDS_AH:
            line = _num(r.get(linecol))
            if line is None:
                continue
            for sel, col, sgn in (("H", f"{pre}AHH", 1), ("A", f"{pre}AHA", -1)):
                v = _num(r.get(col))
                if v and v > 1:
                    odds.append((ph, bk, "AH", sel, line * sgn + 0.0, v))
        out.append({
            "comp": comp, "season": season, "kickoff": ko, "home": r["HomeTeam"].strip(), "away": r["AwayTeam"].strip(),
            "hg": _int(r.get("FTHG")), "ag": _int(r.get("FTAG")), "hg_ht": _int(r.get("HTHG")), "ag_ht": _int(r.get("HTAG")),
            "referee": (r.get("Referee") or "").strip() or None,
            "stats": {"H": {"shots": _int(r.get("HS")), "sot": _int(r.get("HST")), "corners": _int(r.get("HC")),
                            "fouls": _int(r.get("HF")), "yellow": _int(r.get("HY")), "red": _int(r.get("HR"))},
                      "A": {"shots": _int(r.get("AS")), "sot": _int(r.get("AST")), "corners": _int(r.get("AC")),
                            "fouls": _int(r.get("AF")), "yellow": _int(r.get("AY")), "red": _int(r.get("AR"))}},
            "odds": odds,
        })
    return out


def store(rows: list[dict]) -> int:
    import json
    n = 0
    for r in rows:
        comp = COMPETITIONS[r["comp"]]
        country = comp.country.lower()
        hid = teams.resolve(r["home"], "footballdata", country)
        aid = teams.resolve(r["away"], "footballdata", country)
        hname = db.q1("SELECT name FROM teams WHERE id=?", (hid,))["name"]
        aname = db.q1("SELECT name FROM teams WHERE id=?", (aid,))["name"]
        mid = db.upsert_match({"id": None, "comp": r["comp"], "season": r["season"], "kickoff": r["kickoff"],
                               "home_id": hid, "away_id": aid, "home_name": hname, "away_name": aname,
                               "status": "finished", "hg": r["hg"], "ag": r["ag"], "hg_ht": r["hg_ht"], "ag_ht": r["ag_ht"],
                               "referee": r["referee"], "source": "footballdata"})
        # estadísticas básicas (no pisan las de Statshub si ya existen)
        for side in ("H", "A"):
            ex = db.q1("SELECT stats, source FROM match_stats WHERE match_id=? AND side=?", (mid, side))
            if ex is None:
                db.execute("INSERT INTO match_stats(match_id,side,stats,source) VALUES(?,?,?,?)",
                           (mid, side, json.dumps(r["stats"][side]), "footballdata"))
        db.execute("DELETE FROM odds WHERE match_id=? AND source='footballdata'", (mid,))
        db.executemany("INSERT INTO odds(match_id,ts,phase,bookmaker,market,selection,line,period,odds,source) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       [(mid, r["kickoff"] - (86400 * 3 if ph == "open" else 60), ph, bk, mk, sel, line, "FT", v, "footballdata")
                        for ph, bk, mk, sel, line, v in r["odds"]])
        n += 1
    return n


def download(comp: str, season: str, client: PoliteClient | None = None) -> list[dict]:
    code = COMPETITIONS[comp].football_data_code
    if not code:
        return []
    c = client or PoliteClient(min_interval=1.0, headers={"Accept": "text/csv,*/*"})
    text = c.get_text(BASE.format(season=season, code=code), ttl=3600 * 12)
    return parse_csv(text, comp, season)


def backfill(n_seasons: int = 8, comps: list[str] | None = None) -> dict:
    comps = comps or [k for k, c in COMPETITIONS.items() if c.football_data_code]
    res = {}
    c = PoliteClient(min_interval=1.0, headers={"Accept": "text/csv,*/*"})
    for comp in comps:
        for s in seasons_back(n_seasons):
            try:
                rows = download(comp, s, c)
                res[f"{comp}-{s}"] = store(rows)
            except Exception as e:  # una temporada que falla no para el resto
                res[f"{comp}-{s}"] = f"error: {e}"
    return res
