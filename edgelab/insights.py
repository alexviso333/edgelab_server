"""Análisis de cada apuesta y de cada partido, cruzando todo lo que hay en la base de datos.

Para una apuesta concreta:
  · historial: sus últimos 10 partidos con el dato de ese mercado, cuántas veces habría pasado, casa/fuera,
    contra este rival
  · rol: posición media (mapas de calor) de los últimos partidos frente a los anteriores y la posición
    prevista para este partido
  · rival: qué conceden a los jugadores de esa zona (o al equipo) en sus últimos 10 partidos frente a la media
  · duelo directo: el rival de su zona (alineación prevista) y lo que hace él del dato contrario
    (faltas que comete ↔ faltas que recibe, entradas ↔ regates…), con el % de campo que comparten
  · árbitro: tarjetas y faltas por partido frente a la media
  · 10.000 simulaciones del partido → reparto del dato y % de veces que pasa
  · señales: cada dato empuja a favor o en contra; se cuentan las que apuntan al mismo lado
  · texto en lenguaje de tipster, solo con hechos de la base de datos (nunca se inventan declaraciones)

Para un partido: cruces (lo que un equipo hace × lo que el otro concede, frente a la media de liga),
duelos por banda y simulación completa (goles, córners, tarjetas, tiros, faltas).
"""
from __future__ import annotations

import json
import math
import time

import numpy as np

from .core import db

N_SIMS = 10000

# ---------------------------------------------------------------------------- posiciones y zonas
POS = {
    "GK": ("GK", "C"), "G": ("GK", "C"),
    "CB": ("D", "C"), "LCB": ("D", "C"), "RCB": ("D", "C"), "D": ("D", "C"),
    "LB": ("FB", "L"), "LWB": ("FB", "L"), "RB": ("FB", "R"), "RWB": ("FB", "R"),
    "CDM": ("DM", "C"), "LCDM": ("DM", "C"), "RCDM": ("DM", "C"), "DM": ("DM", "C"),
    "CM": ("M", "C"), "LCM": ("M", "C"), "RCM": ("M", "C"), "M": ("M", "C"),
    "LM": ("WM", "L"), "RM": ("WM", "R"),
    "CAM": ("AM", "C"), "AM": ("AM", "C"), "LAM": ("AM", "C"), "RAM": ("AM", "C"),
    "LW": ("W", "L"), "RW": ("W", "R"),
    "ST": ("F", "C"), "CF": ("F", "C"), "F": ("F", "C"), "LS": ("F", "C"), "RS": ("F", "C"),
}
LINE_WORD = {"GK": "porteros", "D": "centrales", "FB": "laterales", "DM": "mediocentros defensivos", "M": "centrocampistas",
             "WM": "interiores de banda", "AM": "mediapuntas", "W": "extremos", "F": "delanteros centro"}
LINE_ONE = {"GK": "portero", "D": "central", "FB": "lateral", "DM": "pivote", "M": "centrocampista", "WM": "carrilero",
            "AM": "mediapunta", "W": "extremo", "F": "delantero centro"}
SIDE_WORD = {"L": "izquierda", "R": "derecha", "C": "centro"}
# a quién tiene enfrente cada línea (el rival ataca hacia el otro lado: su banda izquierda es nuestra derecha)
MIRROR_LINE = {"W": ("FB", "WM"), "WM": ("FB", "WM"), "FB": ("W", "WM"), "F": ("D",), "D": ("F",), "AM": ("DM", "M"),
               "DM": ("AM", "M"), "M": ("M", "DM", "AM"), "GK": ("F",)}
MIRROR_SIDE = {"L": "R", "R": "L", "C": "C"}


def pos_info(code: str | None) -> tuple[str, str] | None:
    return POS.get((code or "").upper())


# ---------------------------------------------------------------------------- datos que mira cada mercado
PLAYER_STAT = {"goal": "goals", "first": "goals", "last": "goals", "team_first": "goals", "team_last": "goals",
               "assist": "assists", "goal_assist": None, "shots": "shots", "sot": "sot", "sot_out": "sot", "sot_head": "sot",
               "tackles": "tackles", "fouls": "fouls", "fouled": "was_fouled", "cards": "yellow", "saves": "saves"}
STAT_WORD = {"goals": "goles", "assists": "asistencias", "shots": "tiros", "sot": "tiros a puerta", "tackles": "entradas",
             "fouls": "faltas cometidas", "was_fouled": "faltas recibidas", "yellow": "tarjetas", "saves": "paradas",
             "ga": "goles + asistencias", "won_contest": "regates"}
# el dato "contrario" del rival directo
DUEL_STAT = {"was_fouled": ("fouls", "comete {} faltas"), "fouls": ("was_fouled", "recibe {} faltas"),
             "tackles": ("won_contest", "completa {} regates"), "won_contest": ("tackles", "hace {} entradas"),
             "shots": ("tackles", "hace {} entradas"), "sot": ("tackles", "hace {} entradas"), "yellow": ("was_fouled", "recibe {} faltas")}
# si el par hace mucho de lo suyo, ¿ayuda (+1) o estorba (-1) a nuestra apuesta?
DUEL_SIGN = {"was_fouled": 1, "fouls": 1, "tackles": 1, "won_contest": -1, "shots": -1, "sot": -1, "yellow": 1}
TEAM_KEY = {"CORNERS": ("corners", "córners"), "CARDS": ("cards", "tarjetas"), "SHOTS": ("shots", "tiros"),
            "SOT": ("sot", "tiros a puerta"), "FOULS": ("fouls", "faltas"), "OFFSIDES": ("offsides", "fueras de juego"),
            "TACKLES": ("totalTackle", "entradas"), "SAVES": ("goalkeeperSaves", "paradas")}
CROSS_KEYS = [  # (clave, nombre, el que lo hace, el que lo provoca en contra)
    ("expectedGoals", "goles esperados (xG)"), ("totalShotsOnGoal", "tiros"), ("shotsOnGoal", "tiros a puerta"),
    ("cornerKicks", "córners"), ("fouls", "faltas"), ("yellowCards", "tarjetas"), ("offsides", "fueras de juego"),
    ("goalKicks", "saques de puerta"), ("throwIns", "saques de banda"), ("freeKicks", "tiros libres"),
    ("touchesInOppBox", "toques en el área rival"), ("bigChanceCreated", "ocasiones claras"),
    ("goalkeeperSaves", "paradas del portero"), ("accurateCross", "centros buenos"), ("totalTackle", "entradas"),
]


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        s = str(v).split("/")[0].replace("%", "").strip()
        try:
            return float(s)
        except ValueError:
            return None


def team_stat(st: dict, key: str) -> float | None:
    from .models.secondary import STAT_KEYS, stat_value
    if key in STAT_KEYS:
        return stat_value(st, key)
    return _num(st.get(key))


def fmt(x, d: int = 1) -> str:
    if x is None:
        return "–"
    s = f"{x:.{d}f}".replace(".", ",")
    return s[:-2] if d and s.endswith(",0") else s


def fline(L) -> str:
    return fmt(float(L), 1) if L is not None else ""


# ---------------------------------------------------------------------------- medias de referencia (caché diaria)
def league_refs() -> dict:
    day = time.strftime("%Y-%m-%d")
    c = db.kv_get("insight_refs")
    if c and c.get("day") == day:
        return c
    mx = (db.q1("SELECT MAX(kickoff) AS t FROM matches WHERE status='finished'") or {}).get("t") or int(time.time())
    since = min(int(time.time()), mx) - 400 * 86400
    pos: dict = {}
    for r in db.q("SELECT ps.position, SUM(ps.minutes) AS mins, SUM(ps.fouls) AS fouls, SUM(ps.was_fouled) AS was_fouled, "
                  "SUM(ps.tackles) AS tackles, SUM(ps.shots) AS shots, SUM(ps.sot) AS sot, SUM(ps.won_contest) AS won_contest, "
                  "SUM(ps.yellow) AS yellow, SUM(ps.goals) AS goals, SUM(ps.assists) AS assists, SUM(ps.saves) AS saves "
                  "FROM player_stats ps JOIN matches m ON m.id=ps.match_id WHERE ps.minutes>0 AND m.kickoff>? GROUP BY ps.position", (since,)):
        pi = pos_info(r["position"])
        if not pi or not r["mins"]:
            continue
        g = pos.setdefault(pi[0], {"mins": 0.0})
        g["mins"] += r["mins"]
        for k in ("fouls", "was_fouled", "tackles", "shots", "sot", "won_contest", "yellow", "goals", "assists", "saves"):
            g[k] = g.get(k, 0.0) + (r[k] or 0)
    per90 = {ln: {k: v / g["mins"] * 90 for k, v in g.items() if k != "mins"} for ln, g in pos.items() if g["mins"] > 0}
    team: dict = {}
    rows = db.q("SELECT ms.stats FROM match_stats ms JOIN matches m ON m.id=ms.match_id WHERE m.kickoff>? AND m.status='finished' "
                "ORDER BY m.kickoff DESC LIMIT 6000", (since,))
    acc: dict = {}
    for r in rows:
        st = json.loads(r["stats"])
        for k, _ in CROSS_KEYS:
            v = team_stat(st, k)
            if v is not None:
                acc.setdefault(k, []).append(v)
        for code, (k, _) in TEAM_KEY.items():
            v = team_stat(st, k)
            if v is not None:
                acc.setdefault("T:" + code, []).append(v)
    for k, vs in acc.items():
        if len(vs) >= 30:
            team[k] = float(np.mean(vs))
    ref_rows = db.q("SELECT m.referee_id, ms.stats FROM matches m JOIN match_stats ms ON ms.match_id=m.id WHERE m.referee_id IS NOT NULL "
                    "AND m.kickoff>? AND m.status='finished'", (since,))
    cards = [team_stat(json.loads(r["stats"]), "cards") for r in ref_rows]
    cards = [v for v in cards if v is not None]
    out = {"day": day, "pos": per90, "team": team, "cards_match": float(np.mean(cards) * 2) if cards else 4.2}
    db.kv_set("insight_refs", out)
    return out


# ---------------------------------------------------------------------------- piezas
COMP_NAME = {"UCL": "Champions", "UEL": "Europa League", "UECL": "Conference League", "EPL": "Premier League", "LALIGA": "LaLiga",
             "SERIEA": "Serie A", "BUNDES": "Bundesliga", "LIGUE1": "Ligue 1", "CDR": "Copa del Rey"}


def comp_label(c: str | None) -> str:
    if not c:
        return ""
    if c in COMP_NAME:
        return COMP_NAME[c]
    for k, v in (("Champions", "Champions"), ("Europa League", "Europa League"), ("Conference", "Conference League")):
        if k in c:
            return v
    return c


