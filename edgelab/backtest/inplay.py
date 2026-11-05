"""Backtest de estrategias en directo.

Reproduce cada partido minuto a minuto con su cronología real (goles, rojas,
xG por tiro). La cuota en el momento de entrar sale de:
  1. cuotas en vivo grabadas (grabador propio o histórico de Betfair), si hay;
  2. si no, una cuota SIMULADA: el modelo en vivo estándar (sin xG en vivo)
     calibrado al cierre pre-partido, más el margen típico de bet365 en directo.
Con precios simulados, lo que se mide es si la frecuencia REAL de resultados en
ese estado difiere de lo que pondría un modelo de mercado estándar. Es la mejor
aproximación gratuita; cada resultado se marca con price_source para no
confundir simulado con real, y nada pasa a dinero real sin validarse en papel.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

from ..config import settings
from ..core import db
from ..core.markets import Outcome, Selection, apply_margin, selection_probs, settle
from ..models.inplay import InplayModel, InplayParams, LiveState
from ..strategies.dsl import FEATURE_OPS, Strategy


@dataclass
class MatchTL:
    id: int
    comp: str
    season: str
    kickoff: int
    lh: float
    la: float
    fav: str                 # H | A
    fav_prob: float
    hg: int
    ag: int
    events: list[dict]       # minute, kind, side, xg
    feats: dict


class PriceEngine:
    """Cuotas de entrada/cobertura: grabadas si existen, si no simuladas."""

    def __init__(self, params: InplayParams | None = None, margin: float | None = None,
                 market_params: InplayParams | None = None):
        # precio "de mercado" simulado = modelo en vivo ESTÁNDAR (lo que asume una casa);
        # probabilidad "verdadera" = nuestro modelo calibrado con los partidos reales.
        self.market_model = InplayModel(market_params or InplayParams())
        self.true_model = InplayModel(params or InplayParams())
        self.margin = settings.assumed_inplay_margin if margin is None else margin
        self._cache: dict = {}
        self._recorded: dict[int, pd.DataFrame] = {}

    def dist(self, m: MatchTL, st: LiveState, use_xg: bool = False) -> np.ndarray:
        key = (m.id, round(st.minute, 1), st.hg, st.ag, st.red_h, st.red_a,
               round(st.xg_h, 1) if use_xg and st.xg_h is not None else None,
               round(st.xg_a, 1) if use_xg and st.xg_a is not None else None)
        if key not in self._cache:
            if use_xg:
                self._cache[key] = self.true_model.final_distribution(m.lh, m.la, st)
            else:
                s2 = LiveState(st.minute, st.hg, st.ag, st.red_h, st.red_a, None, None, st.added_time)
                self._cache[key] = self.market_model.final_distribution(m.lh, m.la, s2)
            if len(self._cache) > 200000:
                self._cache.clear()
        return self._cache[key]

    def recorded(self, m: MatchTL, sel: Selection, minute: float, score: tuple[int, int]) -> float | None:
        if m.id not in self._recorded:
            rows = db.q("SELECT minute, market, selection, line, odds, score_h, score_a, bookmaker FROM odds "
                        "WHERE match_id=? AND phase='live' AND period='FT'", (m.id,))
            self._recorded[m.id] = pd.DataFrame(rows)
        df = self._recorded[m.id]
        if df.empty:
            return None
        sub = df[(df.market == sel.market) & (df.selection == sel.selection) & (df.score_h == score[0]) &
                 (df.score_a == score[1]) & ((df.minute - minute).abs() <= 1.5)]
        if sel.line is not None:
            sub = sub[(sub.line - sel.line).abs() < 1e-6]
        if sub.empty:
            return None
        pref = sub[sub.bookmaker == "Bet365"]
        return float((pref if not pref.empty else sub).iloc[0]["odds"])

    def price(self, m: MatchTL, sel: Selection, st: LiveState) -> tuple[float, str, float]:
        """(cuota, fuente, prob. justa del modelo de mercado)."""
        P = self.dist(m, st)
        w, p, l = selection_probs(sel, P)
        q = w / max(w + l, 1e-12)
        rec = self.recorded(m, sel, st.minute, (st.hg, st.ag))
        if rec:
            return rec, "real", q
        # mercado completo para repartir el margen de forma realista
        if sel.market == "1X2":
            probs = [float(np.tril(P, -1).sum()), float(np.trace(P)), float(np.triu(P, 1).sum())]
            idx = {"H": 0, "D": 1, "A": 2}[sel.selection]
            odds = apply_margin(probs, self.margin)[idx]
        else:
            odds = apply_margin([q, 1 - q], self.margin * 0.75)[0]   # 2 vías: margen algo menor
        odds = max(1.01, min(odds, 101.0))
        return round(odds, 2), "simulated", q


def load_timelines(comps: list[str] | None = None, min_season: str | None = None) -> list[MatchTL]:
    """Partidos terminados con cronología y λ pre-partido."""
    from ..models.features import best_market_probs
    sql = ("SELECT m.id, m.comp, m.season, m.kickoff, m.hg, m.ag, p.payload FROM matches m "
           "LEFT JOIN predictions p ON p.match_id=m.id AND p.model='prematch' "
           "WHERE m.status='finished' AND m.hg IS NOT NULL AND EXISTS (SELECT 1 FROM timeline t WHERE t.match_id=m.id)")
    rows = db.q(sql)
    import json
    tl = {}
    for e in db.q("SELECT match_id, minute, kind, side, xg FROM timeline ORDER BY match_id, minute"):
        tl.setdefault(e["match_id"], []).append(e)
    out = []
    for r in rows:
        if comps and r["comp"] not in comps:
            continue
        if min_season and (r["season"] or "") < min_season:
            continue
        pay = json.loads(r["payload"]) if r["payload"] else {}
        lh, la = pay.get("lh"), pay.get("la")
        ph, pa = pay.get("pH"), pay.get("pA")
        if lh is None or ph is None:
            continue
        evs = tl.get(r["id"], [])
        # coherencia: los goles de la cronología deben cuadrar con el marcador
        gh = sum(1 for e in evs if e["kind"] == "goal" and e["side"] == "H")
        ga = sum(1 for e in evs if e["kind"] == "goal" and e["side"] == "A")
        if (gh, ga) != (r["hg"], r["ag"]):
            continue
        out.append(MatchTL(r["id"], r["comp"], r["season"], r["kickoff"], lh, la, "H" if ph >= pa else "A",
                           max(ph, pa), r["hg"], r["ag"], evs, pay.get("feats", {})))
    return out


def _state_at(m: MatchTL, minute: float) -> dict:
    hg = ag = rh = ra = 0
    xh = xa = 0.0
    goals = []
    reds = []
    subs_def = []
    for e in m.events:
        if e["minute"] >= minute:
            break
        if e["kind"] == "goal":
            if e["side"] == "H":
                hg += 1
            else:
                ag += 1
            goals.append((e["minute"], e["side"], hg, ag))
        elif e["kind"] == "red":
            reds.append((e["minute"], e["side"]))
            if e["side"] == "H":
                rh += 1
            else:
                ra += 1
        elif e["kind"] == "shot":
            if e["side"] == "H":
                xh += e.get("xg") or 0
            else:
                xa += e.get("xg") or 0
        elif e["kind"] == "sub_def":
            subs_def.append((e["minute"], e["side"]))
    # los goles también cuentan su xG
    for e in m.events:
        if e["minute"] < minute and e["kind"] == "goal" and e.get("xg"):
            pass
    return {"hg": hg, "ag": ag, "rh": rh, "ra": ra, "xh": xh, "xa": xa, "goals": goals, "reds": reds, "subs_def": subs_def}


def _role_side(role: str, m: MatchTL, s: dict, entry: dict | None = None) -> str | None:
    lead = "H" if s["hg"] > s["ag"] else ("A" if s["ag"] > s["hg"] else None)
    dog = "A" if m.fav == "H" else "H"
    if role in ("H", "A", "D"):
        return role
    if role == "fav":
        return m.fav
    if role == "dog":
        return dog
    if role == "leader":
        return lead
    if role == "trailer":
        return None if lead is None else ("A" if lead == "H" else "H")
    if role == "original_leader":
        return entry.get("leader") if entry else lead
    if role == "opp_of_red":
        if s["reds"]:
            return "A" if s["reds"][-1][1] == "H" else "H"
        return None
    if role == "last_scorer":
        return s["goals"][-1][1] if s["goals"] else None
    if role == "xg_dominant":
        return "H" if s["xh"] > s["xa"] else "A"
    return None


def _trigger_ok(st: Strategy, m: MatchTL, s: dict, minute: float) -> bool:
    t = st.trigger
    lead = "H" if s["hg"] > s["ag"] else ("A" if s["ag"] > s["hg"] else None)
    margin = abs(s["hg"] - s["ag"])
    if "leader" in t:
        want = t["leader"]
        if want == "none" and lead is not None:
            return False
        if want == "fav" and lead != m.fav:
            return False
        if want == "dog" and (lead is None or lead == m.fav):
            return False
        if want in ("H", "A") and lead != want:
            return False
    if "margin" in t and not (t["margin"][0] <= margin <= t["margin"][1]):
        return False
    tg = s["hg"] + s["ag"]
    if "total_goals" in t and not (t["total_goals"][0] <= tg <= t["total_goals"][1]):
        return False
    if "red" in t:
        want = t["red"]
        has = bool(s["reds"])
        if want == "none" and has:
            return False
        if want == "any" and not has:
            return False
        if want in ("fav", "dog"):
            side = m.fav if want == "fav" else ("A" if m.fav == "H" else "H")
            if not any(r[1] == side for r in s["reds"]):
                return False
        if t.get("red_recent") and (not s["reds"] or minute - s["reds"][-1][0] > t["red_recent"]):
            return False
    if t.get("goal_recent"):
        if not s["goals"] or minute - s["goals"][-1][0] > t["goal_recent"]:
            return False
    if t.get("goal_window"):
        lo, hi = t["goal_window"]
        if not any(lo <= g[0] <= hi for g in s["goals"]):
            return False
    if t.get("goals_last10_min"):
        if sum(1 for g in s["goals"] if minute - g[0] <= 10) < t["goals_last10_min"]:
            return False
    xt = s["xh"] + s["xa"]
    if t.get("xg_total_max") is not None and xt > t["xg_total_max"]:
        return False
    if t.get("xg_total_min") is not None and xt < t["xg_total_min"]:
        return False
    if t.get("xg_gap_min") is not None:
        gap = abs(s["xh"] - s["xa"])
        dom = "H" if s["xh"] > s["xa"] else "A"
        if gap < t["xg_gap_min"]:
            return False
        if t.get("xg_team") == "trailer_or_level" and lead == dom:
            return False
    if t.get("comeback_from2"):
        # el líder llegó a ir 2 arriba
        mx = 0
        for g in s["goals"]:
            mx = max(mx, abs(g[2] - g[3]))
        if mx < 2:
            return False
    if t.get("sub_def_leader_recent"):
        if lead is None or not any(sd[1] == lead and minute - sd[0] <= t["sub_def_leader_recent"] for sd in s["subs_def"]):
            return False
    if t.get("added_min"):
        return False  # requiere dato de añadido anunciado (solo con grabador/Sofascore)
    return True


def _context_ok(st: Strategy, m: MatchTL) -> bool:
    c = st.context
    if c.get("comps") and m.comp not in c["comps"]:
        return False
    if c.get("fav_prob") and not (c["fav_prob"][0] <= m.fav_prob <= c["fav_prob"][1]):
        return False
    if c.get("fav_side") and m.fav != ("H" if c["fav_side"] == "home" else "A"):
        return False
    for f in c.get("filters", []):
        v = m.feats.get(f["f"])
        if f["f"] == "exp_goals":
            v = m.lh + m.la
        if v is None or not FEATURE_OPS[f["op"]](v, f["v"]):
            return False
    return True


def _make_sel(spec: dict, m: MatchTL, s: dict, entry: dict | None = None) -> Selection | None:
    mk = spec["market"]
    role = spec.get("selection")
    base = (s["hg"], s["ag"])
    if mk == "1X2":
        side = _role_side(role, m, s, entry)
        return Selection("1X2", side) if side else None
    if mk == "DC":
        if role in ("1X", "X2", "12"):
            return Selection("DC", role)
        if role == "fav_or_draw":
            return Selection("DC", "1X" if m.fav == "H" else "X2")
        if role == "dog_or_draw":
            return Selection("DC", "X2" if m.fav == "H" else "1X")
        return None
    if mk in ("AH", "DNB"):
        side = _role_side(role, m, s, entry)
        if not side or side == "D":
            return None
        return Selection("AH", side, float(spec.get("line", 0.0)), base_h=base[0], base_a=base[1])
    if mk == "OU":
        line = spec.get("line")
        if line is None:
            line = s["hg"] + s["ag"] + float(spec.get("line_offset", 0.5))
        return Selection("OU", role, float(line))
    return None


def run_match(st: Strategy, m: MatchTL, pe: PriceEngine, stake: float = 1.0) -> dict | None:
    if not _context_ok(st, m):
        return None
    lo, hi = st.trigger.get("minute", [1, 90])
    entry = None
    for minute in range(int(lo), int(hi) + 1):
        s = _state_at(m, minute)
        if _trigger_ok(st, m, s, minute):
            sel = _make_sel(st.market, m, s)
            if sel is None:
                continue
            lead = "H" if s["hg"] > s["ag"] else ("A" if s["ag"] > s["hg"] else None)
            ls = LiveState(minute, s["hg"], s["ag"], s["rh"], s["ra"], s["xh"], s["xa"])
            odds, src, q_mkt = pe.price(m, sel, ls)
            if st.price.get("min_odds") and odds < st.price["min_odds"]:
                continue
            if st.price.get("max_odds") and odds > st.price["max_odds"]:
                continue
            q_true = None
            if st.model_filter.get("min_edge") is not None:
                P = pe.dist(m, ls, use_xg=True)
                w, p, l = selection_probs(sel, P)
                q_true = w / max(w + l, 1e-12)
                if q_true * odds - 1 < st.model_filter["min_edge"]:
                    continue
            entry = {"minute": minute, "sel": sel, "odds": odds, "src": src, "leader": lead, "state": s,
                     "q_mkt": q_mkt, "q_true": q_true}
            break
    if entry is None:
        return None
    legs = [(entry["sel"], entry["odds"], stake, entry["minute"], "entry")]
    final = Outcome(m.hg, m.ag)
    # entrada escalonada
    if st.trigger.get("scale_in"):
        m2 = st.trigger["scale_in"]
        s2 = _state_at(m, m2)
        if (s2["hg"], s2["ag"]) == (entry["state"]["hg"], entry["state"]["ag"]):
            ls2 = LiveState(m2, s2["hg"], s2["ag"], s2["rh"], s2["ra"])
            o2, _, _ = pe.price(m, entry["sel"], ls2)
            legs[0] = (entry["sel"], entry["odds"], stake / 2, entry["minute"], "entry")
            legs.append((entry["sel"], o2, stake / 2, m2, "scale_in"))
    ex = st.exit or {"type": "hold"}
    exit_info = None
    if ex.get("type") in ("hedge", "greenup"):
        on = ex.get("on")
        for g in [e for e in m.events if e["kind"] == "goal" and e["minute"] >= entry["minute"]]:
            t2 = g["minute"] + 0.5
            s2 = _state_at(m, t2 + 0.01)
            trig = False
            if on == "equalizer" and s2["hg"] == s2["ag"]:
                trig = True
            elif on == "goal":
                trig = True
            elif on == "goal_for":
                trig = (g["side"] == entry["sel"].selection) or (entry["sel"].market == "DC")
            if not trig:
                continue
            if t2 >= 90:
                break
            ls2 = LiveState(t2, s2["hg"], s2["ag"], s2["rh"], s2["ra"])
            if ex["type"] == "hedge":
                hsel = _make_sel(ex["hedge"], m, s2, entry)
            else:
                hsel = _complement(entry["sel"], s2)
            if hsel is None:
                break
            ho, _, _ = pe.price(m, hsel, ls2)
            base_stake = sum(l[2] for l in legs)
            avg_odds = sum(l[1] * l[2] for l in legs) / base_stake
            size = ex.get("size", "zero")
            if size == "zero":
                hs = base_stake / max(ho - 1, 1e-6)
            elif size == "balance":
                hs = base_stake * avg_odds / ho
            elif size == "half":
                hs = 0.5 * base_stake * avg_odds / ho
            else:
                hs = 0.0
            legs.append((hsel, ho, round(hs, 4), t2, "hedge"))
            exit_info = {"minute": t2, "odds": ho, "stake": hs, "sel": hsel.label()}
            break
    pnl = 0.0
    for sel, o, sk, _, _ in legs:
        w, p = settle(sel, final)
        pnl += sk * (w * (o - 1) - (1 - w - p))
    staked = sum(l[2] for l in legs if l[4] != "hedge")
    return {"id": m.id, "kickoff": m.kickoff, "season": m.season, "comp": m.comp, "minute": entry["minute"],
            "selection": entry["sel"].label(), "odds": entry["odds"], "price_source": entry["src"],
            "q_mkt": entry["q_mkt"], "q_true": entry["q_true"], "stake": staked,
            "hedge_stake": sum(l[2] for l in legs if l[4] == "hedge"), "pnl": pnl, "exit": exit_info,
            "score_entry": f"{entry['state']['hg']}-{entry['state']['ag']}", "final": f"{m.hg}-{m.ag}",
            "win": 1.0 if pnl > 0 else 0.0, "lose": 1.0 if pnl < 0 else 0.0, "strategy_id": st.id}


def _complement(sel: Selection, s: dict) -> Selection | None:
    if sel.market == "1X2":
        return Selection("DC", {"H": "X2", "A": "1X", "D": "12"}[sel.selection])
    if sel.market == "DC":
        return Selection("1X2", {"12": "D", "1X": "A", "X2": "H"}[sel.selection])
    if sel.market == "OU":
        return Selection("OU", "U" if sel.selection == "O" else "O", sel.line)
    if sel.market == "AH":
        return Selection("AH", "A" if sel.selection == "H" else "H", -sel.line, base_h=sel.base_h, base_a=sel.base_a)
    return None


def run(st: Strategy, matches: list[MatchTL], pe: PriceEngine | None = None) -> pd.DataFrame:
    pe = pe or PriceEngine()
    rows = [r for r in (run_match(st, m, pe) for m in matches) if r]
    return pd.DataFrame(rows)
