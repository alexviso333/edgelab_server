"""Noticias de los dos equipos (Google Noticias, español e inglés) y lo que se puede sacar de ellas sin inventar:

  · posibles bajas: titular que nombra a un jugador de la plantilla junto a palabras de lesión, sanción o duda
  · cambio de entrenador: titular con el equipo y palabras de destitución o nombramiento
  · rotaciones: el entrenador anuncia descanso de titulares

Son pistas, no hechos: la web las enseña con el titular y el enlace, y pesan menos que las bajas oficiales.
"""
from __future__ import annotations

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from ..core import db

INJ = ["lesion", "lesionado", "baja", "descartado", "no jugara", "no estara", "se pierde", "duda", "molestias", "sancion", "sancionado",
       "injury", "injured", "ruled out", "doubt", "doubtful", "suspended", "suspension", "miss the", "out for"]
COACH = ["destituido", "destitucion", "cesado", "despedido", "nuevo entrenador", "nuevo tecnico", "interino", "sacked", "fired",
         "new manager", "new head coach", "appointed", "caretaker", "dimite", "dimision"]
# matices: no es lo mismo «descartado» que «tocado» que «vuelve a entrenar»
OUT = ["descartado", "no jugara", "no estara", "se pierde", "baja para", "baja de", "sancionado", "lesion grave", "operado",
       "ruled out", "will miss", "out for", "suspended", "set to miss", "sidelined"]
DOUBT = ["duda", "molestias", "tocado", "en el aire", "al margen", "trabajo especifico", "prueba de ultima hora", "evoluciona",
         "doubt", "doubtful", "fitness test", "late test", "knock", "race against time", "touch and go", "assessed"]
BACK = ["vuelve", "recupera", "recuperado", "alta medica", "recibe el alta", "disponible", "apto", "convocado", "entra en la convocatoria",
        "reaparece", "returns", "back in training", "available", "fit to play", "passed fit", "boost", "in contention"]
ROT = ["rotaciones", "rotara", "descanso a", "reservar", "rotation", "rest players", "rested"]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s)


def fetch(home: str, away: str, ttl: float = 1800) -> list[dict]:
    from .http import PoliteClient
    c = PoliteClient(min_interval=0.5)
    items, seen = [], set()
    qs = [(f'"{home}" "{away}"', "es"), (f'"{home}" (lesión OR baja OR alineación OR convocatoria OR entrenador)', "es"),
          (f'"{away}" (lesión OR baja OR alineación OR convocatoria OR entrenador)', "es"),
          (f'"{home}" (injury OR lineup OR manager)', "en"), (f'"{away}" (injury OR lineup OR manager)', "en")]
    for q, lang in qs:
        gl, ceid = ("ES", "ES:es") if lang == "es" else ("GB", "GB:en")
        url = f"https://news.google.com/rss/search?q={quote(q + ' when:5d')}&hl={lang}&gl={gl}&ceid={ceid}"
        try:
            root = ET.fromstring(c.get_text(url, ttl=ttl))
        except Exception:
            continue
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            if re.search(r"\b(baloncesto|basket\w*|euroliga|acb|nba|balonmano|futbol sala|femenino)\b", _norm(t)):
                continue      # otros deportes y secciones del club
            try:
                ts = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
            except Exception:
                ts = 0
            src = it.find("source")
            items.append({"title": t, "link": it.findtext("link"), "source": src.text if src is not None else "", "ts": ts, "lang": lang})
    items.sort(key=lambda x: -x["ts"])
    return items[:30]


def squad(team_id: int) -> list[dict]:
    """Jugadores que han jugado con el equipo en los últimos 4 meses (nombre completo y apellido)."""
    rows = db.q("SELECT DISTINCT p.id, p.name FROM players p JOIN player_stats ps ON ps.player_id=p.id "
                "LEFT JOIN matches m ON m.id=ps.match_id LEFT JOIN game_meta g ON g.match_id=ps.match_id "
                "WHERE ps.team_id=? AND COALESCE(m.kickoff, g.kickoff) > ?", (team_id, int(time.time()) - 120 * 86400))
    out = []
    for r in rows:
        nm = _norm(r["name"] or "")
        parts = [x for x in nm.split(" ") if len(x) >= 4]
        if parts:
            out.append({"id": r["id"], "name": r["name"], "keys": {nm, parts[-1]}})
    return out


def flags(items: list[dict], m: dict) -> list[dict]:
    out = []
    sq = {"H": squad(m["home_id"]), "A": squad(m["away_id"])}
    tn = {"H": _norm(m["home_name"]), "A": _norm(m["away_name"])}
    for it in items:
        t = _norm(it["title"])
        inj = any(k in t for k in INJ)
        coach = any(k in t for k in COACH)
        rot = any(k in t for k in ROT)
        for side in ("H", "A"):
            if inj or any(k in t for k in BACK):
                for p in sq[side]:
                    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in p["keys"]):
                        back = any(k in t for k in BACK)
                        lvl = "fuera" if any(k in t for k in OUT) else "duda" if any(k in t for k in DOUBT) else "duda"
                        if back and lvl != "fuera":
                            out.append({"kind": "vuelve", "side": side, "player_id": p["id"], "player": p["name"], "title": it["title"],
                                        "link": it["link"], "source": it["source"], "ts": it["ts"]})
                        else:
                            out.append({"kind": "baja", "level": lvl, "side": side, "player_id": p["id"], "player": p["name"],
                                        "title": it["title"], "link": it["link"], "source": it["source"], "ts": it["ts"]})
            short = tn[side].split(" ")[-1] if len(tn[side].split(" ")[-1]) >= 4 else tn[side]
            if coach and (tn[side] in t or short in t):
                out.append({"kind": "entrenador", "side": side, "title": it["title"], "link": it["link"], "source": it["source"], "ts": it["ts"]})
            if rot and (tn[side] in t or short in t):
                out.append({"kind": "rotaciones", "side": side, "title": it["title"], "link": it["link"], "source": it["source"], "ts": it["ts"]})
    seen, uniq = set(), []
    for f in out:
        k = (f["kind"], f["side"], f.get("player_id"))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def refresh(match_id: int, force: bool = False) -> dict:
    m = db.q1("SELECT id, home_id, away_id, home_name, away_name FROM matches WHERE id=?", (match_id,))
    if not m:
        return {}
    cur = db.kv_get(f"news:{match_id}") or {}
    if cur and not force and time.time() - (cur.get("ts") or 0) < 3 * 3600:
        return cur
    items = fetch(m["home_name"], m["away_name"])
    res = {"ts": int(time.time()), "items": items, "flags": flags(items, m)}
    db.kv_set(f"news:{match_id}", res)
    return res


def refresh_upcoming(hours_ahead: float = 48) -> int:
    now = int(time.time())
    n = 0
    for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + int(hours_ahead * 3600))):
        try:
            refresh(r["id"])
            n += 1
        except Exception as e:
            db.log_job("news", "error", f"{r['id']}: {e}")
    return n