def _player_rows(pid: int, before: int, limit: int = 30, team_id: int | None = None) -> list[dict]:
    """Sus partidos de TODAS las competiciones (los de liga de equipos que solo seguimos en Europa también, vía Statshub)."""
    rows = db.q("SELECT ps.*, COALESCE(m.kickoff, g.kickoff) AS kickoff, COALESCE(m.home_id, g.home_id) AS home_id, "
                "COALESCE(m.away_id, g.away_id) AS away_id, COALESCE(m.home_name, g.home_name) AS home_name, "
                "COALESCE(m.away_name, g.away_name) AS away_name, COALESCE(m.comp, g.tournament) AS comp, "
                "COALESCE(m.hg, g.hg) AS hg, COALESCE(m.ag, g.ag) AS ag FROM player_stats ps "
                "LEFT JOIN matches m ON m.id=ps.match_id LEFT JOIN game_meta g ON g.match_id=ps.match_id "
                "WHERE ps.player_id=? AND ps.minutes>0 AND COALESCE(m.kickoff, g.kickoff)<? AND (m.id IS NULL OR m.status='finished') "
                + ("AND ps.team_id=? " if team_id else "") +
                "ORDER BY COALESCE(m.kickoff, g.kickoff) DESC LIMIT ?", (pid, before, team_id, limit) if team_id else (pid, before, limit))
    for r in rows:
        r["comp"] = comp_label(r.get("comp"))
    return rows


def _val(r: dict, stat: str | None) -> float:
    if stat is None:
        return float((r["goals"] or 0) + (r["assists"] or 0))
    v = float(r[stat] or 0)
    if stat == "yellow":
        v += float(r["red"] or 0)
    return v


def _hit(v: float, over: bool, line: float) -> bool:
    return v > line + 1e-9 if over else v < line - 1e-9


def heat_summary(pid: int, before: int, n: int = 8) -> dict | None:
    hs = db.q("SELECT h.*, m.kickoff, m.home_name, m.away_name FROM heatmaps h JOIN matches m ON m.id=h.match_id "
              "WHERE h.player_id=? AND m.kickoff<? ORDER BY m.kickoff DESC LIMIT ?", (pid, before, n))
    if not hs:
        return None
    from .collectors.tactics import GX, GY
    g = np.zeros((GY, GX))
    for h in hs:
        try:
            g += np.array(json.loads(h["grid"]), float)
        except Exception:
            pass
    tot = g.sum()
    recent = hs[:3]
    older = hs[3:]
    ax = lambda rs: float(np.average([r["avg_x"] for r in rs], weights=[max(r["n"] or 1, 1) for r in rs])) if rs else None
    ay = lambda rs: float(np.average([r["avg_y"] for r in rs], weights=[max(r["n"] or 1, 1) for r in rs])) if rs else None
    return {"grid": (g / tot).round(4).tolist() if tot else g.tolist(), "n_matches": len(hs),
            "avg_x": ax(hs), "avg_y": ay(hs), "recent_x": ax(recent), "older_x": ax(older), "recent_y": ay(recent), "older_y": ay(older),
            "positions": [h["position"] for h in hs], "matches": [f"{h['home_name']} – {h['away_name']}" for h in hs]}


def _overlap(ga: list | None, gd: list | None) -> float | None:
    """% de campo compartido: zona del atacante frente a la zona del defensor vista desde el otro lado."""
    if not ga or not gd:
        return None
    a = np.array(ga, float)
    d = np.array(gd, float)[::-1, ::-1]
    if a.sum() <= 0 or d.sum() <= 0:
        return None
    a, d = a / a.sum(), d / d.sum()
    return float(np.minimum(a, d).sum())


def expected_lineup(mid: int, side: str, team_id: int, before: int) -> list[dict]:
    lu = db.q("SELECT player_id, name, position, starter, source FROM lineups WHERE match_id=? AND side=? AND source IN ('confirmed','predicted') "
              "AND starter=1", (mid, side))
    if lu:
        return [dict(r) for r in lu]
    last = db.q1("SELECT m.id FROM matches m WHERE (m.home_id=? OR m.away_id=?) AND m.status='finished' AND m.kickoff<? "
                 "AND EXISTS (SELECT 1 FROM player_stats ps WHERE ps.match_id=m.id AND ps.team_id=?) ORDER BY m.kickoff DESC LIMIT 1",
                 (team_id, team_id, before, team_id))
    if not last:
        return []
    return [{"player_id": r["player_id"], "name": r["name"], "position": r["position"], "starter": 1, "source": "último once"}
            for r in db.q("SELECT ps.player_id, p.name, ps.position FROM player_stats ps LEFT JOIN players p ON p.id=ps.player_id "
                          "WHERE ps.match_id=? AND ps.team_id=? AND ps.starter=1", (last["id"], team_id))]


def player_rate(pid: int, before: int, stat: str, n: int = 15, prior: float | None = None, k: float = 3.0) -> tuple[float | None, int, float]:
    """Por 90'. Con `prior` (media de su puesto) se contrae hacia ella como si hubiera jugado k partidos más de media:
    así un chaval con 120 minutos no sale con cifras de récord."""
    rs = _player_rows(pid, before, n)
    mins = sum(r["minutes"] or 0 for r in rs)
    if mins < 90:
        return (prior if prior is not None and mins > 0 else None), len(rs), mins
    tot = sum(_val(r, stat) for r in rs)
    from .postmatch import bias_factor, entity_factor
    bf = (bias_factor("player", stat) * entity_factor(f"pl:{pid}:{stat}")) if stat else 1.0
    if prior is not None:
        return (tot + prior * k) / (mins / 90 + k) * bf, len(rs), mins
    return tot / mins * 90 * bf, len(rs), mins


def direct_rivals(m: dict, side: str, pid: int, my_pos: str | None, my_heat: dict | None, stat: str) -> list[dict]:
    """Los rivales de su zona en el once previsto, con lo que hacen del dato contrario."""
    pi = pos_info(my_pos)
    if not pi:
        return []
    opp_side = "A" if side == "H" else "H"
    opp_tid = m["away_id"] if side == "H" else m["home_id"]
    lines = MIRROR_LINE.get(pi[0], ())
    want_side = MIRROR_SIDE[pi[1]]
    cands = []
    lineup = expected_lineup(m["id"], opp_side, opp_tid, m["kickoff"])
    # con mapas de calor: su par es quien más terreno comparte con él de verdad (un lateral que vive arriba se cruza
    # con el lateral rival; un extremo que se mete por dentro, con el central o el pivote), no el de la etiqueta
    if my_heat and my_heat.get("grid"):
        by_ov = []
        for r in lineup:
            oi = pos_info(r["position"])
            if not oi or oi[0] == "GK":
                continue
            h = heat_summary(r["player_id"], m["kickoff"], 6)
            ov = _overlap(my_heat["grid"], (h or {}).get("grid"))
            if ov is not None and (h or {}).get("n_matches", 0) >= 2:
                by_ov.append((ov + (0.03 if oi[0] in lines else 0.0), r, oi, h, ov))
        if len(by_ov) >= 4:
            by_ov.sort(key=lambda x: -x[0])
            cands = [(sc, r, oi) for sc, r, oi, h, ov in by_ov if ov >= 0.12][:2]
            if cands:
                return _rival_rows(m, cands, stat, my_heat, {r["player_id"]: h for _, r, _, h, _ in by_ov})
    for r in lineup:
        oi = pos_info(r["position"])
        if not oi or oi[0] not in lines:
            continue
        if pi[1] != "C" and oi[1] not in (want_side, "C"):
            continue
        if pi[1] != "C" and oi[1] == "C" and oi[0] not in ("D", "DM"):
            continue
        score = (2 if oi[1] == want_side else 1) + (1 if oi[0] == lines[0] else 0)
        cands.append((score, r, oi))
    cands.sort(key=lambda x: -x[0])
    return _rival_rows(m, cands[:2], stat, my_heat, {})


def _rival_rows(m: dict, cands: list, stat: str, my_heat: dict | None, heats: dict) -> list[dict]:
    out = []
    refs = league_refs()["pos"]
    dstat, verb = DUEL_STAT.get(stat, (None, None))
    for score, r, oi in cands[:2]:
        rate = n = None
        lg = refs.get(oi[0], {}).get(dstat) if dstat else None
        if dstat:
            rate, n, _ = player_rate(r["player_id"], m["kickoff"], dstat, prior=lg)
        heat = heats.get(r["player_id"]) or heat_summary(r["player_id"], m["kickoff"], 6)
        out.append({"player_id": r["player_id"], "name": r["name"], "pos": r["position"], "line": oi[0], "side": oi[1],
                    "stat": dstat, "verb": verb, "rate": rate, "lg": lg, "n": n, "heat": heat,
                    "overlap": _overlap((my_heat or {}).get("grid"), (heat or {}).get("grid")), "source": r.get("source")})
    return out


def opp_allowance(m: dict, opp_tid: int, line_code: str | None, stat: str, n_matches: int = 10) -> dict | None:
    """Lo que hacen del dato los jugadores de esa línea contra este rival (por 90'), frente a la media de su línea."""
    if not line_code:
        return None
    # «opponent stats» de Statshub: qué hicieron contra este rival los jugadores de ese puesto, en TODAS sus competiciones
    ms = [r["id"] for r in db.q("SELECT id, kickoff FROM (SELECT id, kickoff, home_id, away_id FROM matches WHERE status='finished' "
                                "UNION SELECT match_id, kickoff, home_id, away_id FROM game_meta WHERE match_id NOT IN (SELECT id FROM matches)) "
                                "WHERE (home_id=? OR away_id=?) AND kickoff<? AND id IN (SELECT match_id FROM player_stats) "
                                "ORDER BY kickoff DESC LIMIT ?", (opp_tid, opp_tid, m["kickoff"], n_matches))]
    if not ms:
        return None
    rows = db.q(f"SELECT * FROM player_stats WHERE match_id IN ({','.join(map(str, ms))}) AND team_id!=? AND minutes>0", (opp_tid,))
    tot = mins = 0.0
    games = set()
    for r in rows:
        pi = pos_info(r["position"])
        if not pi or pi[0] != line_code:
            continue
        tot += _val(r, stat)
        mins += r["minutes"] or 0
        games.add(r["match_id"])
    if mins < 180:
        return None
    lg = league_refs()["pos"].get(line_code, {}).get(stat if stat else "goals")
    rate = tot / mins * 90
    return {"rate": rate, "lg": lg, "ratio": rate / lg if lg else None, "games": len(games)}


def referee_info(m: dict) -> dict | None:
    if not m.get("referee_id"):
        return None
    rows = db.q("SELECT m.id, ms.stats FROM matches m JOIN match_stats ms ON ms.match_id=m.id WHERE m.referee_id=? AND m.status='finished' "
                "AND m.kickoff<? ORDER BY m.kickoff DESC LIMIT 60", (m["referee_id"], m["kickoff"]))
    per: dict = {}
    for r in rows:
        st = json.loads(r["stats"])
        d = per.setdefault(r["id"], {"cards": 0.0, "fouls": 0.0, "ok": 0})
        c, f = team_stat(st, "cards"), team_stat(st, "fouls")
        if c is not None:
            d["cards"] += c
            d["ok"] += 1
        if f is not None:
            d["fouls"] += f
    games = [d for d in per.values() if d["ok"] == 2]
    if len(games) < 3:
        return None
    lg = league_refs()
    return {"name": m.get("referee"), "games": len(games), "cards": float(np.mean([g["cards"] for g in games])),
            "fouls": float(np.mean([g["fouls"] for g in games])) or None, "lg_cards": lg["cards_match"],
            "lg_fouls": (lg["team"].get("T:FOULS") or 11.5) * 2}


