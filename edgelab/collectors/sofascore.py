"""Sofascore (www.sofascore.com/api/v1): mismos IDs de partido que Statshub.

Probado desde una conexión doméstica española (02/11/2026):
  /event/{id}                 estado, minuto (currentPeriodStartTimestamp), marcador, árbitro
  /event/{id}/incidents       goles con minuto, tarjetas (incl. segunda amarilla), cambios, añadido
  /event/{id}/shotmap         cada tiro con minuto y xG  → cronología de xG para el directo
  /event/{id}/statistics      xG, tiros, posesión, córners… por periodo
  /event/{id}/lineups         alineaciones confirmadas (y stats por jugador al terminar)
  /event/{id}/odds/1/all      cuotas pre-partido y EN DIRECTO (isLive=true): 1X2, doble oportunidad,
                              empate no válido, ambos marcan, goles, hándicap asiático, tarjetas, córners,
                              próximo gol. En España la casa que muestra NO es bet365 (excluida en ES):
                              se guarda como bookmaker "Sofascore".
  /sport/football/events/live todos los partidos en juego
La ruta antigua api.sofascore.com devuelve 403: se usa la de www.
"""
from __future__ import annotations

import re
import threading
import time

from ..config import settings
from ..core import db
from .http import Blocked, PoliteClient

BASE = "https://www.sofascore.com/api/v1"
BOOK = "Sofascore"


# Estado COMPARTIDO por todos los clientes de este proceso (cada hilo tenía el suyo: sin pausa ni ritmo comunes, se
# juntaban ráfagas de varios hilos y Sofascore nos bloqueaba una y otra vez). La pausa también se guarda en la base
# de datos para que el otro proceso (la web) la respete.
_G = {"until": 0.0, "pauses": 0, "streak": 0, "last": 0.0, "kv_t": 0.0, "last_ok": 0.0}
_GL = threading.Lock()
GAP = 5.0          # segundos mínimos entre peticiones, sumando todos los hilos (máximo 12 por minuto)


def pause_until(ts: float) -> None:
    db.kv_set("sofa_paused_until", int(ts))


def paused_left() -> int:
    t = db.kv_get("sofa_paused_until") or 0
    return max(0, int(t - time.time()))


def _paused() -> bool:
    now = time.time()
    if now - _G["kv_t"] > 15:               # lee la pausa común (puede ponerla el otro proceso)
        _G["kv_t"] = now
        try:
            _G["until"] = max(_G["until"], float(db.kv_get("sofa_paused_until") or 0))
        except Exception:
            pass
    return now < _G["until"]


