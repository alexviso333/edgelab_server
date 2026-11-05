"""Unificación de nombres de equipo entre fuentes (Statshub, football-data, Understat).

Estrategia:
1. Alias guardados en `team_alias` (aprendidos o manuales).
2. Diccionario manual de abreviaturas conocidas de football-data/Understat.
3. Similitud difusa sobre nombres normalizados, restringida a equipos del mismo país.
4. Si no hay equipo de Statshub aún (histórico antiguo), se crea uno con id negativo
   que luego se fusiona cuando aparezca el real.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from ..core import db

MANUAL = {
    # Inglaterra
    "man united": "Manchester United", "man utd": "Manchester United", "manchester utd": "Manchester United",
    "man city": "Manchester City", "nott'm forest": "Nottingham Forest", "nottingham": "Nottingham Forest",
    "newcastle": "Newcastle United", "wolves": "Wolverhampton", "wolverhampton wanderers": "Wolverhampton",
    "spurs": "Tottenham Hotspur", "tottenham": "Tottenham Hotspur", "west ham": "West Ham United",
    "brighton": "Brighton & Hove Albion", "leeds": "Leeds United", "sheffield united": "Sheffield United",
    "sheffield utd": "Sheffield United", "luton": "Luton Town", "ipswich": "Ipswich Town", "leicester": "Leicester City",
    "west brom": "West Bromwich Albion", "norwich": "Norwich City", "cardiff": "Cardiff City", "hull": "Hull City",
    "stoke": "Stoke City", "swansea": "Swansea City", "huddersfield": "Huddersfield Town", "coventry": "Coventry City",
    # España
    "ath madrid": "Atlético Madrid", "atletico madrid": "Atlético Madrid", "ath bilbao": "Athletic Club",
    "athletic bilbao": "Athletic Club", "betis": "Real Betis", "celta": "Celta Vigo", "espanol": "Espanyol",
    "sociedad": "Real Sociedad", "vallecano": "Rayo Vallecano", "alaves": "Deportivo Alavés", "la coruna": "Deportivo La Coruña",
    "sp gijon": "Sporting Gijón", "valladolid": "Real Valladolid", "huesca": "SD Huesca", "leganes": "Leganés",
    "cadiz": "Cádiz", "almeria": "UD Almería", "las palmas": "UD Las Palmas", "oviedo": "Real Oviedo", "elche": "Elche",
    "girona": "Girona FC", "mallorca": "RCD Mallorca", "osasuna": "Osasuna", "getafe": "Getafe", "granada": "Granada",
    "racing santander": "Racing de Santander", "santander": "Racing de Santander",
    # Italia
    "inter": "Inter", "milan": "Milan", "ac milan": "Milan", "verona": "Hellas Verona", "roma": "Roma",
    "spal": "SPAL", "parma": "Parma", "chievo": "Chievo", "spezia": "Spezia",
    # Alemania
    "m'gladbach": "Borussia M'gladbach", "monchengladbach": "Borussia M'gladbach", "dortmund": "Borussia Dortmund",
    "ein frankfurt": "Eintracht Frankfurt", "fc koln": "1. FC Köln", "koln": "1. FC Köln", "leverkusen": "Bayer 04 Leverkusen",
    "bayern munich": "FC Bayern München", "bayern": "FC Bayern München", "hertha": "Hertha BSC", "mainz": "1. FSV Mainz 05",
    "hoffenheim": "TSG Hoffenheim", "wolfsburg": "VfL Wolfsburg", "stuttgart": "VfB Stuttgart", "augsburg": "FC Augsburg",
    "freiburg": "SC Freiburg", "union berlin": "1. FC Union Berlin", "werder bremen": "SV Werder Bremen",
    "schalke 04": "FC Schalke 04", "bochum": "VfL Bochum 1848", "heidenheim": "1. FC Heidenheim", "darmstadt": "SV Darmstadt 98",
    "hamburg": "Hamburger SV", "st pauli": "FC St. Pauli", "rb leipzig": "RB Leipzig", "fortuna dusseldorf": "Fortuna Düsseldorf",
    "holstein kiel": "Holstein Kiel", "greuther furth": "SpVgg Greuther Fürth", "bielefeld": "Arminia Bielefeld",
    # Francia
    "paris sg": "Paris Saint-Germain", "psg": "Paris Saint-Germain", "st etienne": "Saint-Étienne", "marseille": "Olympique de Marseille",
    "lyon": "Olympique Lyonnais", "nice": "OGC Nice", "lille": "Lille", "monaco": "AS Monaco", "rennes": "Stade Rennais",
    "lens": "RC Lens", "strasbourg": "RC Strasbourg", "brest": "Stade Brestois", "reims": "Stade de Reims", "nantes": "FC Nantes",
    "montpellier": "Montpellier", "toulouse": "Toulouse", "le havre": "Le Havre", "auxerre": "Auxerre", "angers": "Angers",
    "lorient": "FC Lorient", "metz": "FC Metz", "paris fc": "Paris FC", "clermont": "Clermont Foot", "ajaccio": "AC Ajaccio",
    "troyes": "ESTAC Troyes", "bordeaux": "Bordeaux", "nimes": "Nîmes", "dijon": "Dijon",
}

STOP = {"fc", "cf", "ac", "sc", "ssc", "afc", "club", "de", "calcio", "1", "sv", "vfl", "vfb", "tsg", "rcd", "ud", "cd",
        "sd", "real", "as", "ogc", "rc", "stade", "olympique", "us", "fk", "the", "and", "hove", "albion", "united", "city"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9' ]", " ", s).strip()


def tokens(name: str) -> set[str]:
    return {t for t in norm(name).replace("'", "").split() if t and t not in STOP}


def similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if na == nb:
        return 1.0
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    sub = 0.9 if (na in nb or nb in na) and min(len(na), len(nb)) >= 4 else 0.0
    return max(jac, seq * 0.95, sub)


def resolve(name: str, source: str, country: str | None = None, create: bool = True) -> int | None:
    """Devuelve el id de equipo canónico para `name` de la fuente `source`."""
    r = db.q1("SELECT team_id FROM team_alias WHERE alias=? AND source=?", (name, source))
    if r:
        return r["team_id"]
    target = MANUAL.get(norm(name), name)
    cands = db.q("SELECT id, name, short, country FROM teams" + (" WHERE country=? OR country IS NULL" if country else ""),
                 (country,) if country else ())
    best, best_s = None, 0.0
    for c in cands:
        s = max(similarity(target, c["name"]), similarity(target, c["short"] or c["name"]))
        if s > best_s:
            best, best_s = c, s
    if best and best_s >= 0.82:
        db.execute("INSERT OR REPLACE INTO team_alias(alias,source,team_id) VALUES(?,?,?)", (name, source, best["id"]))
        return best["id"]
    if not create:
        return None
    mn = db.q1("SELECT MIN(id) AS v FROM teams")
    new_id = min(-1, (mn["v"] or 0) - 1) if mn and mn["v"] is not None else -1
    db.execute("INSERT INTO teams(id,name,short,slug,country) VALUES(?,?,?,?,?)", (new_id, target, target, None, country))
    db.execute("INSERT OR REPLACE INTO team_alias(alias,source,team_id) VALUES(?,?,?)", (name, source, new_id))
    return new_id


def merge_team(old_id: int, new_id: int) -> None:
    """Fusiona un equipo provisional (id negativo) con el real de Statshub."""
    if old_id == new_id:
        return
    with db.tx() as con:
        con.execute("UPDATE team_alias SET team_id=? WHERE team_id=?", (new_id, old_id))
        con.execute("UPDATE matches SET home_id=? WHERE home_id=?", (new_id, old_id))
        con.execute("UPDATE matches SET away_id=? WHERE away_id=?", (new_id, old_id))
        con.execute("DELETE FROM teams WHERE id=?", (old_id,))


def reconcile_provisional() -> int:
    """Intenta casar equipos provisionales con equipos reales de Statshub."""
    n = 0
    prov = db.q("SELECT id, name, country FROM teams WHERE id < 0")
    real = db.q("SELECT id, name, short, country FROM teams WHERE id > 0")
    for p in prov:
        best, best_s = None, 0.0
        for c in real:
            if p["country"] and c["country"] and norm(p["country"]) not in norm(c["country"]) and norm(c["country"]) not in norm(p["country"]):
                continue
            s = max(similarity(p["name"], c["name"]), similarity(p["name"], c["short"] or c["name"]))
            if s > best_s:
                best, best_s = c, s
        if best and best_s >= 0.85:
            merge_team(p["id"], best["id"])
            n += 1
    return n