def team_history(tid: int, before: int, n: int = 10) -> list[dict]:
    rows = db.q("SELECT m.id, m.kickoff, m.home_id, m.away_id, m.home_name, m.away_name, m.hg, m.ag, m.comp, ms.side, ms.stats "
                "FROM matches m JOIN match_stats ms ON ms.match_id=m.id WHERE (m.home_id=? OR m.away_id=?) AND m.status='finished' "
                "AND m.kickoff<? ORDER BY m.kickoff DESC LIMIT ?", (tid, tid, before, n * 2))
    by: dict = {}
    for r in rows:
        d = by.setdefault(r["id"], {"id": r["id"], "kickoff": r["kickoff"], "home": r["home_id"] == tid, "comp": r["comp"],
                                    "opp": r["away_name"] if r["home_id"] == tid else r["home_name"],
                                    "gf": r["hg"] if r["home_id"] == tid else r["ag"], "ga": r["ag"] if r["home_id"] == tid else r["hg"]})
        mine = (r["side"] == "H") == (r["home_id"] == tid)
        d["for" if mine else "against"] = json.loads(r["stats"])
    out = [d for d in by.values() if "for" in d and "against" in d]
    out.sort(key=lambda d: -d["kickoff"])
    return out[:n]


def managers(mid: int) -> dict:
    return db.kv_get(f"managers:{mid}") or {}


# ---------------------------------------------------------------------------- simulaciones
def _nb(rng, mu, r, size):
    mu = np.maximum(np.asarray(mu, float), 1e-9)
    if r is None or r > 400:
        return rng.poisson(mu, size)
    p = r / (r + mu)
    return rng.negative_binomial(r, p, size)


def sim_player(rate90: float, exp_min: float, starts_min: list[float], disp: float | None, over: bool, line: float,
               scale: float = 1.0, seed: int = 5) -> dict:
    rng = np.random.default_rng(seed)
    if starts_min and exp_min >= 45:
        mins = rng.choice(np.array(starts_min, float), N_SIMS)
    else:
        mins = np.clip(rng.normal(exp_min, 8 if exp_min < 45 else 12, N_SIMS), 1, 95)
    mu = rate90 * mins / 90 * scale
    k = _nb(rng, mu, disp, N_SIMS)
    hist = np.bincount(np.minimum(k, 12), minlength=13)[:13]
    hits = int(((k > line) if over else (k < line)).sum())
    return {"n": N_SIMS, "hits": hits, "p": hits / N_SIMS, "hist": hist.tolist(), "mean": float(k.mean()), "line": line, "over": over}


def sim_count(cf, side: str | None, over: bool, line: float, per: str = "FT", seed: int = 6) -> dict:
    rng = np.random.default_rng(seed)
    sc = {"HT": 0.46}.get(per, 1.0)
    a = _nb(rng, cf.mu_h * sc, cf.disp, N_SIMS)
    b = _nb(rng, cf.mu_a * sc, cf.disp, N_SIMS)
    k = a if side == "H" else b if side == "A" else a + b
    top = int(max(12, np.percentile(k, 99) + 1))
    hist = np.bincount(np.minimum(k, top), minlength=top + 1)
    w = int(((k > line + 1e-9) if over else (k < line - 1e-9)).sum())
    push = int((np.abs(k - line) < 1e-9).sum())
    return {"n": N_SIMS, "hits": w, "push": push, "p": w / N_SIMS, "hist": hist.tolist(), "mean": float(k.mean()), "line": line, "over": over}


def sim_goals(lh: float, la: float, market: str, sel: str, line, seed: int = 7) -> dict | None:
    from .plans import Sim
    from .pricing import goal_probs
    s = _SIMS.get((round(lh, 3), round(la, 3)))
    if s is None:
        s = Sim(lh, la, n=N_SIMS, seed=seed)
        if len(_SIMS) > 30:
            _SIMS.clear()
        _SIMS[(round(lh, 3), round(la, 3))] = s
    fh, fa = s.fh, s.fa
    n = 11
    P = np.zeros((n, n))
    np.add.at(P, (np.minimum(fh, n - 1), np.minimum(fa, n - 1)), 1)
    P /= P.sum()
    r = goal_probs(market, sel, line, P)
    if not r:
        return None
    w, p, l = r
    tot = np.minimum(fh + fa, 8)
    scores: dict = {}
    for a, b in zip(fh[:N_SIMS], fa[:N_SIMS]):
        k = f"{min(a, 5)}-{min(b, 5)}"
        scores[k] = scores.get(k, 0) + 1
    top = sorted(scores.items(), key=lambda x: -x[1])[:6]
    return {"n": N_SIMS, "hits": int(round(w * N_SIMS)), "push": int(round(p * N_SIMS)), "p": w, "hist": np.bincount(tot, minlength=9).tolist(),
            "hist_kind": "goles totales", "scores": [[k, v / N_SIMS] for k, v in top],
            "home_win": float((fh > fa).mean()), "draw": float((fh == fa).mean()), "away_win": float((fh < fa).mean())}


_SIMS: dict = {}


# ---------------------------------------------------------------------------- análisis de una apuesta
def _row_for(rows: list[dict], market: str, period: str, selection: str, line) -> dict | None:
    L = None if line in (None, "", "null") else float(line)
    for r in rows:
        if r["market"] == market and r["period"] == period and r["selection"] == selection and \
                ((r["line"] is None and L is None) or (r["line"] is not None and L is not None and abs(float(r["line"]) - L) < 1e-9)):
            return r
    return None


def _resolve_player(m: dict, name: str) -> dict:
    """Jugador de la cuota → id (por once previsto/jugado o por la tabla de jugadores de los dos equipos)."""
    from .markets_all import _tok
    tn = _tok(name)
    last = norm_last(name)
    cands = db.q("SELECT player_id AS id, name, side FROM lineups WHERE match_id=?", (m["id"],))
    if not cands:
        cands = []
        for side, tid in (("H", m["home_id"]), ("A", m["away_id"])):
            cands += [{**dict(r), "side": side} for r in db.q("SELECT id, name FROM players WHERE team_id=?", (tid,))]
    best = None
    for c in cands:
        if not c["name"]:
            continue
        if _tok(c["name"]) == tn:
            return {"pid": c["id"], "side": c["side"]}
        if norm_last(c["name"]) == last and best is None:
            best = {"pid": c["id"], "side": c["side"]}
    return best or {}


def norm_last(name: str) -> str:
    from .markets_all import norm
    parts = norm(name).split()
    return parts[-1] if parts else ""


def bet_insight(mid: int, market: str, period: str, selection: str, line=None) -> dict | None:
    from .pricing import price_match
    pr = price_match(mid)
    if not pr:
        return None
    m = pr["match"]
    row = _row_for(pr["rows"], market, period, selection, line)
    if not row:
        return None
    meta = row.get("meta") or {}
    signals: list[dict] = []
    facts: dict = {"row": {k: row.get(k) for k in ("market", "period", "selection", "line", "label", "group", "p", "p_model", "p_market",
                                                     "fair", "b365", "b365_open", "best", "best_book", "ev", "conf", "n_books", "player", "team", "kf")}}
    mg = managers(mid)
    facts["managers"] = mg
    if meta.get("t") == "player" and not meta.get("pid") and row.get("player"):
        meta = {**meta, **_resolve_player(m, row["player"])}
        if meta.get("side") and not row.get("team"):
            row = {**row, "team": meta["side"]}
    if meta.get("t") == "player" and meta.get("pid"):
        _player_insight(m, pr, row, meta, facts, signals)
        miss = next((p for p in ((db.kv_get(f"missing:{mid}") or {}).get("players") or []) if p.get("id") == int(meta["pid"])), None)
        if not miss:
            fl = [f for f in ((db.kv_get(f"news:{mid}") or {}).get("flags") or []) if f.get("player_id") == int(meta["pid"])]
            fl.sort(key=lambda f: -(f.get("ts") or 0))           # la noticia más reciente manda
            nf = fl[0] if fl else None
            if nf and nf["kind"] == "baja":
                facts["news_flag"] = nf
                fuera = nf.get("level") == "fuera"
                signals.append({"key": "noticia", "dir": -1, "w": 3.0 if fuera else 1.5,
                                "text": (f"Una noticia lo da como baja: «{nf['title'][:110]}»" if fuera
                                         else f"Ojo, una noticia lo pone en duda: «{nf['title'][:110]}»")})
            elif nf and nf["kind"] == "vuelve":
                signals.append({"key": "noticia", "dir": 1, "w": 0.8, "text": f"Buenas noticias: «{nf['title'][:110]}»"})
        if miss:
            facts["missing"] = miss
            signals.append({"key": "baja", "dir": -1 if miss["status"] == "baja" else -1, "w": 5.0 if miss["status"] == "baja" else 1.5,
                            "text": f"{'No juega' if miss['status'] == 'baja' else 'Es duda'}: {miss['reason']}" + (f" ({miss['detail']})" if miss.get("detail") and not miss["detail"].isdigit() else "")})
    elif meta.get("t") == "count" or market.split("_")[0] in TEAM_KEY:
        _count_insight(m, pr, row, meta, facts, signals)
    elif market in ("1X2", "DC", "DNB", "AH", "OU", "TEAM_OU", "BTTS", "CS") and period == "FT":
        _goals_insight(m, pr, row, facts, signals)
    else:
        _generic_insight(m, pr, row, facts, signals)
    if meta.get("t") != "player":
        _absence_signals(m, row, facts, signals)
    _market_signals(row, facts, signals)
    try:
        _edge_signals(m, row, meta, facts, signals)
    except Exception as e:
        db.log_job("insight", "error", f"señales nuevas {mid}: {e}")
    from .review import signal_weight
    for s_ in signals:      # lo aprendido en la revisión: cada tipo de señal pesa según lo que ha acertado de verdad
        s_["w"] = round(s_["w"] * signal_weight(s_["key"]), 3)
    pro = [s for s in signals if s["dir"] > 0]
    con = [s for s in signals if s["dir"] < 0]
    wp, wc = sum(s["w"] for s in pro), sum(s["w"] for s in con)
    score = wp / (wp + wc) if (wp + wc) else 0.5
    ev = row.get("ev")
    sim_p = (facts.get("sim") or {}).get("p")
    if ev is not None and ev > 0.02 and score >= 0.6 and row.get("conf") != "baja":
        verdict = ("Apostar", "good")
    elif ev is not None and ev > 0 and score >= 0.5:
        verdict = ("Con cautela", "warn")
    elif ev is not None and ev <= 0:
        verdict = ("No paga lo suficiente", "bad")
    else:
        verdict = ("Evitar", "bad")
    if facts.get("start_risk") and verdict[1] == "good":
        verdict = ("Espera al once", "warn")
    if facts.get("absence_risk") and verdict[1] == "good":
        verdict = ("Espera al once", "warn")
    if facts.get("move_warn") and verdict[1] == "good":
        verdict = ("Espera al once", "warn")
    if facts.get("news_flag") and verdict[1] == "good":
        verdict = ("Espera al once", "warn")
    if (facts.get("missing") or {}).get("status") == "baja":
        verdict = ("No juega", "bad")
    elif (facts.get("missing") or {}).get("status") == "duda" and verdict[1] == "good":
        verdict = ("Espera al once", "warn")
    facts.update({"signals": signals, "n_pro": len(pro), "n_con": len(con), "score": round(score, 3), "verdict": verdict[0],
                  "tone": verdict[1], "match": {k: m.get(k) for k in ("id", "comp", "kickoff", "home_name", "away_name", "referee")},
                  "sim_p": sim_p})
    facts["text"] = tipster_text(m, row, facts)
    return facts