class Sofascore:
    def __init__(self, client: PoliteClient | None = None):
        self.c = client or PoliteClient(min_interval=2.0, max_retries=1, headers={"Referer": "https://www.sofascore.com/",
                                                                   "Origin": "https://www.sofascore.com",
                                                                   "Accept": "*/*"})
        self.enabled = settings.sofascore_enabled

    @property
    def ok(self) -> bool:
        """Activo salvo pausa por bloqueo; la pausa se levanta sola (10, 20, 40… minutos, máximo 2 h)."""
        return bool(self.enabled) and not _paused()

    @ok.setter
    def ok(self, v: bool) -> None:
        self.enabled = v

    @property
    def paused_until(self) -> float:
        return _G["until"] if _paused() else 0.0

    def _get(self, path: str, ttl: float | None = None) -> dict:
        if not self.ok:
            return {}
        with _GL:                                   # una petición cada GAP segundos entre todos los hilos
            # al volver de una pausa, 30 min con la mitad de ritmo: si se vuelve a tope, nos bloquean otra vez enseguida
            gap = GAP * (2.5 if time.time() < _G.get("slow_until", 0) else 1)
            wait = _G["last"] + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            _G["last"] = time.time()
        try:
            d = self.c.get_json(f"{BASE}{path}", ttl=ttl)
            with _GL:
                _G["streak"] = 0
                if time.time() - _G["last_ok"] > 3600 and time.time() > _G.get("slow_until", 0) + 3600:
                    _G["pauses"] = 0                # una hora sin problemas: la próxima pausa vuelve a ser corta
                _G["last_ok"] = time.time()
            return d or {}
        except FileNotFoundError:
            return {}
        except Blocked:
            with _GL:
                _G["streak"] += 1
                if _G["streak"] >= 3 and time.time() >= _G["until"]:   # nos frenan: se para un rato, todos a la vez
                    mins = min(120, 10 * 2 ** _G["pauses"])
                    _G["pauses"] += 1
                    _G["streak"] = 0
                    _G["until"] = time.time() + mins * 60
                    _G["slow_until"] = _G["until"] + 1800
                    pause_until(_G["until"])
                    db.log_job("sofascore", "error", f"Sofascore nos frena: pausa de {mins} min y se reintenta solo")
            return {}
        except Exception:
            return {}

    def event(self, eid: int) -> dict:
        return self._get(f"/event/{eid}").get("event", {})

    def incidents(self, eid: int, ttl: float | None = None) -> list[dict]:
        return self._get(f"/event/{eid}/incidents", ttl).get("incidents", [])

    def shotmap(self, eid: int, ttl: float | None = None) -> list[dict]:
        return self._get(f"/event/{eid}/shotmap", ttl).get("shotmap", [])

    def statistics(self, eid: int) -> dict:
        return self._get(f"/event/{eid}/statistics")

    def lineups(self, eid: int, ttl: float | None = None) -> dict:
        return self._get(f"/event/{eid}/lineups", ttl)

    def player_heatmap(self, eid: int, pid: int, ttl: float | None = 60) -> list[dict]:
        return self._get(f"/event/{eid}/player/{pid}/heatmap", ttl).get("heatmap", [])

    def team_heatmap(self, eid: int, tid: int, ttl: float | None = 60) -> dict:
        return self._get(f"/event/{eid}/heatmap/{tid}", ttl)

    def odds(self, eid: int) -> dict:
        return self._get(f"/event/{eid}/odds/1/all")

    def live_events(self) -> list[dict]:
        return self._get("/sport/football/events/live").get("events", [])


# ---------------------------------------------------------------------------
# Parsers (formato verificado con respuestas reales)
# ---------------------------------------------------------------------------
def _minute(i: dict) -> float:
    t = float(i.get("time") or 0)
    added = i.get("addedTime") or 0
    if isinstance(added, (int, float)) and 0 < added < 900:
        t += float(added)
    return t


def parse_incidents(incs: list[dict]) -> list[dict]:
    out = []
    for i in incs:
        t = i.get("incidentType")
        side = "H" if i.get("isHome") else "A"
        m = _minute(i)
        if t == "goal":
            out.append({"minute": m, "kind": "goal", "side": side, "player": (i.get("player") or {}).get("name"),
                        "own": i.get("incidentClass") == "ownGoal"})
        elif t == "card" and i.get("incidentClass") in ("red", "yellowRed") and not i.get("rescinded"):
            out.append({"minute": m, "kind": "red", "side": side, "player": (i.get("player") or {}).get("name")})
        elif t == "card" and i.get("incidentClass") == "yellow" and not i.get("rescinded"):
            out.append({"minute": m, "kind": "yellow", "side": side, "player": (i.get("player") or {}).get("name")})
        elif t == "substitution":
            pin = (i.get("playerIn") or {}).get("position") or ""
            out.append({"minute": m, "kind": "sub_def" if pin == "D" else "sub_att" if pin == "F" else "sub",
                        "side": side, "player": (i.get("playerIn") or {}).get("name")})
        elif t == "injuryTime" and i.get("length"):
            out.append({"minute": m, "kind": "added", "side": "N", "player": str(i.get("length"))})
    out.sort(key=lambda e: e["minute"])
    return out


def parse_shotmap(shots: list[dict]) -> list[dict]:
    out = []
    for s in shots:
        out.append({"minute": _minute(s) - 0.01, "kind": "shot", "side": "H" if s.get("isHome") else "A",
                    "xg": float(s.get("xg") or 0.0), "player": (s.get("player") or {}).get("name")})
    return out


def build_timeline(incs: list[dict], shots: list[dict]) -> list[dict]:
    ev = [e for e in parse_incidents(incs) if e["kind"] in ("goal", "red", "sub_def", "sub_att", "added")]
    ev += parse_shotmap(shots)
    ev.sort(key=lambda e: (e["minute"], e["kind"] != "shot"))
    return ev


