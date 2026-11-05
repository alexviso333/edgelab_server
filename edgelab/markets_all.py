"""Todos los mercados de bet365 que trae Statshub (≈130), incluidos los de jugador.

Para cada mercado "raro" (los que no son 1X2/hándicap/goles…) devuelve una etiqueta en
castellano, el grupo donde se enseña y la probabilidad del modelo cuando se puede calcular:

  - con la matriz de marcadores: goles exactos, por mitades, márgenes, descanso/final,
    mitad con más goles, quién marca primero/último, porterías a cero, par/impar…
  - con los modelos de conteo (binomial negativa por equipo): córners, tarjetas, tiros,
    faltas, fueras de juego, entradas, paradas, «quién tendrá más…», bandas, carreras…
  - con el modelo de jugadores (ritmos por 90' de sus últimos partidos × minutos
    esperados × goles esperados del equipo hoy): goleador, primero/último, 2+, asistencias,
    tiros, tiros a puerta, entradas, faltas, faltas recibidas, tarjeta, paradas…

Lo que no se puede modelar (pases de un jugador, carreras de córners…) se muestra igual con
las cuotas de todas las casas y su consenso cuando hay casas suficientes.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from functools import lru_cache

import numpy as np
from scipy.stats import nbinom, poisson

from .core import db

T_TOT = 94.0   # minutos efectivos para repartir ritmos de córners


@lru_cache(maxsize=50000)
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


@lru_cache(maxsize=50000)
def _tok(s: str) -> frozenset:
    return frozenset(t for t in norm(s).split() if len(t) > 1)


# ---------------------------------------------------------------------------
# Modelo de jugadores
# ---------------------------------------------------------------------------
STATS = ("goals", "xg", "assists", "xa", "shots", "sot", "tackles", "fouls", "was_fouled", "yellow", "saves")


def player_model(m: dict, lam: dict, card_mult: float = 1.0) -> dict:
    if not m.get("home_id") or not lam:
        return {}
    since = m["kickoff"] - 240 * 86400
    lu = db.q("SELECT side, player_id, name, position, starter, source FROM lineups WHERE match_id=? AND source IN ('confirmed','predicted')",
              (m["id"],))
    have_lu = bool(lu)
    cand: dict = {}
    if have_lu:
        for r in lu:
            cand[r["player_id"]] = {"side": r["side"], "name": r["name"], "pos": r["position"], "starter": bool(r["starter"]),
                                    "lu": r["source"]}
    else:
        for side, tid in (("H", m["home_id"]), ("A", m["away_id"])):
            for r in db.q("SELECT DISTINCT ps.player_id, p.name, p.position FROM player_stats ps JOIN matches mt ON mt.id=ps.match_id "
                          "JOIN players p ON p.id=ps.player_id WHERE ps.team_id=? AND mt.kickoff BETWEEN ? AND ?",
                          (tid, since, m["kickoff"])):
                cand[r["player_id"]] = {"side": side, "name": r["name"], "pos": r["position"], "starter": None, "lu": None}
    if not cand:
        return {}
    ids = ",".join(str(int(i)) for i in cand)
    rows = db.q(f"SELECT ps.*, mt.kickoff FROM player_stats ps JOIN matches mt ON mt.id=ps.match_id WHERE ps.player_id IN ({ids}) "
                f"AND mt.kickoff BETWEEN ? AND ? ORDER BY mt.kickoff DESC", (since, m["kickoff"]))
    by: dict = {}
    for r in rows:
        by.setdefault(r["player_id"], []).append(r)
    # minutos de cada equipo en sus últimos 6 partidos (para esperar minutos sin alineación)
    team_last = {}
    for side, tid in (("H", m["home_id"]), ("A", m["away_id"])):
        last = db.q("SELECT id FROM matches WHERE (home_id=? OR away_id=?) AND status='finished' AND kickoff<? ORDER BY kickoff DESC LIMIT 6",
                    (tid, tid, m["kickoff"]))
        team_last[side] = [x["id"] for x in last]
    out = {}
    for pid, c in cand.items():
        rs = [r for r in by.get(pid, []) if (r["minutes"] or 0) > 0][:15]
        mins = sum(r["minutes"] or 0 for r in rs)
        if mins < 90:
            continue
        rate = {}
        from .postmatch import bias_factor, entity_factor
        for k in STATS:
            v = sum((r[k] or 0) for r in rs if r[k] is not None)
            rate[k] = v / mins * 90 * bias_factor("player", k) * entity_factor(f"pl:{pid}:{k}")
        starts = [r["minutes"] for r in rs if r["starter"]]
        if have_lu:
            exp_min = (min(88.0, float(np.mean(starts))) if starts else 75.0) if c["starter"] else 14.0
        else:
            played = {r["match_id"]: r["minutes"] or 0 for r in by.get(pid, [])}
            tl = team_last[c["side"]]
            exp_min = float(np.mean([played.get(x, 0) for x in tl])) if tl else 0.0
        if exp_min < 3:
            continue
        out[pid] = {**c, "id": pid, "n": len(rs), "minutes": mins, "rate": rate, "exp_min": exp_min}
    heat_adjust(m, out, have_lu)
    # λ de gol y asistencia coherentes con los goles esperados del equipo
    for side, lt in (("H", lam["lh"]), ("A", lam["la"])):
        ps = [p for p in out.values() if p["side"] == side]
        raw_g = {p["id"]: (0.65 * p["rate"]["xg"] + 0.35 * p["rate"]["goals"]) * p["exp_min"] / 90 * p.get("box_f", 1.0) for p in ps}
        raw_a = {p["id"]: (0.6 * p["rate"]["xa"] + 0.4 * p["rate"]["assists"]) * p["exp_min"] / 90 for p in ps}
        sg, sa = sum(raw_g.values()), sum(raw_a.values())
        tg = 0.96 * lt
        kg = tg / sg if sg > 0 else 1.0
        ka = 0.72 * lt / sa if sa > 0 else 1.0
        if not have_lu:          # sin once la suma por jugador no es fiable: solo se recorta si se pasa
            kg, ka = min(kg, 1.0) if sg > tg else max(min(kg, 1.6), 0.7), min(ka, 1.0) if sa > 0.72 * lt else max(min(ka, 1.6), 0.7)
        opp = lam["la"] if side == "H" else lam["lh"]
        from .postmatch import entity_factor
        opp_id = m["away_id"] if side == "H" else m["home_id"]
        # lo aprendido del rival (partido a partido): cuántos tiros deja, cuántas faltas hace, cuántas provoca
        f_sh, f_sot = entity_factor(f"op:{opp_id}:SHOTS"), entity_factor(f"op:{opp_id}:SOT")
        f_fouls, f_fouled = entity_factor(f"op:{opp_id}:FOULS"), entity_factor(f"tm:{opp_id}:FOULS")
        try:
            opp_f = {"header": opp_concede(opp_id, m["kickoff"], "header"), "outside the box": opp_concede(opp_id, m["kickoff"], "outside")}
        except Exception:
            opp_f = {}
        for p in ps:
            f = p["exp_min"] / 90
            p["lam"] = {"goal": raw_g[p["id"]] * kg, "assist": raw_a[p["id"]] * ka,
                        "shots": p["rate"]["shots"] * f * math.sqrt(max(0.5, min(2.0, lt / 1.35))) * f_sh * p.get("box_f", 1.0),
                        "sot": p["rate"]["sot"] * f * math.sqrt(max(0.5, min(2.0, lt / 1.35))) * f_sot * p.get("box_f", 1.0),
                        "tackles": p["rate"]["tackles"] * f, "fouls": p["rate"]["fouls"] * f * f_fouls * p.get("duel_fouls", 1.0),
                        "fouled": p["rate"]["was_fouled"] * f * f_fouled * p.get("duel_fouled", 1.0),
                        "cards": p["rate"]["yellow"] * f * card_mult,
                        "saves": p["rate"]["saves"] * f * max(0.6, min(1.8, opp / 1.35))}
            p["conf"] = "media" if (have_lu and p["n"] >= 6 and p["minutes"] >= 450) else "baja"
            try:          # cabeza / fuera del área: lo suyo (Statshub), sus duelos aéreos y lo que concede este rival
                p["shares"] = player_shares(p["id"], p.get("pos"), m["kickoff"])
                p["opp_f"] = opp_f
            except Exception:
                p["shares"] = None
    # nombres completos (la alineación prevista trae "J. Simões"; las cuotas, "João Simões")
    if out:
        ids2 = ",".join(str(int(i)) for i in out)
        for r in db.q(f"SELECT id, name FROM players WHERE id IN ({ids2})"):
            if r["name"]:
                out[r["id"]]["names"] = list({out[r["id"]]["name"], r["name"]})
    idx: dict = {}
    for p in out.values():
        for nm in p.get("names", [p["name"]]):
            idx.setdefault(_tok(nm), p)
    return {"players": out, "idx": idx, "lineup": have_lu}



def heat_adjust(m: dict, out: dict, have_lu: bool) -> None:
    """Mapas de calor en el precio de las apuestas de jugador.
    - box_f: si en sus últimos 3 partidos pisa el área rival más (o menos) que antes — cambio de rol, de sistema, de
      posición —, sus tiros y su parte de los goles del equipo lo notan antes que su media de 15 partidos.
    - duel_fouled / duel_fouls: con el once conocido, su par real es el rival con el que más terreno comparte (no el de
      la etiqueta de posición: un lateral que vive arriba se cruza con el lateral rival, no con el extremo). Si ese par
      hace muchas faltas (o recibe muchas), sus faltas recibidas (o cometidas) suben, en proporción a lo que comparten."""
    from .collectors.tactics import box_trend, heat_profiles
    try:
        prof = heat_profiles(list(out), m["kickoff"])
    except Exception:
        return
    for pid, p in out.items():
        bt = box_trend(prof.get(pid))
        if bt is not None:
            p["box_f"] = float(min(1.18, max(0.85, bt ** 0.6)))
            p["box_trend"] = round(bt, 3)
        if pid in prof:
            b, t = prof[pid]["box"]
            p["box_share"] = round(b / t, 3) if t else None
    if not have_lu:
        return
    try:
        from .insights import _overlap, league_refs, pos_info
        refs = league_refs()["pos"]
    except Exception:
        return
    st = [p for p in out.values() if p.get("starter") and (pos_info(p.get("pos")) or ("GK",))[0] != "GK" and p["id"] in prof]
    for p in st:
        best, ov = None, 0.0
        for q in st:
            if q["side"] == p["side"]:
                continue
            o = _overlap(prof[p["id"]]["grid"].tolist(), prof[q["id"]]["grid"].tolist()) or 0.0
            if o > ov:
                best, ov = q, o
        if not best or ov < 0.2:
            continue
        qi = pos_info(best.get("pos"))
        lg = refs.get(qi[0], {}) if qi else {}
        w = min(1.0, ov / 0.45) * 0.35
        p["duel"] = {"id": best["id"], "name": best["name"], "overlap": round(ov, 3)}
        if lg.get("fouls"):
            p["duel_fouled"] = float(min(1.15, max(0.88, (best["rate"]["fouls"] / lg["fouls"]) ** w)))
        if lg.get("was_fouled"):
            p["duel_fouls"] = float(min(1.15, max(0.88, (best["rate"]["was_fouled"] / lg["was_fouled"]) ** w)))

def _name_keys(nm: str):
    toks = norm(nm).split()
    if not toks:
        return None, None
    return toks[-1], toks[0][0]


def find_player(pm: dict, name: str):
    if not pm:
        return None
    memo = pm.setdefault("_memo", {})
    if name not in memo:
        memo[name] = _find_player(pm, name)
    return memo[name]


def _find_player(pm: dict, name: str):
    """Empareja el nombre de la cuota con un jugador: nombre completo, apellido + inicial, apellido único u orden invertido."""
    t = _tok(name)
    p = pm["idx"].get(t)
    if p:
        return p
    last, ini = _name_keys(name)
    if not last:
        return None
    allp = list(pm["players"].values())
    def names(q):
        return q.get("names", [q["name"]])
    c1 = [q for q in allp if any(_name_keys(n) == (last, ini) for n in names(q))]
    if len(c1) == 1:
        return c1[0]
    c2 = [q for q in allp if any(norm(n).split()[-1:] == [last] for n in names(q))]
    if len(c2) == 1:
        return c2[0]
    for q in allp:          # orden invertido ("Isak Alexander") o nombre compuesto
        if any(len(_tok(n) & t) >= 2 for n in names(q)):
            return q
    first = norm(name).split()[0]
    c3 = [q for q in allp if any(norm(n).split()[-1:] == [first] for n in names(q))]
    return c3[0] if len(c3) == 1 else None


# ---------------------------------------------------------------------------
def _p_over(mu: float, line: float, r: float | None = None) -> tuple[float, float, float]:
    """(gana Over, push, pierde) para un conteo con media mu (Poisson o binomial negativa con r)."""
    k = np.arange(0, int(max(60, mu * 4 + 20)))
    pmf = poisson.pmf(k, max(mu, 1e-9)) if not r else nbinom.pmf(k, r, r / (r + max(mu, 1e-9)))
    q = round(line * 4) / 4
    parts = [q - 0.25, q + 0.25] if abs(q * 2 - round(q * 2)) > 1e-9 else [q]
    w = p = 0.0
    for L in parts:
        w += pmf[k > L + 1e-9].sum()
        p += pmf[np.abs(k - L) < 1e-9].sum()
    w, p = w / len(parts), p / len(parts)
    return float(w), float(p), float(max(0.0, 1 - w - p))


# qué parte de sus goles / tiros a puerta son de cabeza, desde fuera del área, de penalti… según su puesto
# (un central marca casi la mitad de cabeza; un mediapunta tira mucho desde fuera). Medias de las grandes ligas.
_SHARES = {
    "goal": {"header": {"D": 0.42, "M": 0.11, "F": 0.18}, "outside the box": {"D": 0.10, "M": 0.30, "F": 0.11},
             "penalty": {"D": 0.03, "M": 0.12, "F": 0.11}, "free kick": {"D": 0.01, "M": 0.05, "F": 0.02},
             "left foot": {"D": 0.22, "M": 0.30, "F": 0.30}, "right foot": {"D": 0.36, "M": 0.59, "F": 0.52}},
    "sot": {"header": {"D": 0.38, "M": 0.09, "F": 0.15}, "outside the box": {"D": 0.25, "M": 0.40, "F": 0.18}},
}


AER_AVG = {"D": 2.0, "M": 0.8, "F": 1.6}      # duelos aéreos ganados por 90' de media según el puesto


def _pos(p) -> str:
    pos = str((p or {}).get("pos") or "M")[:1].upper()
    return pos if pos in ("D", "M", "F") else "M"


def player_shares(pid: int, pos: str | None, before: int, n: int = 15) -> dict | None:
    """Qué parte de SUS tiros a puerta son de cabeza y desde fuera del área (Statshub, partido a partido), partiendo
    de la media de su puesto (ajustada por los duelos aéreos que gana) y acercándose a lo suyo cuantos más datos hay."""
    rows = db.q("SELECT ps.sot, ps.minutes, ps.aerial_won, ps.position, pe.head_sot, pe.out_sot FROM player_stats ps "
                "JOIN matches mt ON mt.id=ps.match_id LEFT JOIN player_extra pe ON pe.player_id=ps.player_id AND pe.match_id=ps.match_id "
                "WHERE ps.player_id=? AND mt.kickoff<? AND ps.minutes>0 ORDER BY mt.kickoff DESC LIMIT ?", (pid, before, n))
    if not rows:
        return None
    ps_ = pos or next((r["position"] for r in rows if r["position"]), None)
    P = _pos({"pos": ps_})
    mins = sum(r["minutes"] or 0 for r in rows)
    aer = sum(r["aerial_won"] or 0 for r in rows if r["aerial_won"] is not None)
    f_aer = min(1.6, max(0.6, ((aer / mins * 90) / AER_AVG[P]) ** 0.5)) if mins >= 270 else 1.0
    prior_h = _SHARES["sot"]["header"][P] * f_aer
    prior_o = _SHARES["sot"]["outside the box"][P]
    ex = [r for r in rows if r["head_sot"] is not None]
    sot = sum(r["sot"] or 0 for r in ex)
    K = 5.0
    sh_h = (sum(r["head_sot"] or 0 for r in ex) + K * prior_h) / (sot + K)
    sh_o = (sum(r["out_sot"] or 0 for r in ex) + K * prior_o) / (sot + K)
    return {"header": round(min(0.9, max(0.01, sh_h)), 4), "outside the box": round(min(0.9, max(0.01, sh_o)), 4),
            "n": len(ex), "sot_n": sot, "aer": round(f_aer, 2), "pos": P}


def opp_concede(opp_id: int, before: int, how: str) -> float:
    """Cuánto concede el rival en remates de cabeza (o desde fuera) a puerta frente a la media (partido a partido)."""
    col = "head_sot" if how == "header" else "out_sot"
    avg = db.kv_get(f"pex_avg:{col}")
    if not avg or time.time() - avg.get("ts", 0) > 86400:
        r = db.q1(f"SELECT AVG(t) AS a, COUNT(*) AS n FROM (SELECT SUM(pe.{col}) AS t FROM player_extra pe JOIN player_stats ps "
                  f"ON ps.player_id=pe.player_id AND ps.match_id=pe.match_id GROUP BY pe.match_id, ps.team_id)")
        avg = {"a": (r or {}).get("a") or 0, "n": (r or {}).get("n") or 0, "ts": int(time.time())}
        db.kv_set(f"pex_avg:{col}", avg)
    if not avg["a"] or avg["n"] < 50:
        return 1.0
    rows = db.q(f"SELECT pe.match_id, SUM(pe.{col}) AS t FROM player_extra pe JOIN player_stats ps ON ps.player_id=pe.player_id "
                f"AND ps.match_id=pe.match_id JOIN matches mt ON mt.id=pe.match_id WHERE (mt.home_id=? OR mt.away_id=?) AND ps.team_id!=? "
                f"AND mt.kickoff<? GROUP BY pe.match_id ORDER BY mt.kickoff DESC LIMIT 15", (opp_id, opp_id, opp_id, before))
    n = len(rows)
    tot = sum(r["t"] or 0 for r in rows)
    return round(min(1.4, max(0.7, (tot + 6 * avg["a"]) / ((n + 6) * avg["a"]))), 3)


def _corner_f(c, p) -> float:
    """Más córners esperados de su equipo → más balones para rematar de cabeza."""
    try:
        f = (c.cm.get("CORNERS") or {}).get("model")
        mu = float(f.mu_h if p.get("side") == "H" else f.mu_a)
        return min(1.25, max(0.8, (mu / 5.0) ** 0.5))
    except Exception:
        return 1.0


def _share(p: dict, how: str, sot: bool = False, cf: float = 1.0) -> float:
    P = _pos(p)
    t = _SHARES["sot" if sot else "goal"].get(how) or {}
    base = t.get(P, 0.15)
    sh = (p or {}).get("shares")
    if not sh or how not in ("header", "outside the box"):
        return base
    s_sot = sh[how]
    v = s_sot if sot else base * min(3.0, max(0.33, s_sot / _SHARES["sot"][how][P]))
    v *= ((p.get("opp_f") or {}).get(how, 1.0))
    if how == "header":
        v *= cf
    return min(0.85, max(0.005, v))


def _ou(sel_over: bool, r):
    w, p, l = r
    return (w, p, l) if sel_over else (l, p, w)


def _yes(pr: float) -> tuple[float, float, float]:
    pr = float(min(max(pr, 0.0), 1.0))
    return pr, 0.0, 1 - pr


_OU = re.compile(r"^(over|under)\s+([\d.]+)", re.I)
_PLAYER_OU = re.compile(r"^(.*?)\s*(?:\((?:booked)\)\s*)?(over|under)\s+([\d.]+)$", re.I)


class Ctx:
    def __init__(self, m, lam, mats, cm, share):
        self.m, self.lam, self.mats, self.cm, self.share = m, lam, mats, cm, share
        self._pm = None
        self._sim = None
        self.exact = False
        self.first_scorer = self.last_scorer = None
        self.team_first = self.team_last = None
        self.home, self.away = m["home_name"], m["away_name"]

    @property
    def pm(self):
        if self._pm is None:
            cards = self.cm.get("CARDS", {}).get("ref", 1.0) if self.cm else 1.0
            try:
                self._pm = player_model(self.m, self.lam, cards)
            except Exception:
                self._pm = {}
        return self._pm

    @property
    def sim(self):
        if self._sim is None and self.lam:
            from .plans import Sim
            self._sim = Sim(self.lam["lh"], self.lam["la"], n=3000, seed=11)
        return self._sim

    def side_of(self, label: str):
        n = norm(label)
        h, a = norm(self.home), norm(self.away)
        if n in ("1", "home") or (h and (n == h or n.startswith(h) or h.startswith(n))):
            return "H"
        if n in ("2", "away") or (a and (n == a or n.startswith(a) or a.startswith(n))):
            return "A"
        if n in ("draw", "tie", "x"):
            return "D"
        th, ta, tn = _tok(self.home), _tok(self.away), _tok(label)
        if tn and tn & th and not tn & ta:
            return "H"
        if tn and tn & ta and not tn & th:
            return "A"
        return None


# ---------------------------------------------------------------------------
def _grid(P):
    n = P.shape[0]
    I = np.arange(n)[:, None] + np.zeros((1, n), int)
    J = np.zeros((n, 1), int) + np.arange(n)[None, :]
    return I, J


def _count_pmf(cf, side=None, scale=1.0):
    k = np.arange(80)
    if getattr(cf, "exact", False):          # liquidación: el valor real, sin incertidumbre
        if scale != 1.0:
            return None
        v = cf.mu_h if side == "H" else cf.mu_a if side == "A" else cf.mu_h + cf.mu_a
        out = np.zeros(80); out[min(int(round(v)), 79)] = 1.0
        return out
    mu_h, mu_a = cf.mu_h * scale, cf.mu_a * scale
    def one(mu):
        r = cf.disp
        return poisson.pmf(k, mu) if r > 500 else nbinom.pmf(k, r, r / (r + mu))
    if side == "H":
        return one(mu_h)
    if side == "A":
        return one(mu_a)
    t = np.convolve(one(mu_h), one(mu_a))[:80]
    return t / t.sum()


COUNT_NAMES = {"Corners": "CORNERS", "Bookings": "CARDS", "Cards": "CARDS", "Fouls": "FOULS", "Offsides": "OFFSIDES",
               "Tackles": "TACKLES", "Shots": "SHOTS", "Shots on Target": "SOT"}
WORD = {"CORNERS": "córners", "CARDS": "tarjetas", "FOULS": "faltas", "OFFSIDES": "fueras de juego", "TACKLES": "entradas",
        "SHOTS": "tiros", "SOT": "tiros a puerta", "SAVES": "paradas"}
HALF_SHARE_COUNT = {"CORNERS": 0.46, "CARDS": 0.38, "FOULS": 0.48, "OFFSIDES": 0.5, "TACKLES": 0.5, "SHOTS": 0.47, "SOT": 0.47, "SAVES": 0.47}


def price_raw(name: str, per: str, label: str, line, c: Ctx) -> dict:
    """→ {'label', 'group', 'probs' (w,p,l) o None, 'conf', 'player'}"""
    base = name.replace(" HT", "").replace(" 2H", "")
    lab = label.strip()
    L = None if line is None else float(line)
    H, A = c.home, c.away
    team = {"H": H, "A": A}
    P = c.mats.get("2H" if "2nd Half" in name else per) if c.mats else None
    out = {"label": f"{name}: {lab}", "group": "especiales", "probs": None, "conf": None, "player": None}
    mo = _OU.match(lab)

    # ---------------- goles: matriz de marcadores
    if base in ("Exact Total Goals", "Exact 1st Half Goals", "Exact 2nd Half Goals") or re.match(r"^(Home|Away) Team Exact Goals$", base):
        side = None
        txt = lab
        if base.startswith(("Home", "Away")):
            side = "H" if base.startswith("Home") else "A"
            txt = lab.split(" - ")[-1]
        mt = re.match(r"^(\d+)(\+)?\s*(?:or more\s*)?goals?", txt, re.I)
        if not mt:
            return out
        k, plus = int(mt.group(1)), bool(mt.group(2)) or "more" in txt.lower()
        per2 = "HT" if "1st Half" in base else "2H" if "2nd Half" in base else per
        PP = c.mats.get(per2) if c.mats else None
        who = f"{team[side]}: " if side else ""
        when = {"HT": " en la 1ª parte", "2H": " en la 2ª parte"}.get(per2, "")
        txt_k = f"{k} o más goles" if plus else f"exactamente {k} gol{'' if k == 1 else 'es'}"
        lbl_k = f"{who}{txt_k}{when}"
        out.update(label=lbl_k[0].upper() + lbl_k[1:], group="goles" if per2 == "FT" else "mitades")
        if PP is not None:
            I, J = _grid(PP)
            v = I if side == "H" else J if side == "A" else I + J
            out["probs"] = _yes(PP[(v >= k) if plus else (v == k)].sum())
        return out
    if base == "Number of Goals In Match":
        out.update(group="goles", label=f"Goles en el partido: {lab}")
        if P is not None:
            I, J = _grid(P); t = I + J
            l2 = lab.lower()
            mt = re.findall(r"\d+", l2)
            if "or" in l2 and len(mt) == 2:
                out.update(label=f"{mt[0]} o {mt[1]} goles", probs=_yes(P[(t >= int(mt[0])) & (t <= int(mt[1]))].sum()))
            elif l2.startswith("over") and mt:
                out.update(label=f"Más de {mt[0]} goles", probs=_yes(P[t > int(mt[0])].sum()))
            elif l2.startswith("under") and mt:
                out.update(label=f"Menos de {mt[0]} goles", probs=_yes(P[t < int(mt[0])].sum()))
        return out
    if base == "Teams to Score":
        out["group"] = "goles"
        if P is not None:
            I, J = _grid(P)
            l2 = lab.lower()
            if l2.startswith("both"):
                out.update(label="Marcan los dos", probs=_yes(P[(I > 0) & (J > 0)].sum()))
            elif "only" in l2:
                s = c.side_of(lab.replace(" Only", "").replace(" only", ""))
                if s in ("H", "A"):
                    out.update(label=f"Solo marca {team[s]}", probs=_yes(P[((I > 0) & (J == 0)) if s == "H" else ((J > 0) & (I == 0))].sum()))
            elif "no" in l2 or "neither" in l2:
                out.update(label="No marca nadie", probs=_yes(P[0, 0]))
        return out
    if base in ("Clean Sheet Home", "Clean Sheet Away"):
        s = "H" if "Home" in base else "A"
        yes = lab.lower().startswith("y")
        out.update(group="goles", label=f"{team[s]} deja la portería a cero: {'sí' if yes else 'no'}")
        if P is not None:
            I, J = _grid(P)
            pc = P[(J == 0) if s == "H" else (I == 0)].sum()
            out["probs"] = _yes(pc if yes else 1 - pc)
        return out
    if base in ("First Team To Score", "Last Team To Score"):
        s = c.side_of(lab)
        first = base.startswith("First")
        out.update(group="goles")
        if s in ("H", "A"):
            out["label"] = f"{'Primer' if first else 'Último'} gol: {team[s]}"
            if c.lam:
                lt = c.lam["lh"] + c.lam["la"]
                lx = c.lam["lh"] if s == "H" else c.lam["la"]
                p00 = float(c.mats["FT"][0, 0]) if c.mats else math.exp(-lt)
                out["probs"] = _yes(lx / lt * (1 - p00))
        elif "no" in lab.lower():
            out["label"] = "No hay goles"
            if c.mats:
                out["probs"] = _yes(c.mats["FT"][0, 0])
        return out
    if base == "Winning Margin":
        out["group"] = "resultado"
        mt = re.match(r"^(\d)(\+)?\s*\((1|2)\)$", lab)
        if P is not None:
            I, J = _grid(P); d = I - J
            if mt:
                k, plus, s = int(mt.group(1)), bool(mt.group(2)), "H" if mt.group(3) == "1" else "A"
                dd = d if s == "H" else -d
                out.update(label=f"{team[s]} gana por {k}{' o más' if plus else ''}", probs=_yes(P[(dd >= k) if plus else (dd == k)].sum()))
            elif "no goal" in lab.lower():
                out.update(label="Empate 0-0", probs=_yes(P[0, 0]))
            elif "draw" in lab.lower():
                out.update(label="Empate con goles", probs=_yes(P[(d == 0) & (I > 0)].sum()))
        return out
    if base == "Half Time / Full Time":
        out["group"] = "mitades"
        parts = [x.strip().lower() for x in lab.split("/")]
        mp = {"home": "H", "draw": "D", "away": "A", "1": "H", "x": "D", "2": "A"}
        if len(parts) == 2 and parts[0] in mp and parts[1] in mp and c.mats:
            a, b = mp[parts[0]], mp[parts[1]]
            nm = {"H": H, "D": "empate", "A": A}
            out["label"] = f"Descanso: {nm[a]} · Final: {nm[b]}"
            out["probs"] = _yes(_htft(c.mats["HT"], c.mats["2H"], a, b))
        return out
    if base == "Half With Most Goals" or base in ("Home Team Highest Scoring Half", "Away Team Highest Scoring Half"):
        out["group"] = "mitades"
        side = "H" if base.startswith("Home") else "A" if base.startswith("Away") else None
        who = f"{team[side]}: " if side else ""
        l2 = lab.lower()
        tgt = "1" if l2.startswith("1") else "2" if l2.startswith("2") else "T"
        out["label"] = f"{who}{'la 1ª parte' if tgt == '1' else 'la 2ª parte' if tgt == '2' else 'las dos igual'} con más goles"
        if c.mats:
            h1 = _marg(c.mats["HT"], side)
            h2 = _marg(c.mats["2H"], side)
            jt = np.outer(h1, h2)
            n = len(h1)
            D = np.subtract.outer(np.arange(n), np.arange(n))
            out["probs"] = _yes(jt[D > 0].sum() if tgt == "1" else jt[D < 0].sum() if tgt == "2" else jt[D == 0].sum())
        return out
    if base.startswith("European Handicap"):
        s = c.side_of(lab)
        out.update(group="handicap", label=f"Hándicap europeo{' (1ª parte)' if per == 'HT' else ''}: {'empate' if s == 'D' else team.get(s, lab)}",
                   nocons=True)
        return out          # la casa no dice la línea: solo cuotas
    if base.startswith("Odd/Even"):
        side = "H" if base.endswith("Home") else "A" if base.endswith("Away") else None
        odd = lab.lower().startswith("odd")
        out.update(group="goles", label=f"{team[side] + ': ' if side else ''}goles {'impares' if odd else 'pares'}{' (1ª parte)' if per == 'HT' else ''}")
        if P is not None:
            I, J = _grid(P); v = I if side == "H" else J if side == "A" else I + J
            out["probs"] = _yes(P[(v % 2) == (1 if odd else 0)].sum())
        return out
    if base.startswith("First 10 Minutes") and mo and not c.exact:
        over = mo.group(1).lower() == "over"
        out.update(group="goles", label=f"{'Hay' if over else 'No hay'} gol en los 10 primeros minutos")
        if c.lam:
            from .models.inplay import time_profile, InplayParams
            w = time_profile(InplayParams.from_dict(db.load_params("inplay")))
            lt = (c.lam["lh"] + c.lam["la"]) * float(w[:10].sum())
            p0 = math.exp(-lt)
            out["probs"] = _yes(1 - p0 if over else p0)
        return out
    # ---------------- conteos de equipo
    cm = c.cm or {}
    cnt = _count_market(base, lab, L, per, c)
    if cnt is not None:
        return cnt

    # ---------------- jugadores
    pl = _player_market(base, lab, L, c)
    if pl is not None:
        return pl
    out["nocons"] = True      # mercado sin reglas conocidas: ni modelo ni consenso
    return out


def _marg(Pm, side):
    if side == "H":
        return Pm.sum(axis=1)
    if side == "A":
        return Pm.sum(axis=0)
    n = Pm.shape[0]
    t = np.zeros(2 * n)
    for i in range(n):
        for j in range(n):
            t[i + j] += Pm[i, j]
    return t


def _htft(PH, P2, a, b):
    n = PH.shape[0]
    tot = 0.0
    for i in range(n):
        for j in range(n):
            ph = PH[i, j]
            if ph < 1e-7:
                continue
            ra = "H" if i > j else "A" if j > i else "D"
            if ra != a:
                continue
            I, J = _grid(P2)
            fi, fj = I + i, J + j
            mask = (fi > fj) if b == "H" else (fi < fj) if b == "A" else (fi == fj)
            tot += ph * P2[mask].sum()
    return tot


def _count_market(base, lab, L, per, c: Ctx):
    cm = c.cm or {}
    H, A = c.home, c.away
    team = {"H": H, "A": A}
    mo = _OU.match(lab)
    code = side = None
    m3 = re.match(r"^Most (Corners|Cards|Fouls|Offsides|Shots on Target|Shots|Tackles)( 3-Way)?$", base)
    if m3:
        code = COUNT_NAMES[m3.group(1)]
        s = c.side_of(lab)
        out = {"group": _grp(code), "label": f"Más {WORD[code]}: {'empate' if s == 'D' else team.get(s, lab)}", "probs": None, "conf": None, "player": None,
               "meta": {"t": "most", "code": code, "side": s}}
        if code in cm and s:
            a, b = _count_pmf(cm[code]["model"], "H"), _count_pmf(cm[code]["model"], "A")
            j = np.outer(a, b); n = len(a)
            D = np.subtract.outer(np.arange(n), np.arange(n))
            out["probs"] = _yes(j[D > 0].sum() if s == "H" else j[D < 0].sum() if s == "A" else j[D == 0].sum())
        return out
    if base == "Corners Race":
        # carrera a X córners: quién llega antes a X (o nadie). Los córners caen uno a uno; cada uno es del local con
        # probabilidad μH/(μH+μA) y el total del partido sigue el modelo de córners
        mt = re.search(r"\(([+-]?\d+)\)", lab)
        X = abs(int(mt.group(1))) if mt else (int(abs(L)) if L else None)
        s = "N" if "neither" in lab.lower() or "ninguno" in lab.lower() else c.side_of(re.sub(r"\s*\(.*$", "", lab))
        out = {"group": "corners", "label": f"Carrera a {X} córners: {'ninguno llega' if s == 'N' else team.get(s, lab)}",
               "probs": None, "conf": None, "player": None}
        if c.exact or "CORNERS" not in cm or not X or s not in ("H", "A", "N"):
            return out
        f = cm["CORNERS"]["model"]
        q = f.mu_h / max(f.mu_h + f.mu_a, 1e-9)
        tot = _count_pmf(f)
        ph = pa = 0.0
        # P(llega primero el local con exactamente n córners jugados) = el n-ésimo es suyo, lleva X-1 en n-1 y el rival < X
        from math import comb
        for n in range(X, min(len(tot), 2 * X)):
            p_ge = tot[n:].sum()               # que haya al menos n córners en el partido
            if p_ge <= 0:
                break
            k = n - X                          # córners del rival en ese momento (< X)
            ph += p_ge * comb(n - 1, X - 1) * q ** X * (1 - q) ** k
            pa += p_ge * comb(n - 1, X - 1) * (1 - q) ** X * q ** k
        pn = max(0.0, 1 - ph - pa)
        out["probs"] = _yes(ph if s == "H" else pa if s == "A" else pn)
        return out
    if base == "Corner Handicap":
        # hándicap de córners a 3 vías: «Equipo (-1)» gana si sus córners − 1 superan a los del rival; «Tie (1)» empate
        # con esa ventaja (córners local − visitante = 1)
        mt = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", lab)
        h = float(mt.group(1)) if mt else (L or 0.0)
        is_tie = lab.lower().startswith(("tie", "draw", "empate"))
        s = "D" if is_tie else c.side_of(re.sub(r"\s*\(.*$", "", lab))
        nm = "empate" if s == "D" else team.get(s, lab)
        out = {"group": "corners", "label": f"Hándicap de córners: {nm} ({'+' if h > 0 and not is_tie else ''}{h:g})",
               "probs": None, "conf": None, "player": None}
        if "CORNERS" not in cm or s not in ("H", "A", "D"):
            return out
        f = cm["CORNERS"]["model"]
        a, b = _count_pmf(f, "H"), _count_pmf(f, "A")
        n = min(len(a), len(b))
        j = np.outer(a[:n], b[:n])
        D = np.subtract.outer(np.arange(n), np.arange(n)).astype(float)       # local − visitante
        if s == "H":
            out["probs"] = _yes(j[D + h > 0].sum())
        elif s == "A":
            out["probs"] = _yes(j[-D + h > 0].sum())
        else:
            out["probs"] = _yes(j[np.abs(D - h) < 1e-9].sum())
        return out
    if base in ("First Match Corner", "Last Match Corner") and c.exact:
        return None
    if base == "Time of First Corner" and c.exact:
        return None
    if base in ("First Match Corner", "Last Match Corner"):
        s = "H" if lab.strip() == "1" else "A" if lab.strip() == "2" else c.side_of(lab)
        out = {"group": "corners", "label": f"{'Primer' if base.startswith('First') else 'Último'} córner: {team.get(s, lab)}", "probs": None, "conf": None, "player": None}
        if "CORNERS" in cm and s in ("H", "A"):
            f = cm["CORNERS"]["model"]
            out["probs"] = _yes((f.mu_h if s == "H" else f.mu_a) / (f.mu_h + f.mu_a))
        return out
    if base == "Time of First Corner":
        mt = re.search(r"(\d+):", lab)
        neg = lab.lower().startswith("no")
        out = {"group": "corners", "label": f"{'Ningún' if neg else 'Algún'} córner antes del minuto {mt.group(1) if mt else '?'}", "probs": None, "conf": None, "player": None}
        if "CORNERS" in cm and mt:
            f = cm["CORNERS"]["model"]
            p0 = math.exp(-(f.mu_h + f.mu_a) * int(mt.group(1)) / T_TOT)
            out["probs"] = _yes(p0 if neg else 1 - p0)
        return out
    if base in ("Total Corners", "Total Corners 3-Way", "Total Cards 3-Way"):
        code = "CARDS" if "Cards" in base else "CORNERS"
        out = {"group": _grp(code), "label": f"{WORD[code].capitalize()}: {lab}", "probs": None, "conf": None, "player": None}
        if code in cm:
            pmf = _count_pmf(cm[code]["model"])
            k = np.arange(len(pmf))
            l2 = lab.lower()
            nums = [int(x) for x in re.findall(r"\d+", l2)]
            if " - " in lab and len(nums) == 2:
                out.update(label=f"Entre {nums[0]} y {nums[1]} {WORD[code]}", probs=_yes(pmf[(k >= nums[0]) & (k <= nums[1])].sum()))
            elif l2.startswith("exactly") and nums:
                out.update(label=f"Exactamente {nums[0]} {WORD[code]}", probs=_yes(pmf[k == nums[0]].sum()))
            elif (l2.startswith("over") or l2.endswith("+") or "more" in l2) and nums:
                strict = l2.startswith("over")
                out.update(label=f"{'Más de' if strict else 'Al menos'} {nums[0]} {WORD[code]}", probs=_yes(pmf[(k > nums[0]) if strict else (k >= nums[0])].sum()))
            elif (l2.startswith("under") or "fewer" in l2 or "less" in l2) and nums:
                out.update(label=f"Menos de {nums[0]} {WORD[code]}", probs=_yes(pmf[k < nums[0]].sum()))
        return out
    if base == "Multicorners" and mo and c.exact:
        return None
    if base == "Multicorners" and mo:
        over = mo.group(1).lower() == "over"
        out = {"group": "corners", "label": f"Multicórners (córners 1ª × 2ª parte) {'más' if over else 'menos'} de {mo.group(2)}", "probs": None, "conf": None, "player": None}
        if "CORNERS" in cm:
            f = cm["CORNERS"]["model"]
            a = _count_pmf(f, None, 0.46)[:30]; b = _count_pmf(f, None, 0.54)[:30]
            prod = np.multiply.outer(np.arange(30), np.arange(30))
            j = np.outer(a, b)
            pov = j[prod > float(mo.group(2))].sum()
            out["probs"] = _yes(pov if over else 1 - pov)
        return out
    # totales genéricos: "<Algo> Totals/Total ... Home/Away" con Over/Under
    if mo:
        b2 = base
        if b2.startswith("1st Half "):
            b2 = b2.replace("1st Half ", "")
            per = "HT"
        side = "H" if b2.endswith(" Home") else "A" if b2.endswith(" Away") else None
        core = re.sub(r"\s+(Home|Away)$", "", b2)
        core = re.sub(r"^(Total|Team|Alternative|Match)\s+", "", core)
        core = re.sub(r"\s+Totals?$", "", core)
        if core == "Goalkeeper Saves":
            code = "SAVES"
        else:
            code = COUNT_NAMES.get(core)
        if not code:
            return None
        over = mo.group(1).lower() == "over"
        Lx = float(mo.group(2)) if L is None else L
        when = " (1ª parte)" if per == "HT" else ""
        out = {"group": _grp(code), "label": f"{team[side] + ': ' if side else ''}{'más' if over else 'menos'} de {str(Lx).rstrip('0').rstrip('.').replace('.', ',')} {WORD[code]}{when}",
               "probs": None, "conf": None, "player": None,
               "meta": {"t": "count", "code": code, "side": side, "over": over, "line": Lx, "per": per}}
        out["label"] = out["label"][0].upper() + out["label"][1:]
        if code in cm:
            f = cm[code]["model"]
            sc = HALF_SHARE_COUNT.get(code, 0.47) if per == "HT" else 1.0
            pmf = _count_pmf(f, side, sc)
            if pmf is None:
                return out
            k = np.arange(len(pmf))
            q = round(Lx * 4) / 4
            parts = [q - 0.25, q + 0.25] if abs(q * 2 - round(q * 2)) > 1e-9 else [q]
            w = p = 0.0
            for Lp in parts:
                w += pmf[k > Lp + 1e-9].sum(); p += pmf[np.abs(k - Lp) < 1e-9].sum()
            w, p = w / len(parts), p / len(parts)
            out["probs"] = _ou(over, (float(w), float(p), float(max(0, 1 - w - p))))
        return out
    return None


def _grp(code):
    return {"CORNERS": "corners", "CARDS": "tarjetas"}.get(code, "estadisticas")


PLAYER_MARKETS = {
    "Anytime Goalscorer": ("goal", "marca"), "To Score 2+ Goals": ("goal", "marca"), "To Score 3+ Goals": ("goal", "marca"),
    "First Goalscorer": ("first", "marca el primer gol"), "Last Goalscorer": ("last", "marca el último gol"),
    "Player Shots": ("shots", "tiros"), "Player Shots On Target": ("sot", "tiros a puerta"),
    "Player Shots on Target Outside Box": ("sot_out", "tiros a puerta desde fuera del área"),
    "Player Headed Shots on Target": ("sot_head", "remates de cabeza a puerta"),
    "Player Tackles": ("tackles", "entradas"), "Player Fouls": ("fouls", "faltas cometidas"),
    "Player Fouls Won": ("fouled", "faltas recibidas"), "Player To Be Fouled": ("fouled", "faltas recibidas"),
    "Player Assists": ("assist", "asistencias"), "Player To Assist": ("assist", "asistencias"),
    "To Be Shown A Card": ("cards", "tarjeta"), "Player to be Booked": ("cards", "tarjeta"),
    "Player Passes": ("passes", "pases"), "Goalkeeper Saves": ("saves", "paradas"),
}
DISP = {"shots": 6.0, "sot": 8.0, "tackles": 6.0, "fouls": 8.0, "fouled": 6.0, "saves": 10.0}


def _player_market(base, lab, L, c: Ctx):
    kind = None
    name = None
    line = L
    extra = None
    if base in PLAYER_MARKETS:
        kind, word = PLAYER_MARKETS[base]
        mt = _PLAYER_OU.match(lab)
        if mt:
            name, ou, ln = mt.group(1).strip(), mt.group(2).lower(), float(mt.group(3))
            line = ln
        else:
            mt2 = re.match(r"^(.*?)\s*\((?:score or assist|booked)\)", lab, re.I)
            name, ou = (mt2.group(1) if mt2 else re.sub(r"\s*\(.*$", "", lab)).strip(), "over"
            line = 0.5 if line is None else line
        over = ou == "over"
    elif base == "Team Goalscorer":
        mt = re.match(r"^(.*?)\s*\((First|Last)\)", lab, re.I)
        if not mt:
            return None
        name, kind, over, line = mt.group(1).strip(), "team_" + mt.group(2).lower(), True, 0.5
        word = f"marca el {'primer' if kind == 'team_first' else 'último'} gol de su equipo"
    elif base == "Player Cards":
        # bet365 mezcla tres apuestas distintas: «(Booked)» le sacan tarjeta, «(1st Card)» la PRIMERA tarjeta del
        # partido es suya y «(Sent Off)» le expulsan. Antes las tres se calculaban como «le sacan tarjeta» (mal)
        low = lab.lower()
        name = re.sub(r"\s*\(.*$", "", lab).strip()
        if "1st card" in low or "first card" in low:
            kind, over, line, word = "first_card", True, 0.5, "ve la primera tarjeta del partido"
        elif "sent off" in low or "red" in low:
            kind, over, line, word = "red", True, 0.5, "es expulsado"
        else:
            kind, over, line, word = "cards", True, 0.5, "tarjeta"
    elif base == "Multi Scorers":
        mt = re.match(r"^(.*?)\s*\((\d) or More\)", lab, re.I)
        if not mt:
            return None
        name, kind, over, line, word = mt.group(1).strip(), "goal", True, int(mt.group(2)) - 0.5, "marca"
    elif base == "Player To Score or Assist":
        # «(Score or Assist) (2)» = 2+ goles o asistencias; «(Score) (2)» = 2+ goles; «(Assist) (2)» = 2+ asistencias
        # (antes todas se calculaban como «marca o asiste» 1+: precios equivocados)
        mt = re.match(r"^(.*?)\s*\((Score or Assist|Score|Assist)\)\s*(?:\((\d+)\))?", lab, re.I)
        if mt:
            name = mt.group(1).strip()
            what = mt.group(2).lower()
            nreq = int(mt.group(3)) if mt.group(3) else 1
        else:
            mt = re.match(r"^(.*?)\s*over", lab, re.I)
            name, what, nreq = (mt.group(1) if mt else lab).strip(), "score or assist", 1
        over, line = True, nreq - 0.5
        kind = {"score or assist": "goal_assist", "score": "goal", "assist": "assist"}[what]
        word = {"goal_assist": "marca o da una asistencia", "goal": "marca", "assist": "da una asistencia"}[kind]
        if nreq > 1:
            word = {"goal_assist": f"suma {nreq} o más entre goles y asistencias", "goal": f"marca {nreq} o más goles",
                    "assist": f"da {nreq} o más asistencias"}[kind]
    elif base == "Goal Method":
        mt = re.match(r"^(.*?)\s*\((Header|Outside the Box|Penalty|Free Kick|Left Foot|Right Foot)\)", lab, re.I)
        if not mt:
            return None
        name, kind, over, line = mt.group(1).strip(), "goal", True, 0.5
        extra = mt.group(2).lower()
        word = {"header": "marca de cabeza", "outside the box": "marca desde fuera del área", "penalty": "marca de penalti",
                "free kick": "marca de falta directa", "left foot": "marca con la izquierda", "right foot": "marca con la derecha"}[mt.group(2).lower()]
    else:
        return None
    if not name:
        return None
    p = find_player(c.pm, name)
    disp_name = name
    if kind in ("goal",) and line is not None and abs(line - 0.5) < 1e-9 and over:
        lbl = f"{disp_name} {word}" + (" (en cualquier momento)" if base == "Anytime Goalscorer" else "")
    elif kind in ("first", "last", "team_first", "team_last", "cards", "goal_assist", "first_card", "red", "assist") and base in (
            "Player Cards", "Player To Score or Assist", "Team Goalscorer", "First Goalscorer", "Last Goalscorer", "To Be Shown A Card",
            "Player to be Booked") or kind in ("first", "last", "team_first", "team_last", "cards", "goal_assist"):
        lbl = f"{disp_name} {word}"
    elif kind == "goal":
        lbl = f"{disp_name} marca {int(line + 0.5)} o más goles"
    else:
        lbl = f"{disp_name}: {'más' if over else 'menos'} de {str(line).rstrip('0').rstrip('.').replace('.', ',')} {word}"
    if p and extra:
        lbl = f"{disp_name} {word}"
    out = {"group": "jugadores", "label": lbl, "probs": None, "conf": "baja", "player": disp_name,
           "team": None if not p else p["side"],
           "meta": {"t": "player", "kind": kind, "over": bool(over), "line": line, "pid": None if not p else p.get("id"),
                    "extra": base if extra else None}}
    if not p or "lam" not in p:
        return out
    if p.get("exact"):                      # liquidación con lo que pasó de verdad
        a = p["lam"]
        def cnt(v):
            if v is None:
                return None
            d = v - line
            w = 1.0 if d > 1e-9 else 0.0
            pu = 1.0 if abs(d) <= 1e-9 else 0.0
            return _ou(over, (w, pu, 1 - w - pu))
        pr = None
        if kind == "goal" and not extra:
            pr = cnt(a.get("goal"))
        elif kind == "assist":
            pr = cnt(a.get("assist"))
        elif kind == "goal_assist" and a.get("goal") is not None:
            pr = cnt((a.get("goal") or 0) + (a.get("assist") or 0))
        elif kind == "red":
            pr = None if a.get("red") is None else _yes(1.0 if a["red"] else 0.0)
        elif kind in ("first", "last"):
            who = c.first_scorer if kind == "first" else c.last_scorer
            pr = None if who is None else _yes(1.0 if who and find_player(c.pm, who) is p else 0.0)
        elif kind in ("team_first", "team_last"):
            who = (c.team_first if kind == "team_first" else c.team_last).get(p["side"]) if c.team_first is not None else None
            pr = None if c.team_first is None else _yes(1.0 if who and find_player(c.pm, who) is p else 0.0)
        elif kind in ("shots", "sot", "tackles", "fouls", "fouled", "saves", "sot_head", "sot_out"):
            pr = cnt(a.get(kind))          # cabeza / fuera: Statshub extra-stats de ese partido
        elif kind == "cards":
            pr = None if a.get("cards") is None else _yes(1.0 if a["cards"] > 0 else 0.0)
        out["probs"] = pr
        return out
    lam = p["lam"]
    lt = c.lam["lh"] + c.lam["la"]
    lteam = c.lam["lh"] if p["side"] == "H" else c.lam["la"]
    pr = None
    if kind == "goal":
        lg = lam["goal"] * (_share(p, extra, cf=_corner_f(c, p)) if isinstance(extra, str) else (extra or 1.0))
        pr = _ou(over, _p_over(lg, line))
    elif kind == "goal_assist":
        pr = _ou(over, _p_over(lam["goal"] + lam["assist"], line))
    elif kind == "first_card":
        # su ritmo de tarjetas frente al de todo el partido: P(la primera es suya) = λ_jugador/Λ × P(hay alguna)
        try:
            f = (c.cm.get("CARDS") or {}).get("model")
            L_all = float(f.mu_h + f.mu_a)
        except Exception:
            L_all = 4.2
        if L_all > 0:
            pr = _yes(min(lam["cards"], L_all) / L_all * (1 - math.exp(-L_all)))
    elif kind == "red":
        pr = _yes(1 - math.exp(-lam["cards"] * 0.07))      # ~7 % de las tarjetas acaban en roja (directa o segunda)
    elif kind == "assist":
        pr = _ou(over, _p_over(lam["assist"], line))
    elif kind in ("first", "last"):
        pr = _yes(lam["goal"] / lt * (1 - math.exp(-lt)))
    elif kind in ("team_first", "team_last"):
        pr = _yes(lam["goal"] / max(lteam, 1e-6) * (1 - math.exp(-lteam)))
    elif kind == "saves":
        # paradas: la mitad de su ritmo habitual y la mitad de los tiros a puerta que esperamos del rival HOY (≈70 % se paran)
        mu = lam["saves"]
        try:
            f = (c.cm.get("SOT") or {}).get("model")
            opp_sot = float(f.mu_a if p["side"] == "H" else f.mu_h)
            mu = 0.5 * mu + 0.5 * opp_sot * 0.70 * min(1.0, p["exp_min"] / 90)
        except Exception:
            pass
        lam = {**lam, "saves": mu}
        pr = _ou(over, _p_over(mu, line, DISP.get(kind)))
    elif kind in ("fouls", "fouled"):
        # el árbitro (y la lluvia) también mueve las faltas de cada jugador
        rf = float(((c.cm or {}).get("FOULS") or {}).get("ref") or 1.0)
        lam = {**lam, kind: lam[kind] * rf}
        pr = _ou(over, _p_over(lam[kind], line, DISP.get(kind)))
    elif kind in ("shots", "sot", "tackles"):
        pr = _ou(over, _p_over(lam[kind], line, DISP.get(kind)))
    elif kind == "sot_out":
        pr = _ou(over, _p_over(lam["sot"] * _share(p, "outside the box", True), line))
        out["meta"]["mu"] = round(lam["sot"] * _share(p, "outside the box", True), 2)
    elif kind == "sot_head":
        pr = _ou(over, _p_over(lam["sot"] * _share(p, "header", True, _corner_f(c, p)), line))
        out["meta"]["mu"] = round(lam["sot"] * _share(p, "header", True, _corner_f(c, p)), 2)
    elif kind == "cards":
        pr = _yes(1 - math.exp(-lam["cards"]))
    out["probs"] = pr
    if isinstance(lam.get(kind), (int, float)):
        out["meta"]["mu"] = round(float(lam[kind]), 2)        # lo que esperamos que haga (para la escalera y las fichas)
    if kind in ("shots", "sot", "tackles", "fouls", "fouled") and p.get("exp_min"):
        # Sustituto+ de bet365: si le cambian, cuentan las estadísticas del que entra por él. El «hueco» juega el partido
        # entero; el que entra suele hacer algo menos (85 % de su ritmo) y suele entrar con piernas frescas
        em = max(1.0, float(p["exp_min"]))
        lam_s = lam[kind] * (1 + 0.85 * max(0.0, 93.0 - em) / em)
        ps = _ou(over, _p_over(lam_s, line, DISP.get(kind)))
        if ps:
            out["meta"]["p_sub"] = round(float(ps[0]), 4)
            out["meta"]["mu_sub"] = round(lam_s, 2)
    # cabeza / fuera: «media» solo si tenemos sus remates partido a partido (6+ partidos con el dato)
    if kind in ("first_card", "red"):
        out["conf"] = "baja"
        out["exp_min"] = round(p["exp_min"])
        return out
    out["conf"] = p.get("conf", "baja") if kind not in ("sot_out", "sot_head") else \
        ("media" if p.get("conf") == "media" and (p.get("shares") or {}).get("n", 0) >= 6 else "baja")
    out["exp_min"] = round(p["exp_min"])
    return out



# ---------------------------------------------------------------------------
# Liquidación de los mercados "raros" con lo que pasó de verdad
# ---------------------------------------------------------------------------
class _Exact:
    exact = True
    disp = 1e9

    def __init__(self, h, a):
        self.mu_h, self.mu_a = h, a


def exact_ctx(m: dict):
    """Contexto con el resultado real: matrices de un solo marcador, estadísticas y jugadores reales."""
    from .models.poisson import MAXG
    import json as _json
    if m.get("hg") is None:
        return None
    def one(h, a):
        P = np.zeros((MAXG, MAXG)); P[min(h, MAXG - 1), min(a, MAXG - 1)] = 1.0
        return P
    mats = {"FT": one(m["hg"], m["ag"])}
    if m.get("hg_ht") is not None:
        mats["HT"] = one(m["hg_ht"], m["ag_ht"])
        mats["2H"] = one(m["hg"] - m["hg_ht"], m["ag"] - m["ag_ht"])
    cm = {}
    st = {r["side"]: _json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (m["id"],))}
    if "H" in st and "A" in st:
        from .models.secondary import stat_value
        keys = {"CORNERS": "corners", "CARDS": "cards", "SHOTS": "shots", "SOT": "sot"}
        raw = {"FOULS": "fouls", "OFFSIDES": "offsides", "TACKLES": "totalTackle", "SAVES": "goalkeeperSaves"}
        for code, k in keys.items():
            h, a = stat_value(st["H"], k), stat_value(st["A"], k)
            if h is not None and a is not None:
                cm[code] = {"model": _Exact(h, a)}
        for code, k in raw.items():
            h, a = st["H"].get(k), st["A"].get(k)
            if h is not None and a is not None:
                cm[code] = {"model": _Exact(float(h), float(a))}
    c = Ctx(m, {"lh": max(m["hg"], 0.01), "la": max(m["ag"], 0.01)}, mats, cm, 0.45)
    c.exact = True
    # jugadores reales
    rows = db.q("SELECT ps.*, p.name AS pname, pe.head_sot, pe.out_sot FROM player_stats ps LEFT JOIN players p ON p.id=ps.player_id "
                "LEFT JOIN player_extra pe ON pe.player_id=ps.player_id AND pe.match_id=ps.match_id WHERE ps.match_id=?", (m["id"],))
    players = {}
    for r in rows:
        if not (r["minutes"] or 0) > 0:
            continue
        players[r["player_id"]] = {"id": r["player_id"], "name": r["pname"] or str(r["player_id"]), "names": [r["pname"] or ""],
                                   "side": r["side"], "exact": True,
                                   "lam": {"goal": r["goals"], "assist": r["assists"], "shots": r["shots"], "sot": r["sot"],
                                           "tackles": r["tackles"], "fouls": r["fouls"], "fouled": r["was_fouled"],
                                           "cards": None if r["yellow"] is None else (r["yellow"] or 0) + (r["red"] or 0), "saves": r["saves"],
                                           "sot_head": r["head_sot"], "sot_out": r["out_sot"], "red": r["red"]}}
    idx = {}
    for p in players.values():
        idx.setdefault(_tok(p["name"]), p)
    c._pm = {"players": players, "idx": idx, "lineup": True} if players else {}
    goals = db.q("SELECT minute, side, player FROM timeline WHERE match_id=? AND kind='goal' ORDER BY minute", (m["id"],))
    if goals and all(g["player"] for g in goals):
        c.first_scorer, c.last_scorer = goals[0]["player"], goals[-1]["player"]
        c.team_first = {s: next((g["player"] for g in goals if g["side"] == s), "") for s in ("H", "A")}
        c.team_last = {s: next((g["player"] for g in reversed(goals) if g["side"] == s), "") for s in ("H", "A")}
    elif m["hg"] + m["ag"] == 0:
        c.first_scorer = c.last_scorer = ""
        c.team_first = c.team_last = {"H": "", "A": ""}
    return c


def settle_raw(m: dict, market: str, period: str, selection: str, line) -> tuple[float, float] | None:
    """(fracción ganada, fracción devuelta) o None si todavía no se puede saber."""
    c = exact_ctx(m)
    if c is None:
        return None
    r = price_raw(market[4:] if market.startswith("RAW:") else market, period or "FT", selection, line, c)
    pr = r.get("probs")
    if not pr:
        return None
    w, p, l = (round(float(x), 6) for x in pr)
    if min(abs(w), abs(w - 0.5), abs(w - 1)) > 1e-6 or min(abs(p), abs(p - 0.5), abs(p - 1)) > 1e-6:
        return None           # no es un resultado limpio: mejor no liquidar
    return w, p