def _fav_side(row) -> str | None:
    """A qué equipo le conviene que salga la apuesta (None si a ninguno en concreto)."""
    mk, sel = row.get("market") or "", str(row.get("selection") or "")
    if mk in ("1X2", "DNB", "AH", "EH") and sel in ("H", "A"):
        return sel
    if mk == "DC":
        return {"1X": "H", "X2": "A"}.get(sel)
    if row.get("team") in ("H", "A"):
        return row["team"]
    if mk == "TEAM_OU" and sel[:1] in ("H", "A"):
        return sel[:1] if sel[1:2] == "O" else ("A" if sel[:1] == "H" else "H")
    return None


def _absence_signals(m, row, facts, signals) -> None:
    """Bajas, noticias y rotaciones en las apuestas de equipo: si al equipo al que le conviene la apuesta le falta lo
    importante, señal en contra (y «Espera al once» si el once aún no es oficial); si le falta al rival, a favor."""
    try:
        from .absences import team_ratios
        tr = team_ratios(m["id"])
    except Exception:
        return
    fav = _fav_side(row)
    names = {"H": m.get("home_name"), "A": m.get("away_name")}
    facts["absences"] = tr
    for side, t in tr.items():
        r = t.get("r", 1.0)
        if r >= 0.9:
            continue
        who = ", ".join(t.get("missing") or []) or "varios titulares"
        txt = f"{names[side]} sale debilitado ({t.get('src')}): le faltan {who}; aporta en torno al {round(r * 100)} % de lo habitual"
        if fav == side:
            signals.append({"key": "bajas", "dir": -1, "w": 2.5 if r < 0.8 else 1.6, "text": txt})
            if "confirmado" not in (t.get("src") or ""):
                facts["absence_risk"] = True
        elif fav:
            signals.append({"key": "bajas", "dir": 1, "w": 1.2 if r < 0.8 else 0.8, "text": txt})
        else:
            mk, sel = row.get("market") or "", str(row.get("selection") or "")
            if mk in ("OU", "BTTS") or mk.endswith("_OU"):
                over = sel[:1] in ("O", "Y")
                signals.append({"key": "bajas", "dir": -1 if over else 1, "w": 0.9, "text": txt})


def _player_insight(m, pr, row, meta, facts, signals) -> None:
    from .markets_all import DISP, player_model
    kind, over, line, pid = meta["kind"], meta["over"], float(meta.get("line") or 0.5), int(meta["pid"])
    stat = PLAYER_STAT.get(kind, "goals")
    side = row.get("team") or "H"
    tid = m["home_id"] if side == "H" else m["away_id"]
    opp_tid = m["away_id"] if side == "H" else m["home_id"]
    opp_name = m["away_name"] if side == "H" else m["home_name"]
    team_name = m["home_name"] if side == "H" else m["away_name"]
    rows_all = _player_rows(pid, m["kickoff"], 40)
    # contexto: si ha cambiado de equipo, cuenta lo que ha hecho en el actual (con otro equipo el rol era otro)
    rows_team = [r for r in rows_all if r["team_id"] == tid]
    moved = bool(rows_all) and rows_all[0]["team_id"] != tid if rows_all else False
    first_other = next((i for i, r in enumerate(rows_all) if r["team_id"] != tid), None)
    rows = rows_team if len(rows_team) >= 5 else rows_all
    facts["context"] = {"team_games": len(rows_team), "all_games": len(rows_all),
                        "joined": first_other is not None and first_other >= 1 and len(rows_team) < len(rows_all),
                        "prev_team": (rows_all[first_other]["home_name"] if rows_all[first_other]["side"] == "H" else rows_all[first_other]["away_name"])
                        if first_other is not None else None, "moved_recent": moved}
    pname = row.get("player") or (db.q1("SELECT name FROM players WHERE id=?", (pid,)) or {}).get("name")
    # ventana: 10 por defecto; si hay 15+ con su equipo actual, también se mira la serie larga
    last = rows[:10]
    hist = []
    for r in last:
        v = _val(r, stat)
        home = r["side"] == "H"
        hist.append({"kickoff": r["kickoff"], "opp": r["away_name"] if home else r["home_name"], "home": home, "v": v,
                     "min": r["minutes"], "starter": bool(r["starter"]), "hit": _hit(v, over, line), "pos": r["position"], "comp": r["comp"]})
    n_hit = sum(h["hit"] for h in hist)
    venue = [h for h in hist if h["home"] == (side == "H")]
    mins = sum(r["minutes"] or 0 for r in rows[:15])
    rate = sum(_val(r, stat) for r in rows[:15]) / mins * 90 if mins >= 90 else None
    mins5 = sum(r["minutes"] or 0 for r in rows[:5])
    rate5 = sum(_val(r, stat) for r in rows[:5]) / mins5 * 90 if mins5 >= 90 else None
    vs_opp = [{"kickoff": r["kickoff"], "v": _val(r, stat), "min": r["minutes"]} for r in rows
              if opp_tid in (r["home_id"], r["away_id"])][:5]
    # minutos y rol en este partido
    lu = db.q1("SELECT position, starter, source FROM lineups WHERE match_id=? AND player_id=? AND source IN ('confirmed','predicted')",
               (m["id"], pid))
    usual_pos = max(set(h["pos"] for h in hist if h["pos"]), key=lambda p: sum(1 for h in hist if h["pos"] == p), default=None)
    pos_now = (lu or {}).get("position") or usual_pos
    pm = player_model(m, pr["lambdas"]) if pr.get("lambdas") else {}
    pinfo = (pm.get("players") or {}).get(pid) if pm else None
    exp_min = (pinfo or {}).get("exp_min") or (row.get("exp_min") or 0) or (75.0 if (lu or {}).get("starter") else 30.0)
    starts_min = [r["minutes"] for r in rows[:15] if r["starter"] and r["minutes"]]
    heat = heat_summary(pid, m["kickoff"])
    facts.update({"type": "player", "player": {"id": pid, "name": pname, "team": team_name, "side": side, "pos": pos_now, "usual_pos": usual_pos,
                                                "lineup": (lu or {}).get("source"), "starter": None if not lu else bool(lu["starter"]),
                                                "exp_min": round(exp_min), "rate90": rate, "rate90_last5": rate5, "stat": stat,
                                                "stat_word": STAT_WORD.get(stat or "ga", "goles + asistencias")},
                  "history": hist, "n_hit": n_hit, "n_hist": len(hist), "venue_hit": sum(h["hit"] for h in venue), "venue_n": len(venue),
                  "vs_opp": vs_opp, "opp_name": opp_name, "heat": heat, "line": line, "over": over, "kind": kind})
    # competición y serie larga: el mismo jugador no rinde igual en Champions que en liga, y 10 partidos a veces engañan
    comp_now = comp_label(m.get("comp"))
    long = [{"v": _val(r, stat), "comp": r["comp"], "min": r["minutes"] or 0} for r in rows[:20]]
    same = [x for x in long if x["comp"] == comp_now and x["min"] >= 30]
    if comp_now and len(same) >= 3 and len(same) < len(long):
        hs = sum(_hit(x["v"], over, line) for x in same)
        f_ = hs / len(same)
        facts["comp_split"] = {"comp": comp_now, "hit": hs, "n": len(same)}
        signals.append({"key": "competicion", "dir": 1 if f_ >= 0.6 else -1 if f_ <= 0.4 else 0, "w": 0.9,
                        "text": f"En {comp_now}: habría ganado en {hs} de sus últimos {len(same)} partidos"})
    if len(long) >= 15:
        hl = sum(_hit(x["v"], over, line) for x in long if x["min"] >= 30)
        nl = sum(1 for x in long if x["min"] >= 30)
        if nl >= 12:
            facts["long"] = {"hit": hl, "n": nl}
            signals.append({"key": "serie", "dir": 1 if hl / nl >= 0.6 else -1 if hl / nl <= 0.4 else 0, "w": 0.8,
                            "text": f"Mirando más atrás: {hl} de {nl} (contando partidos de más de 30')"})
    ctx = facts.get("context") or {}
    if ctx.get("joined") and ctx.get("prev_team"):
        signals.append({"key": "fichaje", "dir": 0, "w": 0,
                        "text": f"Solo contamos sus {ctx['team_games']} partidos con {team_name}; antes jugaba en {ctx['prev_team']} con otro rol"})
    # balón parado: quien tira los penaltis o las faltas suma tiros (y goles) que su media de juego no explica
    try:
        from .collectors.tactics import set_piece_roles
        roles = set_piece_roles(tid, m["kickoff"])
    except Exception:
        roles = {}
    if stat in ("shots", "sot", "goals", None) and roles:
        pen = roles.get("penalty") or []
        fk = roles.get("free-kick") or []
        if pen and pen[0][0] == pid and (len(pen) == 1 or pen[0][2] > pen[1][2]):
            facts["pen_taker"] = pen[0][2]
            signals.append({"key": "penaltis", "dir": 1 if over else -1, "w": 1.0 if stat in ("goals", None, "sot") else 0.6,
                            "text": f"Tira los penaltis de {team_name} ({pen[0][2]} en los últimos meses)"})
        if fk and fk[0][0] == pid and stat in ("shots", "sot"):
            facts["fk_taker"] = fk[0][2]
            signals.append({"key": "faltas_directas", "dir": 1 if over else -1, "w": 0.7,
                            "text": f"Es quien tira las faltas directas de {team_name} ({fk[0][2]} tiros de falta)"})
    # calendario: si su equipo vuelve a jugar enseguida (o viene sin descanso) y no hay once oficial, hay riesgo de que rote
    try:
        from .context import schedule
        sc = schedule(tid, m["kickoff"])
        facts["schedule"] = sc
        conf_lu = (db.q1("SELECT 1 AS x FROM lineups WHERE match_id=? AND source='confirmed' LIMIT 1", (m["id"],)) or {}).get("x")
        if not conf_lu and ((sc["next_days"] is not None and sc["next_days"] <= 3.5) or (sc["rest_days"] is not None and sc["rest_days"] < 2.8)):
            why = (f"vuelve a jugar en {sc['next_days']:.0f} días" if sc["next_days"] is not None and sc["next_days"] <= 3.5
                   else f"solo ha descansado {sc['rest_days']:.0f} días")
            signals.append({"key": "rotacion", "dir": -1 if over else 1, "w": 0.8,
                            "text": f"{team_name} {why}: puede que dosifique minutos; mejor con el once oficial"})
    except Exception:
        pass
    # señales: si va a jugar de titular, cuentan sobre todo los partidos en que jugó 60' o más
    full = [x for x in [{"v": _val(r, stat), "min": r["minutes"] or 0} for r in rows[:20]] if x["min"] >= 60][:10]
    facts["n_hit_full"] = sum(_hit(x["v"], over, line) for x in full)
    facts["n_full"] = len(full)
    if exp_min >= 60 and len(full) >= 4 and len(full) < len(hist) - 1:
        frac = facts["n_hit_full"] / len(full)
        signals.append({"key": "racha", "dir": 1 if frac >= 0.6 else -1 if frac <= 0.4 else 0, "w": 1.5,
                        "text": f"Cuando juega más de una hora, habría ganado en {facts['n_hit_full']} de sus últimos {len(full)} partidos"})
        signals.append({"key": "racha10", "dir": 0, "w": 0,
                        "text": f"Contando los partidos que salió de suplente: {n_hit} de {len(hist)}"})
    elif hist:
        frac = n_hit / len(hist)
        signals.append({"key": "racha", "dir": 1 if frac >= 0.6 else -1 if frac <= 0.4 else 0, "w": 1.5,
                        "text": f"Habría ganado en {n_hit} de sus últimos {len(hist)} partidos"})
    if venue:
        fv = sum(h["hit"] for h in venue) / len(venue)
        signals.append({"key": "campo", "dir": 1 if fv >= 0.6 else -1 if fv <= 0.4 else 0, "w": 0.8,
                        "text": f"{'En casa' if side == 'H' else 'Fuera de casa'}: {sum(h['hit'] for h in venue)} de {len(venue)}"})
    if rate and rate5:
        ch = rate5 / rate - 1 if rate else 0
        if abs(ch) >= 0.2:
            signals.append({"key": "tendencia", "dir": (1 if ch > 0 else -1) * (1 if over else -1), "w": 0.8,
                            "text": f"Va {'a más' if rate5 > rate else 'a menos'}: {fmt(rate5, 1)} {STAT_WORD.get(stat, '')} por partido en los últimos 5 (su media es {fmt(rate, 1)})"})
    n_starts = sum(1 for h in hist if h["starter"] and (h["min"] or 0) >= 45)
    facts["n_starts"] = n_starts
    if lu and lu["starter"] and lu["source"] != "confirmed" and len(hist) >= 5 and n_starts <= len(hist) * 0.4:
        facts["start_risk"] = True
        signals.append({"key": "suplencia", "dir": -1 if over else 1, "w": 1.6,
                        "text": f"Solo ha sido titular {n_starts} veces en sus últimos {len(hist)} partidos: espera a la alineación oficial"})
    if lu:
        if lu["starter"]:
            signals.append({"key": "once", "dir": 1 if over else 0, "w": 1.0,
                            "text": f"Sale de titular ({'alineación oficial' if lu['source'] == 'confirmed' else 'alineación prevista'})"})
        else:
            signals.append({"key": "once", "dir": -1 if over else 1, "w": 1.5, "text": "Según la alineación prevista, empieza en el banquillo"})
    elif over and exp_min < 55:
        signals.append({"key": "minutos", "dir": -1, "w": 1.0, "text": f"Aún no hay alineación; calculamos que jugará unos {round(exp_min)} minutos"})
    # rol: posición media reciente frente a la anterior
    if heat and heat.get("recent_x") is not None and heat.get("older_x") is not None:
        dx = heat["recent_x"] - heat["older_x"]
        if abs(dx) >= 5:
            offensive = stat in ("shots", "sot", "goals", "assists", "was_fouled", None)
            d = (1 if dx > 0 else -1) * (1 if offensive else -1) * (1 if over else -1)
            facts["role_shift"] = dx
            signals.append({"key": "rol", "dir": d, "w": 1.0,
                            "text": f"En los 3 últimos partidos juega {'más cerca del área rival' if dx > 0 else 'más atrás'} que antes (según sus mapas de calor)"})
    if stat in ("shots", "sot", "goals", None):
        try:
            from .collectors.tactics import box_trend, heat_profiles
            bt = box_trend(heat_profiles([pid], m["kickoff"]).get(pid))
        except Exception:
            bt = None
        if bt is not None and abs(bt - 1) >= 0.15:
            facts["box_trend"] = round(bt, 3)
            signals.append({"key": "area", "dir": (1 if bt > 1 else -1) * (1 if over else -1), "w": 1.0,
                            "text": f"En sus 3 últimos partidos pisa el área rival un {abs(round((bt - 1) * 100))} % {'más' if bt > 1 else 'menos'} "
                                    f"que antes (mapas de calor): ya lo tenemos en cuenta en su probabilidad"})
    pi = pos_info(pos_now)
    if pos_now and usual_pos and pos_now != usual_pos and pos_info(pos_now) and pos_info(usual_pos) and pos_info(pos_now) != pos_info(usual_pos):
        facts["pos_change"] = [usual_pos, pos_now]
    # rival: qué concede a esa línea
    if stat and pi:
        oa = opp_allowance(m, opp_tid, pi[0], stat)
        if oa and oa.get("ratio"):
            facts["opp_allow"] = {**oa, "line_word": LINE_WORD.get(pi[0], "jugadores")}
            r = oa["ratio"]
            if abs(r - 1) >= 0.12:
                signals.append({"key": "rival", "dir": (1 if r > 1 else -1) * (1 if over else -1), "w": 1.2,
                                "text": f"{opp_name} {'deja hacer mucho' if r > 1 else 'deja hacer poco'} a los {LINE_WORD.get(pi[0], 'jugadores')}: un {abs(round((r - 1) * 100))} % {'más' if r > 1 else 'menos'} de {STAT_WORD.get(stat, '')} de lo normal"})
    # duelo directo
    if stat in DUEL_STAT:
        riv = direct_rivals(m, side, pid, pos_now, heat, stat)
        facts["rivals"] = riv
        for rv in riv[:1]:
            ov = rv.get("overlap")
            if rv["rate"] and rv["lg"] and (rv["n"] or 0) >= 4 and (ov is None or ov >= 0.15):
                r = rv["rate"] / rv["lg"]
                # pesa según cuánto terreno comparten; en tiros, las entradas de un rival influyen poco
                w_ = (1.2 if ov is None else 1.2 * min(1.25, max(0.5, ov / 0.35))) * (0.5 if stat in ("shots", "sot") else 1.0)
                if abs(r - 1) >= 0.15:
                    signals.append({"key": "duelo", "dir": (1 if r > 1 else -1) * (1 if over else -1) * DUEL_SIGN.get(stat, 1), "w": round(w_, 2),
                                    "text": f"Se cruza con {rv['name']}, que {rv['verb'].format(fmt(rv['rate'], 1))} por partido ({abs(round((r - 1) * 100))} % {'más' if r > 1 else 'menos'} de lo normal en su puesto)" + (f"; comparten el {round(ov * 100)} % del terreno" if ov else "")})
    # árbitro
    if stat in ("fouls", "was_fouled", "yellow"):
        ref = referee_info(m)
        if ref:
            facts["referee"] = ref
            key, lg = ("cards", ref["lg_cards"]) if stat == "yellow" else ("fouls", ref["lg_fouls"])
            if ref.get(key) and lg:
                r = ref[key] / lg
                if abs(r - 1) >= 0.1:
                    signals.append({"key": "arbitro", "dir": (1 if r > 1 else -1) * (1 if over else -1), "w": 0.8,
                                    "text": f"El árbitro{(', ' + ref['name'] + ',') if ref['name'] else ''} pita {fmt(ref[key], 1)} {'tarjetas' if key == 'cards' else 'faltas'} por partido (lo normal son {fmt(lg, 1)})"})
    # contexto de equipo para remates y goles
    lam = pr.get("lambdas") or {}
    if stat in ("shots", "sot", "goals", "assists", None) and lam:
        lt = lam["lh"] if side == "H" else lam["la"]
        signals.append({"key": "equipo", "dir": (1 if lt >= 1.55 else -1 if lt <= 1.05 else 0) * (1 if over else -1), "w": 0.8,
                        "text": f"Esperamos que {team_name} marque {fmt(lt, 1)} goles de media"})
    if stat == "saves" and lam:
        lo = lam["la"] if side == "H" else lam["lh"]
        signals.append({"key": "equipo", "dir": (1 if lo >= 1.5 else -1 if lo <= 1.0 else 0) * (1 if over else -1), "w": 0.8,
                        "text": f"Esperamos {fmt(lo, 1)} goles del rival: {'mucho' if lo >= 1.5 else 'poco'} trabajo para el portero"})
    # simulación
    if pinfo and "lam" in pinfo:
        lk = {"goal": "goal", "first": None, "last": None, "assist": "assist", "shots": "shots", "sot": "sot", "tackles": "tackles",
              "fouls": "fouls", "fouled": "fouled", "cards": "cards", "saves": "saves", "goal_assist": None}.get(kind)
        if lk and pinfo["exp_min"] > 0:
            r90 = pinfo["lam"][lk] / pinfo["exp_min"] * 90
            facts["sim"] = sim_player(r90, pinfo["exp_min"], starts_min, DISP.get(lk), over, line)
        elif kind == "goal_assist":
            r90 = (pinfo["lam"]["goal"] + pinfo["lam"]["assist"]) / pinfo["exp_min"] * 90
            facts["sim"] = sim_player(r90, pinfo["exp_min"], starts_min, None, True, 0.5)
    elif rate is not None:
        facts["sim"] = sim_player(rate, exp_min, starts_min, None, over, line)