def store_timeline(match_id: int, events: list[dict]) -> int:
    db.execute("DELETE FROM timeline WHERE match_id=? AND source='sofascore'", (match_id,))
    db.executemany("INSERT INTO timeline(match_id,minute,kind,side,xg,player,source) VALUES(?,?,?,?,?,?,?)",
                   [(match_id, e["minute"], e["kind"], e["side"], e.get("xg"), e.get("player"), "sofascore") for e in events])
    return len(events)


def parse_live_stats(d: dict) -> dict:
    want = {"expectedGoals": "xg", "totalShotsOnGoal": "shots", "shotsOnGoal": "sot", "ballPossession": "poss",
            "cornerKicks": "corners", "bigChanceCreated": "big_chances", "fouls": "fouls", "yellowCards": "yellow",
            "redCards": "red", "touchesInOppBox": "box_touches", "finalThirdEntries": "final_third"}
    names = {"Expected goals": "xg", "Total shots": "shots", "Shots on target": "sot", "Ball possession": "poss",
             "Corner kicks": "corners", "Big chances": "big_chances", "Fouls": "fouls", "Yellow cards": "yellow",
             "Red cards": "red", "Touches in penalty area": "box_touches"}
    out = {"H": {}, "A": {}}
    for per in d.get("statistics", []):
        if per.get("period") != "ALL":
            continue
        for g in per.get("groups", []):
            for it in g.get("statisticsItems", []):
                k = want.get(it.get("key")) or names.get(it.get("name"))
                if not k:
                    continue
                for side, key in (("H", "homeValue"), ("A", "awayValue")):
                    try:
                        out[side][k] = float(it.get(key))
                    except (TypeError, ValueError):
                        pass
    return out


def frac_to_dec(f: str | None) -> float | None:
    if not f:
        return None
    try:
        a, b = f.split("/")
        return round(1 + float(a) / float(b), 4)
    except Exception:
        try:
            return float(f)
        except Exception:
            return None


_LINE_RE = re.compile(r"^\(([+-]?\d+(?:\.\d+)?)\)\s*(.*)$")


def parse_odds(data: dict, home: str | None = None, away: str | None = None) -> list[dict]:
    """→ filas {market, selection, line, period, live, odds, open}."""
    rows = []
    for m in data.get("markets", []):
        name = m.get("marketName", "")
        live = bool(m.get("isLive"))
        per = "HT" if m.get("marketPeriod") == "1st half" or name == "1st half" else "FT"
        group = m.get("choiceGroup")
        if m.get("suspended"):
            continue
        choices = m.get("choices", [])
        for idx, c in enumerate(choices):
            o = frac_to_dec(c.get("fractionalValue"))
            op = frac_to_dec(c.get("initialFractionalValue"))
            if not o or o <= 1:
                continue
            cn = c.get("name", "")
            mk = sel = None
            line = None
            if name in ("Full time", "1st half"):
                mk, sel = "1X2", {"1": "H", "X": "D", "2": "A"}.get(cn)
            elif name == "Double chance":
                mk, sel = "DC", cn if cn in ("1X", "X2", "12") else None
            elif name == "Draw no bet":
                mk, sel, line = "DNB", {"1": "H", "2": "A"}.get(cn), 0.0
            elif name == "Both teams to score":
                mk, sel = "BTTS", {"Yes": "Y", "No": "N"}.get(cn)
            elif name in ("Match goals", "Cards in match", "Corners 2-Way"):
                mk = {"Match goals": "OU", "Cards in match": "CARDS_OU", "Corners 2-Way": "CORNERS_OU"}[name]
                sel = {"Over": "O", "Under": "U"}.get(cn)
                try:
                    line = float(group)
                except (TypeError, ValueError):
                    sel = None
            elif name == "Asian handicap":
                mt = _LINE_RE.match(cn)
                if mt:
                    line = float(mt.group(1))
                    team = mt.group(2).strip().lower()
                    hl, al = (home or "").lower(), (away or "").lower()
                    if team and hl and (team in hl or hl in team):
                        sel = "H"
                    elif team and al and (team in al or al in team):
                        sel = "A"
                    else:
                        sel = "H" if idx == 0 else "A"
                    mk = "AH"
            elif name == "Next goal":
                mk = "NEXT_GOAL"
                sel = "NONE" if cn.lower().startswith("no goal") else ("H" if idx == 0 else "A")
            if not mk or not sel:
                continue
            rows.append({"market": mk, "selection": sel, "line": line, "period": per, "live": live,
                         "odds": o, "open": op, "bookmaker": BOOK})
    return rows


