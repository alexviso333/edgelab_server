"""Oddspedia: comparador de cuotas con bet365 (y otras 10 casas con licencia en España) ANTES y DURANTE el partido:
1X2, goles (todas las líneas, también las asiáticas .25/.75), hándicap asiático y córners. Es la alternativa a
Sofascore para las cuotas en directo, y no nos bloquea.

Su robots.txt permite expresamente la API (`Allow: /api/v1/*`); de la web solo se lee la lista de partidos del día.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from ..core import db
from .espn import _norm, _sim

BASE = "https://oddspedia.com"
Q = "bookmakerGeoCode=ES&bookmakerGeoState=&geoCode=ES&language=es"
BET365 = "126"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept": "application/json, text/html", "Accept-Language": "es-ES,es;q=0.9"}
_FAIL = {"until": 0.0, "n": 0}
_C: dict = {}


def _get(path: str, as_json: bool = True, timeout: int = 15):
    if time.time() < _FAIL["until"]:
        return None
    try:
        raw = urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_UA), timeout=timeout).read().decode("utf-8", "ignore")
        _FAIL["n"] = 0
        _FAIL["since"] = 0
        return json.loads(raw) if as_json else raw
    except Exception as e:
        _FAIL["n"] += 1
        _FAIL["since"] = _FAIL.get("since") or time.time()
        if _FAIL["n"] >= 3:
            _FAIL["until"] = time.time() + 600
            _FAIL["n"] = 0
            db.log_job("oddspedia", "error", f"pausa 10 min: {e}")
        return None


def _today_links() -> list[tuple[str, str]]:
    c = _C.get("links")
    if c and time.time() - c[0] < 1800:
        return c[1]
    html = _get("/es/futbol", as_json=False)
    links = sorted(set(re.findall(r'href="/es/futbol/([a-z0-9-]+)-(\d{4,})"', html or "")))
    if html:
        _C["links"] = (time.time(), links)
    return links


def match_key(m: dict) -> str | None:
    """Clave de Oddspedia del partido (se busca por los nombres en la lista del día y se comprueba con su ficha)."""
    key = f"odp_key:{m['id']}"
    c = db.kv_get(key)
    if c is not None:
        return c or None
    hn, an = _norm(m["home_name"]), _norm(m["away_name"])
    cands = []
    for slug, k in _today_links():
        s = slug.replace("-", " ")
        sc = (max(_sim(hn, s), _part(hn, s)) + max(_sim(an, s), _part(an, s))) / 2
        if sc >= 0.6:
            cands.append((sc, k))
    for sc, k in sorted(cands, reverse=True)[:3]:
        info = (_get(f"/api/v1/getMatchInfo?geoCode=ES&language=es&matchKey={k}") or {}).get("data") or {}
        if not info:
            continue
        ok = (_sim(m["home_name"], info.get("ht") or "") + _sim(m["away_name"], info.get("at") or "")) / 2 >= 0.62
        if ok:
            db.kv_set(key, k)
            return k
    return None


def _part(name: str, slug: str) -> float:
    toks = [t for t in name.split() if len(t) > 2]
    return 1.0 if toks and all(t in slug for t in toks) else 0.0


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def _bname(o: dict) -> str:
    return "Bet365" if str(o.get("bid")) == BET365 else (o.get("bookie_name") or f"odp{o.get('bid')}")


def parse_odds(base: dict, ou: dict | None, ah: dict | None, corners: dict | None, live: bool) -> list[dict]:
    """Filas {bookmaker, market, selection, line, odds} (bet365 = 'Bet365')."""
    part = "inplay" if live else "prematch"
    rows = []

    def groups(d):
        return ((d or {}).get("data") or {}).get(part) or []

    def final(g):
        return next((p for p in g.get("periods") or [] if p.get("name") in ("Final", "Full Time")), None)

    for g in groups(base):
        if g.get("id") == 1:                       # 1X2
            p = final(g)
            for o in (p or {}).get("odds") or []:
                for i, sel in ((1, "H"), (2, "D"), (3, "A")):
                    v = _f(o.get(f"o{i}"))
                    if v:
                        rows.append({"bookmaker": _bname(o), "market": "1X2", "selection": sel, "line": None, "odds": v})
    for d, gid, mk in ((ou, 4, "OU"), (ah, 3, "AH"), (corners, None, "CORNERS_OU")):
        for g in groups(d):
            if gid is not None and g.get("id") != gid:
                continue
            if gid is None and "esquina" not in str(g.get("name", "")).lower():
                continue
            p = final(g)
            if not p or not isinstance(p.get("odds"), dict):
                continue
            for kind in ("main", "alternative"):
                for ln in p["odds"].get(kind) or []:
                    name = str(ln.get("name") or "")
                    for o in (ln.get("odds") or {}).values():
                        o1, o2 = _f(o.get("o1")), _f(o.get("o2"))
                        if mk == "AH":
                            mm = re.match(r"([+-]?\d+(?:\.\d+)?)/([+-]?\d+(?:\.\d+)?)", name)
                            if not mm:
                                continue
                            lh, la = float(mm.group(1)), float(mm.group(2))
                            if o1:
                                rows.append({"bookmaker": _bname(o), "market": "AH", "selection": "H", "line": lh, "odds": o1})
                            if o2:
                                rows.append({"bookmaker": _bname(o), "market": "AH", "selection": "A", "line": la, "odds": o2})
                        else:
                            try:
                                line = float(name)
                            except ValueError:
                                continue
                            if o1:
                                rows.append({"bookmaker": _bname(o), "market": mk, "selection": "O", "line": line, "odds": o1})
                            if o2:
                                rows.append({"bookmaker": _bname(o), "market": mk, "selection": "U", "line": line, "odds": o2})
    # sin duplicados (main y alternative repiten la línea principal)
    seen, out = set(), []
    for r in rows:
        k = (r["bookmaker"], r["market"], r["selection"], r["line"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def corner_group_id(base: dict, live: bool) -> int | None:
    part = "inplay" if live else "prematch"
    for g in ((base or {}).get("data") or {}).get(part) or []:
        if "esquinas totales" in str(g.get("name", "")).lower() or str(g.get("name", "")).lower() == "total corners":
            return g.get("id")
    return None


def live_odds(m: dict) -> list[dict]:
    """Cuotas en directo de bet365 y del resto de casas (≈4 peticiones por partido)."""
    k = match_key(m)
    if not k:
        return []
    base = _get(f"/api/v1/getMatchOdds?{Q}&matchKey={k}&inplay=1")
    if not base or not ((base.get("data") or {}).get("inplay")):
        return []
    ou = _get(f"/api/v1/getMatchOdds?{Q}&matchKey={k}&inplay=1&oddGroupId=4")
    ah = _get(f"/api/v1/getMatchOdds?{Q}&matchKey={k}&inplay=1&oddGroupId=3")
    cg = corner_group_id(base, True)
    co = _get(f"/api/v1/getMatchOdds?{Q}&matchKey={k}&inplay=1&oddGroupId={cg}") if cg else None
    return parse_odds(base, ou, ah, co, True)


def store_live(mid: int, rows: list[dict], minute: float | None, score) -> int:
    now = int(time.time())
    out = [(mid, now, "live", r["bookmaker"], r["market"], r["selection"], r["line"], "FT", r["odds"], minute,
            score[0] if score else None, score[1] if score else None, "oddspedia") for r in rows]
    if out:
        db.executemany("INSERT INTO odds(match_id,ts,phase,bookmaker,market,selection,line,period,odds,minute,score_h,score_a,source) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def match_status(m: dict) -> str | None:
    """'canceled'/'postponed' si Oddspedia dice que no se juega (segunda opinión para no dar por jugados los aplazados)."""
    k = match_key(m)
    if not k:
        return None
    i = (_get(f"/api/v1/getMatchInfo?geoCode=ES&language=es&matchKey={k}") or {}).get("data") or {}
    txt = f"{i.get('special_status') or ''} {i.get('postmatch_status') or ''} {i.get('status_reason') or ''}".lower()
    if str(i.get("cancel") or "0") not in ("0", "", "None") or "cancel" in txt or "abandon" in txt:
        return "canceled"
    if "postpon" in txt or "aplaz" in txt:
        return "postponed"
    return None