def _count_insight(m, pr, row, meta, facts, signals) -> None:
    from .pricing import count_models
    if meta.get("t") == "count":
        code, side, over, line, per = meta["code"], meta.get("side"), meta["over"], float(meta["line"]), meta.get("per", "FT")
    else:
        code = row["market"].split("_")[0]
        sel = row["selection"]
        side = sel[0] if len(sel) == 2 else None
        over = sel[-1] == "O"
        line = float(row["line"] or 0)
        per = row["period"]
    key, word = TEAM_KEY.get(code, (None, None))
    if not key:
        return _generic_insight(m, pr, row, facts, signals)
    hh, ha = team_history(m["home_id"], m["kickoff"]), team_history(m["away_id"], m["kickoff"])
    lg = league_refs()["team"].get("T:" + code)

    def series(h):
        return [{"kickoff": d["kickoff"], "opp": d["opp"], "home": d["home"], "for": team_stat(d["for"], key),
                 "against": team_stat(d["against"], key)} for d in h]
    sh, sa = series(hh), series(ha)
    facts.update({"type": "count", "code": code, "word": word, "side": side, "line": line, "over": over, "lg": lg,
                  "home_series": sh, "away_series": sa})

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return float(np.mean(xs)) if xs else None
    hf, hag, af, aag = mean([x["for"] for x in sh]), mean([x["against"] for x in sh]), mean([x["for"] for x in sa]), mean([x["against"] for x in sa])
    facts["means"] = {"home_for": hf, "home_against": hag, "away_for": af, "away_against": aag}
    # partidos de cada equipo que habrían pasado con esta línea
    def tot(x, who):
        if x["for"] is None or x["against"] is None:
            return None
        return x["for"] if who == "own" else x["for"] + x["against"]
    if side in ("H", "A"):
        ser = sh if side == "H" else sa
        vals = [x["for"] for x in ser if x["for"] is not None]
        nh = sum(_hit(v, over, line) for v in vals)
        facts["n_hit"], facts["n_hist"] = nh, len(vals)
        if vals:
            signals.append({"key": "racha", "dir": 1 if nh / len(vals) >= 0.6 else -1 if nh / len(vals) <= 0.4 else 0, "w": 1.5,
                            "text": f"Habría ganado en {nh} de los últimos {len(vals)} partidos de {m['home_name'] if side == 'H' else m['away_name']}"})
        opp_ag = aag if side == "H" else hag
        if opp_ag and lg:
            r = opp_ag / lg
            if abs(r - 1) >= 0.1:
                signals.append({"key": "rival", "dir": (1 if r > 1 else -1) * (1 if over else -1), "w": 1.2,
                                "text": f"A {m['away_name'] if side == 'H' else m['home_name']} sus rivales le hacen {fmt(opp_ag)} {word} por partido (lo normal son {fmt(lg)})"})
    else:
        nh = n = 0
        for ser in (sh, sa):
            for x in ser:
                t = tot(x, "all")
                if t is not None:
                    n += 1
                    nh += _hit(t, over, line)
        facts["n_hit"], facts["n_hist"] = nh, n
        if n:
            signals.append({"key": "racha", "dir": 1 if nh / n >= 0.6 else -1 if nh / n <= 0.4 else 0, "w": 1.5,
                            "text": f"Habría ganado en {nh} de los últimos {n} partidos de estos dos equipos"})
        if lg and hf and aag and af and hag:
            cross_h, cross_a = (hf / lg) * (aag / lg), (af / lg) * (hag / lg)
            for nm, c, a, b in ((m["home_name"], cross_h, hf, aag), (m["away_name"], cross_a, af, hag)):
                if abs(c - 1) >= 0.15:
                    signals.append({"key": "cruce", "dir": (1 if c > 1 else -1) * (1 if over else -1), "w": 1.0,
                                    "text": f"{nm} hace {fmt(a)} {word} por partido y a su rival le hacen {fmt(b)} (lo normal son {fmt(lg)})"})
    if code in ("CARDS", "FOULS"):
        ref = referee_info(m)
        if ref:
            facts["referee"] = ref
            k2, lgv = ("cards", ref["lg_cards"]) if code == "CARDS" else ("fouls", ref["lg_fouls"])
            if ref.get(k2) and lgv:
                r = ref[k2] / lgv
                if abs(r - 1) >= 0.08:
                    signals.append({"key": "arbitro", "dir": (1 if r > 1 else -1) * (1 if over else -1), "w": 1.2,
                                    "text": f"El árbitro{(', ' + ref['name'] + ',') if ref['name'] else ''} pita {fmt(ref[k2])} {word} por partido (lo normal son {fmt(lgv)})"})
    cm = count_models(m)
    if code in cm:
        facts["sim"] = sim_count(cm[code]["model"], side, over, line, per)
        facts["mu"] = cm[code]["mu"]
    elif None not in (hf, hag, af, aag):
        from .models.secondary import CountForecast
        mh, ma = (hf + aag) / 2, (af + hag) / 2
        facts["sim"] = sim_count(CountForecast(mh, ma, 20.0), side, over, line, per)
        facts["mu"] = [round(mh, 2), round(ma, 2)]