def store_odds(match_id: int, rows: list[dict], minute: float | None = None, score: tuple[int, int] | None = None) -> int:
    """Pre-partido: guarda apertura (initial) la primera vez y el precio actual como snap.
    En directo: phase='live' con minuto y marcador."""
    now = int(time.time())
    pre = [r for r in rows if not r["live"]]
    live = [r for r in rows if r["live"]]
    out = []
    if pre:
        have_open = db.q1("SELECT 1 FROM odds WHERE match_id=? AND bookmaker=? AND phase='open' LIMIT 1", (match_id, BOOK))
        if not have_open:
            out += [(match_id, now - 1, "open", BOOK, r["market"], r["selection"], r["line"], r["period"], r["open"] or r["odds"],
                     None, None, None, "sofascore") for r in pre]
        out += [(match_id, now, "snap", BOOK, r["market"], r["selection"], r["line"], r["period"], r["odds"], None, None, None,
                 "sofascore") for r in pre]
    if live:
        out += [(match_id, now, "live", BOOK, r["market"], r["selection"], r["line"], r["period"], r["odds"], minute,
                 score[0] if score else None, score[1] if score else None, "sofascore") for r in live]
    db.executemany("INSERT INTO odds(match_id,ts,phase,bookmaker,market,selection,line,period,odds,minute,score_h,score_a,source) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    if pre:
        db.upsert_odds_now(match_id, [(BOOK, r["market"], r["selection"], r["line"], r["period"], r["odds"]) for r in pre], now)
    return len(out)


def parse_lineups(d: dict) -> dict:
    """Alineaciones (confirmadas o jugadas) con stats por jugador si el partido terminó."""
    out = {"confirmed": bool(d.get("confirmed")), "H": [], "A": [], "formation": {}}
    for side, key in (("H", "home"), ("A", "away")):
        t = d.get(key) or {}
        out["formation"][side] = t.get("formation")
        for p in t.get("players", []):
            pl = p.get("player") or {}
            st = p.get("statistics") or {}
            out[side].append({"id": pl.get("id"), "name": pl.get("shortName") or pl.get("name"), "pos": p.get("position") or pl.get("position"),
                              "jersey": p.get("jerseyNumber") or pl.get("jerseyNumber"), "starter": not p.get("substitute", False),
                              "stats": st})
    return out


def minute_of(ev: dict, now: float | None = None) -> float | None:
    now = now or time.time()
    st = (ev.get("status") or {}).get("description", "").lower()
    start = (ev.get("time") or {}).get("currentPeriodStartTimestamp") or ev.get("currentPeriodStartTimestamp")
    if "halftime" in st or st == "ht":
        return 45.0
    if not start:
        return None
    el = (now - start) / 60
    if "2nd" in st:
        return 45 + el
    if "1st" in st:
        return el
    if "extra" in st or "overtime" in st:
        return 90 + el
    return None


def store_lineups(match_id: int, lu: dict) -> int:
    """Guarda la alineación de Sofascore. Confirmada → sustituye a la prevista de Statshub;
    no confirmada → solo si no había ninguna."""
    if not lu.get("H") or not lu.get("A"):
        return 0
    src = "confirmed" if lu.get("confirmed") else "predicted"
    if src == "predicted" and db.q1("SELECT 1 FROM lineups WHERE match_id=? AND source IN ('predicted','confirmed') LIMIT 1", (match_id,)):
        return 0
    now = int(time.time())
    db.execute("DELETE FROM lineups WHERE match_id=? AND source IN ('predicted','confirmed')", (match_id,))
    rows = []
    for side in ("H", "A"):
        for p in lu[side]:
            if not p.get("id"):
                continue
            try:
                jersey = int(p.get("jersey")) if p.get("jersey") not in (None, "") else None
            except (TypeError, ValueError):
                jersey = None
            rows.append((match_id, side, p["id"], p.get("name"), p.get("pos"), jersey, 1 if p.get("starter") else 0, src,
                         lu["formation"].get(side), now))
    db.executemany("INSERT OR REPLACE INTO lineups(match_id,side,player_id,name,position,jersey,starter,source,formation,ts) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    if src == "confirmed":
        db.execute("UPDATE matches SET lineup_status='confirmed' WHERE id=?", (match_id,))
    return len(rows)