def _goals_insight(m, pr, row, facts, signals) -> None:
    lam = pr.get("lambdas") or {}
    if not lam:
        return
    hh, ha = team_history(m["home_id"], m["kickoff"]), team_history(m["away_id"], m["kickoff"])

    def ser(h):
        return [{"kickoff": d["kickoff"], "opp": d["opp"], "home": d["home"], "gf": d["gf"], "ga": d["ga"],
                 "xg": team_stat(d["for"], "xg"), "xga": team_stat(d["against"], "xg")} for d in h]
    sh, sa = ser(hh), ser(ha)
    facts.update({"type": "goals", "home_series": sh, "away_series": sa, "lam": lam})
    sim = sim_goals(lam["lh"], lam["la"], row["market"], row["selection"], row["line"])
    if sim:
        facts["sim"] = sim
    mk, sel, L = row["market"], row["selection"], row["line"]

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return float(np.mean(xs)) if xs else None
    xh, xha, xa, xaa = mean([x["xg"] for x in sh]), mean([x["xga"] for x in sh]), mean([x["xg"] for x in sa]), mean([x["xga"] for x in sa])
    facts["means"] = {"home_xg": xh, "home_xga": xha, "away_xg": xa, "away_xga": xaa}
    # racha del mercado en sus partidos
    def hit_goal(x, who):
        g, a = x["gf"], x["ga"]
        if g is None or a is None:
            return None
        if mk == "OU":
            return _hit(g + a, sel == "O", float(L))
        if mk == "BTTS":
            return (g > 0 and a > 0) == (sel == "Y")
        if mk == "TEAM_OU" and who == sel[0]:
            return _hit(g, sel[1] == "O", float(L))
        return None
    hits = [h for x in sh for h in [hit_goal(x, "H")] if h is not None] + [h for x in sa for h in [hit_goal(x, "A")] if h is not None]
    if hits:
        facts["n_hit"], facts["n_hist"] = sum(hits), len(hits)
        f = sum(hits) / len(hits)
        signals.append({"key": "racha", "dir": 1 if f >= 0.6 else -1 if f <= 0.4 else 0, "w": 1.3,
                        "text": f"Habría ganado en {sum(hits)} de los últimos {len(hits)} partidos de {'estos dos equipos' if mk != 'TEAM_OU' else 'ese equipo'}"})
    want_goals = (mk == "OU" and sel == "O") or (mk == "BTTS" and sel == "Y") or (mk == "TEAM_OU" and sel[-1] == "O")
    no_goals = (mk == "OU" and sel == "U") or (mk == "BTTS" and sel == "N") or (mk == "TEAM_OU" and sel[-1] == "U")
    if (want_goals or no_goals) and None not in (xh, xaa, xa, xha):
        exp = (xh + xaa) / 2 + (xa + xha) / 2
        signals.append({"key": "xg", "dir": (1 if exp >= 2.8 else -1 if exp <= 2.3 else 0) * (1 if want_goals else -1), "w": 1.2,
                        "text": f"Por las ocasiones que generan y conceden (xG), esperamos {fmt(exp, 1)} goles entre los dos"})
    if want_goals or no_goals:
        try:
            from .context import weather
            w = (db.kv_get(f"weather3:{m['id']}") or {}).get("w") or weather(m["id"])
        except Exception:
            w = None
        if w and (w["rain"] >= 3 or w["wind"] >= 35):
            # lluvia fuerte o viento fuerte: menos juego limpio y menos goles (el peso se reajusta con los resultados)
            signals.append({"key": "clima", "dir": -1 if want_goals else 1, "w": 0.8,
                            "text": f"Tiempo previsto: {w['rain']:g} mm de lluvia y viento de {w['wind']} km/h en {w.get('city') or 'el estadio'}"})
    if mk in ("1X2", "DNB", "AH", "DC"):
        fav = "H" if lam["lh"] > lam["la"] else "A"
        mine = sel[0] if sel in ("H", "A", "D") else ("H" if sel in ("1X",) else "A" if sel == "X2" else None)
        for side, s in (("H", sh), ("A", sa)):
            pts = [3 if x["gf"] > x["ga"] else 1 if x["gf"] == x["ga"] else 0 for x in s[:6] if x["gf"] is not None]
            if pts and mine == side:
                f = sum(pts) / (3 * len(pts))
                signals.append({"key": "forma", "dir": 1 if f >= 0.6 else -1 if f <= 0.35 else 0, "w": 1.0,
                                "text": f"{m['home_name'] if side == 'H' else m['away_name']} ha sumado {sum(pts)} de {3 * len(pts)} puntos en sus últimos {len(pts)} partidos"})
        if mine in ("H", "A"):
            signals.append({"key": "modelo", "dir": 1 if mine == fav else -1, "w": 0.6,
                            "text": f"Esperamos {fmt(lam['lh'], 1)} goles de {m['home_name']} y {fmt(lam['la'], 1)} de {m['away_name']}"})


def _generic_insight(m, pr, row, facts, signals) -> None:
    facts.setdefault("type", "generic")
    lam = pr.get("lambdas") or {}
    if lam:
        facts["lam"] = lam


def _market_signals(row, facts, signals) -> None:
    pm, pc, p = row.get("p_model"), row.get("p_market"), row.get("p")
    b = row.get("b365")
    if b and p:
        implied = 1 / b
        push = row.get("push") or 0
        ev = p * b + push - 1
        txt = (f"Creemos que sale {round(p * 100)} de cada 100 veces; bet365 la paga como si saliera {round(implied * 100)}" if push < 0.02 else
               f"Creemos que gana {round(p * 100)} de cada 100 veces y otras {round(push * 100)} te devuelven el dinero: a esta cuota compensa"
               if ev > 0 else f"Gana {round(p * 100)} de cada 100 veces y otras {round(push * 100)} te devuelven el dinero: a esta cuota no compensa")
        signals.append({"key": "precio", "dir": 1 if ev > 0.02 else -1 if ev < 0 else 0, "w": 2.0, "text": txt})
    if pm is not None and pc is not None:
        agree = abs(pm - pc) < 0.06
        signals.append({"key": "consenso", "dir": 1 if agree else 0, "w": 0.6,
                        "text": f"Nuestro modelo dice {round(pm * 100)} % y las demás casas de apuestas {round(pc * 100)} %" + (": coinciden" if agree else "")})
    o, c = row.get("b365_open"), row.get("b365")
    if o and c and abs(c / o - 1) >= 0.04:
        signals.append({"key": "movimiento", "dir": 1 if c < o else -1, "w": 0.7,
                        "text": f"La cuota ha {'bajado' if c < o else 'subido'} de {fmt(o, 2)} a {fmt(c, 2)}: {'mucha gente la está apostando' if c < o else 'el mercado la ve menos clara'}"})
    s = facts.get("sim")
    if s and s.get("p") is not None and b:
        signals.append({"key": "simulacion", "dir": 1 if s["p"] > 1 / b else -1, "w": 1.0,
                        "text": f"Simulando el partido {s['n']:,} veces, sale el {round(s['p'] * 100)} %".replace(",", ".")})


# ---------------------------------------------------------------------------- texto de tipster

def _edge_signals(m, row, meta, facts, signals) -> None:
    """Lo que el modelo ya usa y antes no se contaba en el análisis: casas afiladas, cuota de bet365 que se dispara,
    cómo llegan los equipos (toques en el área), lo que se juega cada uno y el tiempo en los mercados de conteo."""
    import time as _t
    mk, sel, per = row.get("market") or "", str(row.get("selection") or ""), row.get("period") or "FT"
    ln = None if row.get("line") is None else round(float(row["line"]), 2)
    names = {"H": m.get("home_name"), "A": m.get("away_name")}
    # 1) casas afiladas (Pinnacle / Betfair): si han movido su probabilidad a favor o en contra en las últimas horas
    if m.get("status") == "notstarted" and per == "FT" and mk in ("1X2", "OU", "AH"):
        from .steam import _at, _devig, _groups
        grp = _groups(mk, sel, ln)
        if grp:
            now = int(_t.time())
            rows = db.q("SELECT bookmaker, selection, line, odds, ts FROM odds WHERE match_id=? AND period='FT' AND market=? "
                        "AND bookmaker IN ('Pinnacle','Betfair Exchange') AND phase IN ('open','snap','close') AND ts>=? ORDER BY ts",
                        (m["id"], mk, now - 48 * 3600))
            best = None
            for bk in ("Pinnacle", "Betfair Exchange"):
                hist: dict = {}
                for r in rows:
                    if r["bookmaker"] == bk:
                        hist.setdefault((r["selection"], None if r["line"] is None else round(float(r["line"]), 2)), []).append((r["ts"], r["odds"]))
                pn = _devig([_at(hist.get(g, []), now) for g in grp])
                po = _devig([_at(hist.get(g, []), now - 3 * 3600) or _at(hist.get(g, []), now - 45 * 60) for g in grp])
                i = next((j for j, g in enumerate(grp) if g[0] == sel), None)
                if pn and po and i is not None and (best is None or abs(pn[i] - po[i]) > abs(best[1])):
                    best = (bk, pn[i] - po[i], po[i], pn[i])
            if best and abs(best[1]) >= 0.02:
                bk, mv, a, b = best
                facts["sharp_move"] = {"book": bk, "from": round(a, 4), "to": round(b, 4)}
                signals.append({"key": "afiladas", "dir": 1 if mv > 0 else -1, "w": 1.5 if abs(mv) >= 0.03 else 1.0,
                                "text": f"{bk}, la casa que mejor anticipa, la ha pasado de un {round(a * 100)} % a un {round(b * 100)} % en las "
                                        f"últimas horas: {'ha entrado dinero o una noticia a favor' if mv > 0 else 'el dinero informado va en contra'}"})
    # 2) bet365 ha alargado mucho la cuota en pocas horas y el modelo es de antes: posible baja que aún no sabemos
    if m.get("status") == "notstarted" and row.get("b365"):
        try:
            from .plans import b365_drift
            r_ = b365_drift(m["id"]).get((mk, per, sel, ln))
        except Exception:
            r_ = None
        if r_ and r_ >= 1.12:
            pc = db.q1("SELECT created FROM predictions WHERE match_id=? AND model='prematch'", (m["id"],))
            ab = db.kv_get(f"absr:{m['id']}") or {}
            if (not pc or pc["created"] < _t.time() - 3 * 3600) and not (ab and _t.time() - ab.get("ts", 0) < 1800):
                facts["move_warn"] = True
                signals.append({"key": "aviso_cuota", "dir": -1, "w": 2.0,
                                "text": f"bet365 la ha pasado de {fmt(row['b365'] / r_, 2)} a {fmt(row['b365'], 2)} en pocas horas: puede haber "
                                        f"una baja o noticia que aún no tenemos. Mejor esperar al once"})
    code = mk.split("_")[0]
    over = sel[-1:] == "O" if mk.endswith("_OU") else None
    side = (meta.get("side") if meta.get("t") == "count" else (sel[0] if mk.endswith("_OU") and len(sel) == 2 else None))
    if meta.get("t") == "count":
        code, over = meta.get("code") or code, meta.get("over")
    # 3) cómo llegan: toques en el área rival últimamente frente a su media (córners y tiros)
    if code in ("CORNERS", "SHOTS", "SOT") and over is not None:
        try:
            from .pricing import count_models
            proc = (count_models(m).get(code) or {}).get("proc") or {}
        except Exception:
            proc = {}
        for s_ in ("H", "A"):
            f_ = proc.get(s_, 1.0)
            if abs(f_ - 1) < 0.05 or (side and side != s_):
                continue
            up = f_ > 1
            signals.append({"key": "tendencia_area", "dir": (1 if up else -1) * (1 if over else -1), "w": 0.8,
                            "text": f"{names[s_]} pisa el área rival {'bastante más' if up else 'bastante menos'} en sus últimos partidos que en su media "
                                    f"(≈{'+' if up else ''}{round((f_ ** 2 - 1) * 100)} % de toques): sus {'córners' if code == 'CORNERS' else 'tiros'} lo notan antes que la media"})
    # 4) tiempo en conteos: viento → más córners; lluvia → más faltas y tarjetas
    if code in ("CORNERS", "FOULS", "CARDS") and over is not None:
        w = (db.kv_get(f"weather3:{m['id']}") or {}).get("w") or {}
        wind, rain = float(w.get("wind") or 0), float(w.get("rain") or 0)
        if code == "CORNERS" and wind >= 30:
            signals.append({"key": "clima", "dir": 1 if over else -1, "w": 0.6,
                            "text": f"Viento previsto de {wind:.0f} km/h: más balones despejados a córner"})
        if code in ("FOULS", "CARDS") and rain >= 3:
            signals.append({"key": "clima", "dir": 1 if over else -1, "w": 0.5,
                            "text": f"Lluvia prevista ({rain:g} mm): campo mojado, más entradas a destiempo"})
    # 5) lo que se juega cada uno (tramo final de liga) y viento/lluvia en los goles esperados
    if mk in ("1X2", "DNB", "AH", "DC", "OU", "TEAM_OU", "BTTS"):
        try:
            from .context import context_factors
            cf = context_factors(m)
        except Exception:
            cf = {}
        fav = _fav_side(row)
        for why in cf.get("why") or []:
            if why.startswith("tiempo:"):
                if mk in ("OU", "BTTS", "TEAM_OU") and not any(s_["key"] == "clima" for s_ in signals):
                    want = sel[-1:] in ("O", "Y")
                    signals.append({"key": "clima", "dir": -1 if want else 1, "w": 0.6,
                                    "text": f"Previsión de {why[8:]}: el modelo ya baja un poco los goles esperados"})
                continue
            lazy = "H" if names["H"] and why.startswith(names["H"]) else "A"
            d = 0 if not fav else (-1 if fav == lazy else 1)
            if mk in ("OU", "BTTS") and not fav:
                continue
            signals.append({"key": "jugarse", "dir": d, "w": 0.8, "text": f"{why[0].upper()}{why[1:]}: suele rendir algo menos"})

def _first(name: str | None) -> str:
    if not name:
        return ""
    parts = name.split()
    return parts[-1] if len(parts) > 1 and len(parts[-1]) > 2 else name


def tipster_text(m: dict, row: dict, f: dict) -> list[str]:
    """Párrafos cortos, en lenguaje de tipster, solo con hechos que están en `f`."""
    out: list[str] = []
    ev, b = row.get("ev"), row.get("b365")
    t = f.get("type")
    if t == "player":
        pl = f["player"]
        nm = _first(pl["name"])
        w = pl["stat_word"]
        line, over = f["line"], f["over"]
        h = f["history"]
        if h:
            vals = ", ".join(fmt(x["v"], 0) for x in h[:6])
            s = f"{nm} habría cumplido en {f['n_hit']} de sus últimos {f['n_hist']} partidos ({vals} en los seis más recientes)"
            if f.get("venue_n"):
                s += f"; {f['venue_hit']} de {f['venue_n']} {'en casa' if pl['side'] == 'H' else 'lejos de casa'}"
            out.append(s + ".")
            ctx = f.get("context") or {}
            if ctx.get("joined") and ctx.get("prev_team"):
                out.append(f"Ojo: llegó a {pl['team']} desde {ctx['prev_team']}, así que solo contamos sus {ctx['team_games']} partidos con su equipo actual.")
            if f.get("pen_taker"):
                out.append(f"Además es quien tira los penaltis: un penalti a favor y le sube mucho la probabilidad.")
            if f.get("fk_taker"):
                out.append("También lanza las faltas directas, que son tiros extra.")
            cs = f.get("comp_split")
            if cs:
                out.append(f"En {cs['comp']} habría cumplido en {cs['hit']} de {cs['n']}" + (f"; mirando sus últimos {f['long']['n']} partidos, {f['long']['hit']}." if f.get("long") else "."))
            elif f.get("long"):
                out.append(f"Mirando más atrás, en sus últimos {f['long']['n']} partidos habría cumplido en {f['long']['hit']}.")
            short = sum(1 for x in h if (x["min"] or 0) < 45)
            if short >= 2 and f.get("n_full"):
                out.append(f"Pero ojo a los minutos: en {short} de esos partidos jugó menos de 45 minutos. Contando solo sus últimos "
                           f"{f['n_full']} partidos de 60' o más, habría cumplido en {f['n_hit_full']} de ellos.")
        if pl.get("rate90") is not None:
            s = f"Promedia {fmt(pl['rate90'], 2)} {w} por 90 minutos"
            if pl.get("rate90_last5") is not None and pl["rate90"] and abs(pl["rate90_last5"] / pl["rate90"] - 1) >= 0.2:
                s += f" y viene {'al alza' if pl['rate90_last5'] > pl['rate90'] else 'a la baja'}: {fmt(pl['rate90_last5'], 2)} en los últimos cinco"
            out.append(s + ".")
        coach = (f.get("managers") or {}).get(pl["side"])
        if f.get("role_shift"):
            dx = f["role_shift"]
            who = f"con {_first(coach)} " if coach else ""
            out.append(f"Ojo al rol: {who}está jugando {'más cerca del área rival' if dx > 0 else 'más retrasado'} en los tres últimos partidos "
                       f"(su posición media ha {'subido' if dx > 0 else 'bajado'} {round(abs(dx))} puntos sobre 100 en el mapa de calor).")
        if f.get("pos_change"):
            a, b2 = f["pos_change"]
            out.append(f"En el once previsto aparece como {LINE_ONE.get((pos_info(b2) or ('',))[0], b2)} ({b2}) cuando lo habitual es {a}: cambio de rol.")
        oa = f.get("opp_allow")
        if oa and oa.get("ratio") and abs(oa["ratio"] - 1) >= 0.12:
            out.append(f"{f['opp_name']} {'sufre' if oa['ratio'] > 1 else 'controla bien'} a los {oa['line_word']}: le hacen {fmt(oa['rate'], 2)} {w} por 90' "
                       f"en sus últimos {oa['games']} partidos, un {abs(round((oa['ratio'] - 1) * 100))} % {'más' if oa['ratio'] > 1 else 'menos'} que la media de ese puesto.")
        for rv in (f.get("rivals") or [])[:1]:
            if rv.get("rate") and rv.get("lg") and (rv.get("n") or 0) >= 4:
                r = rv["rate"] / rv["lg"]
                s = f"El duelo clave es con {_first(rv['name'])} ({rv['pos']}), que {rv['verb'].format(fmt(rv['rate'], 2))} por 90'"
                s += f" ({'por encima' if r > 1 else 'por debajo'} de lo normal en su puesto)" if abs(r - 1) >= 0.1 else " (lo normal en su puesto)"
                if rv.get("overlap") is not None:
                    s += f"; por sus mapas de calor comparten el {round(rv['overlap'] * 100)} % del terreno"
                if abs(r - 1) >= 0.1:
                    d = (1 if r > 1 else -1) * (1 if f.get("over", True) else -1) * DUEL_SIGN.get((pl or {}).get("stat") or "", 1)
                    s += ": juega a favor de la apuesta" if d > 0 else ": se lo pone más difícil"
                out.append(s + ".")
        ref = f.get("referee")
        if ref:
            k = "cards" if pl["stat"] == "yellow" else "fouls"
            lg = ref["lg_cards"] if k == "cards" else ref["lg_fouls"]
            if ref.get(k) and lg and abs(ref[k] / lg - 1) >= 0.1:
                out.append(f"Pita {ref['name'] or 'un árbitro'} que saca {fmt(ref[k])} {'tarjetas' if k == 'cards' else 'faltas'} por partido (media {fmt(lg)}).")
        if f.get("start_risk"):
            out.append(f"Riesgo importante: el once previsto lo pone de titular, pero solo ha salido de inicio en {f['n_starts']} de sus últimos "
                       f"{f['n_hist']} partidos. Si no lo ves en el once confirmado (aprox. una hora antes), no la hagas.")
        elif pl.get("lineup"):
            out.append(f"{'Sale de titular' if pl.get('starter') else 'Empieza en el banquillo'} en el once {'confirmado' if pl['lineup'] == 'confirmed' else 'previsto'}; "
                       f"calculamos unos {pl['exp_min']} minutos.")
    elif t == "count":
        w = f["word"]
        mu = f.get("mu")
        mm = f.get("means") or {}
        if mm.get("home_for") is not None and mm.get("away_for") is not None:
            if f.get("code") in ("CARDS", "FOULS", "OFFSIDES"):
                hv = {"CARDS": ("le sacan", "a sus rivales"), "FOULS": ("comete", "sus rivales"), "OFFSIDES": ("cae en", "sus rivales")}[f["code"]]
                out.append(f"En sus últimos 10, a {m['home_name']} " if f["code"] == "CARDS" else f"En sus últimos 10, {m['home_name']} ")
                out[-1] += (f"{hv[0]} {fmt(mm['home_for'])} {w} por partido y {hv[1]}, {fmt(mm['home_against'])}; a {m['away_name']}, "
                            f"{fmt(mm['away_for'])} y {fmt(mm['away_against'])}" if f["code"] == "CARDS" else
                            f"{hv[0]} {fmt(mm['home_for'])} {w} por partido y {hv[1]}, {fmt(mm['home_against'])}; {m['away_name']}, "
                            f"{fmt(mm['away_for'])} y {fmt(mm['away_against'])}") + (f" (media de liga {fmt(f['lg'])} por equipo)." if f.get("lg") else ".")
            else:
                out.append(f"En sus últimos 10, {m['home_name']} hace {fmt(mm['home_for'])} {w} por partido y concede {fmt(mm['home_against'])}; "
                           f"{m['away_name']}, {fmt(mm['away_for'])} y {fmt(mm['away_against'])}" + (f" (media de liga {fmt(f['lg'])} por equipo)." if f.get("lg") else "."))
        if f.get("n_hist"):
            out.append(f"Con esta línea ({fline(f['line'])}) habría pasado {f['n_hit']} de {f['n_hist']} veces.")
        if mu:
            out.append(f"El modelo espera {fmt(mu[0])} de {m['home_name']} y {fmt(mu[1])} de {m['away_name']}.")
        ref = f.get("referee")
        if ref and f["code"] in ("CARDS", "FOULS"):
            k = "cards" if f["code"] == "CARDS" else "fouls"
            lg = ref["lg_cards"] if k == "cards" else ref["lg_fouls"]
            if ref.get(k):
                out.append(f"{ref['name'] or 'El árbitro'} lleva {fmt(ref[k])} {w} por partido en sus últimos {ref['games']} (media {fmt(lg)}).")
    elif t == "goals":
        lam = f.get("lam") or {}
        mm = f.get("means") or {}
        if lam:
            tot = lam['lh'] + lam['la']
            sp = (f.get("sim") or {}).get("p")
            out.append(f"El modelo espera {fmt(lam['lh'], 2)} goles de {m['home_name']} y {fmt(lam['la'], 2)} de {m['away_name']} "
                       f"({fmt(tot, 2)} en total)" + (f"; en las simulaciones esta apuesta sale el {round(sp * 100)} % de las veces." if sp else "."))
        if mm.get("home_xg") is not None and mm.get("away_xg") is not None:
            out.append(f"Últimos 10 por xG: {m['home_name']} genera {fmt(mm['home_xg'], 2)} y concede {fmt(mm['home_xga'], 2)}; "
                       f"{m['away_name']} genera {fmt(mm['away_xg'], 2)} y concede {fmt(mm['away_xga'], 2)}.")
        if f.get("n_hist"):
            out.append(f"Esta apuesta habría pasado en {f['n_hit']} de sus últimos {f['n_hist']} partidos.")
        sc = (f.get("sim") or {}).get("scores")
        if sc:
            out.append("Marcadores más repetidos en las simulaciones: " + ", ".join(f"{k} ({round(v * 100)} %)" for k, v in sc[:3]) + ".")
    s = f.get("sim")
    if s and b:
        p = row.get("p")
        txt = f"En {s['n']:,} simulaciones del partido pasa el {round(s['p'] * 100)} %".replace(",", ".", 1)
        if p is not None and abs(s["p"] - p) >= 0.06:
            txt += (f". Ojo: esa simulación usa solo sus números; mezclándola con lo que dicen las demás casas, el precio justo queda en "
                    f"un {round(p * 100)} %")
        out.append(txt + f". bet365 paga {fmt(b, 2)}, como si pasara el {round(100 / b)} %.")
    if ev is not None:
        pro, con = f.get("n_pro", 0), f.get("n_con", 0)
        if ev > 0.02:
            out.append(f"Veredicto: {pro} señales a favor y {con} en contra. La cuota tiene un {round(ev * 100)} % de valor; "
                       f"la tomaría hasta {fmt(1 / max(row['p'], 1e-6) * 1.02, 2)}.")
        else:
            out.append(f"Veredicto: la cuota no compensa ({'+' if ev > 0 else ''}{round(ev * 100)} %). Haría falta {fmt(1 / max(row['p'], 1e-6) * 1.03, 2)} o más.")
    return out


# ---------------------------------------------------------------------------- análisis del partido
def match_crosses(mid: int) -> dict:
    """Lo que hace cada equipo × lo que concede el rival, frente a la media; y duelos por banda."""
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {}
    refs = league_refs()
    lgk = refs["team"]
    hh, ha = team_history(m["home_id"], m["kickoff"]), team_history(m["away_id"], m["kickoff"])
    out = []

    def mean(h, side, key):
        xs = [team_stat(d[side], key) for d in h]
        xs = [x for x in xs if x is not None]
        return (float(np.mean(xs)), len(xs)) if len(xs) >= 4 else (None, len(xs))
    for key, name in CROSS_KEYS:
        lg = lgk.get(key)
        if not lg:
            continue
        for who, h_for, h_ag, nm, onm in (("H", hh, ha, m["home_name"], m["away_name"]), ("A", ha, hh, m["away_name"], m["home_name"])):
            f, nf = mean(h_for, "for", key)
            a, na = mean(h_ag, "against", key)
            if f is None or a is None:
                continue
            rf, ra = f / lg, a / lg
            proj = lg * rf * ra
            same = (rf >= 1.12 and ra >= 1.12) or (rf <= 0.88 and ra <= 0.88)
            out.append({"key": key, "name": name, "side": who, "team": nm, "opp": onm, "for": f, "against": a, "lg": lg,
                        "proj": proj, "aligned": same, "dir": 1 if rf > 1 and ra > 1 else -1 if rf < 1 and ra < 1 else 0,
                        "strength": abs(math.log(max(rf, 1e-3))) + abs(math.log(max(ra, 1e-3)))})
    out.sort(key=lambda x: (-x["aligned"], -x["strength"]))
    duels = flank_duels(m)
    ref = referee_info(m)
    return {"crosses": out, "duels": duels, "referee": ref, "managers": managers(mid)}


def flank_duels(m: dict) -> list[dict]:
    """Por cada atacante de banda/punta del once previsto, su par rival y los cruces de faltas, entradas y regates."""
    out = []
    refs = league_refs()["pos"]
    for side in ("H", "A"):
        tid = m["home_id"] if side == "H" else m["away_id"]
        for p in expected_lineup(m["id"], side, tid, m["kickoff"]):
            pi = pos_info(p["position"])
            if not pi or pi[0] not in ("W", "WM", "F", "AM"):
                continue
            heat = heat_summary(p["player_id"], m["kickoff"], 6)
            rv = direct_rivals(m, side, p["player_id"], p["position"], heat, "was_fouled")
            if not rv:
                continue
            d = rv[0]
            lgf = refs.get(pi[0], {}).get("was_fouled")
            lgd = refs.get(d["line"], {}).get("fouls")
            fouled, _, _ = player_rate(p["player_id"], m["kickoff"], "was_fouled", prior=lgf)
            drib, _, _ = player_rate(p["player_id"], m["kickoff"], "won_contest", prior=refs.get(pi[0], {}).get("won_contest"))
            tk, _, _ = player_rate(d["player_id"], m["kickoff"], "tackles", prior=refs.get(d["line"], {}).get("tackles"))
            idx = ((fouled or 0) / lgf if lgf else 1) * ((d["rate"] or 0) / lgd if lgd else 1)
            if d.get("overlap") is not None:          # cuanto más terreno comparten, más se van a cruzar
                idx *= min(1.3, max(0.5, d["overlap"] / 0.35)) ** 0.5
            out.append({"side": side, "attacker": {"id": p["player_id"], "name": p["name"], "pos": p["position"], "fouled": fouled,
                                                   "dribbles": drib, "lg_fouled": lgf},
                        "defender": {"id": d["player_id"], "name": d["name"], "pos": d["pos"], "fouls": d["rate"], "tackles": tk, "lg_fouls": lgd},
                        "overlap": d.get("overlap"), "foul_index": idx,
                        "zone": f"banda {SIDE_WORD[pi[1]]}" if pi[1] != "C" else "por el centro"})
    out.sort(key=lambda x: -x["foul_index"])
    return out


def match_simulation(mid: int) -> dict:
    """10.000 partidos simulados: goles, marcadores y estadísticas de cada equipo."""
    from .pricing import count_models, price_match
    pr = price_match(mid)
    if not pr or not pr.get("lambdas"):
        return {}
    m, lam = pr["match"], pr["lambdas"]
    g = sim_goals(lam["lh"], lam["la"], "1X2", "H", None) or {}
    s = _SIMS.get((round(lam["lh"], 3), round(lam["la"], 3)))
    out = {"n": N_SIMS, "lam": lam, "home_win": g.get("home_win"), "draw": g.get("draw"), "away_win": g.get("away_win"),
           "scores": g.get("scores"), "goals_hist": g.get("hist")}
    if s is not None:
        fh, fa = s.fh, s.fa
        out["btts"] = float(((fh > 0) & (fa > 0)).mean())
        out["over25"] = float((fh + fa > 2).mean())
        out["home_cs"] = float((fa == 0).mean())
        out["away_cs"] = float((fh == 0).mean())
        out["first_half"] = None
    cm = count_models(m)
    rng = np.random.default_rng(9)
    stats = []
    hh, ha = team_history(m["home_id"], m["kickoff"]), team_history(m["away_id"], m["kickoff"])
    for code, (key, word) in TEAM_KEY.items():
        if code in cm:
            f = cm[code]["model"]
        else:
            from .models.secondary import CountForecast
            vals = [[team_stat(d[s], key) for d in h] for h, s in ((hh, "for"), (ha, "against"), (ha, "for"), (hh, "against"))]
            vals = [[v for v in vs if v is not None] for vs in vals]
            if any(len(vs) < 4 for vs in vals):
                continue
            f = CountForecast((np.mean(vals[0]) + np.mean(vals[1])) / 2, (np.mean(vals[2]) + np.mean(vals[3])) / 2, 20.0)
        a = _nb(rng, f.mu_h, f.disp, N_SIMS)
        b = _nb(rng, f.mu_a, f.disp, N_SIMS)
        t = a + b
        qs = np.percentile(t, [10, 50, 90]).tolist()
        stats.append({"code": code, "word": word, "home": float(a.mean()), "away": float(b.mean()), "total": float(t.mean()),
                      "p10": qs[0], "p50": qs[1], "p90": qs[2], "home_more": float((a > b).mean()), "away_more": float((a < b).mean()),
                      "hist": np.bincount(np.minimum(t, 30), minlength=31).tolist()})
    out["stats"] = stats
    return out
