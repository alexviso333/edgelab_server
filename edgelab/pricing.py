"""Precio de TODOS los mercados de un partido.

Para cada selección que publique alguna casa calcula:
  - probabilidad del modelo (matriz de marcadores Dixon-Coles para goles; binomial
    negativa para córners, tarjetas y tiros)
  - probabilidad del consenso del mercado (cada casa sin margen, Pinnacle y Betfair pesan doble)
  - probabilidad final (mezcla de las dos), cuota justa, mejor cuota y casa, cuota de bet365,
    valor esperado, stake (¼ de Kelly) y confianza
  - una explicación en castellano de qué pasa con la apuesta según el resultado
Además detecta surebets entre casas.

Fuente de los goles esperados (λ), por orden:
  1. predicción guardada del modelo propio (ensemble walk-forward)
  2. mezcla del mercado afinado (Pinnacle/Betfair sin margen) con Dixon-Coles de los equipos
  3. lo que haya de los dos
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict

import numpy as np

from .config import settings
from .core import db
from .core.markets import devig as _devig_np
from .models.inplay import InplayParams, time_profile
from .models.poisson import implied_lambdas, score_matrix

# Peso de cada casa en la «probabilidad del mercado». Las afiladas (Pinnacle, exchanges) reaccionan antes que nadie a
# noticias y dinero informado y casi no tienen margen: son la referencia. Las blandas copian tarde; bet365, que es justo
# la casa con la que comparamos, pesa poco para no mirarnos al espejo. Con Pinnacle + Betfair presentes, la referencia
# es ~60-70 % suya aunque haya 10 casas blandas.
SHARP = {"Pinnacle": 8.0, "Betfair Exchange": 6.0, "Matchbook": 3.0, "Smarkets": 3.0, "Bet365": 0.5, "Sofascore": 0.5}


def devig(odds: list[float]) -> list[float]:
    """Quitar margen (método potencia) en Python puro: mucho más rápido que numpy para 2-3 cuotas."""
    inv = [1.0 / o for o in odds]
    if len(inv) > 12:
        return list(_devig_np(odds))
    lo, hi = 0.5, 3.0
    for _ in range(36):
        k = (lo + hi) / 2
        if sum(x ** k for x in inv) > 1:
            lo = k
        else:
            hi = k
    pr = [x ** ((lo + hi) / 2) for x in inv]
    t = sum(pr)
    return [x / t for x in pr]
EXCLUDE_BEST = {"Sofascore"}            # casa desconocida (en España Sofascore no dice cuál es)
MAIN = {"1X2", "AH", "OU", "BTTS", "DNB", "DC"}
GOAL_MARKETS = {"1X2", "DC", "DNB", "AH", "OU", "TEAM_OU", "BTTS", "CS"}
COUNT_KEYS = {"CORNERS": "corners", "CARDS": "cards", "SHOTS": "shots", "SOT": "sot"}
COUNT_WORD = {"CORNERS": "córners", "CARDS": "tarjetas", "SHOTS": "tiros", "SOT": "tiros a puerta"}

GROUPS = [
    ("resultado", "Resultado", "Quién gana: 1X2, doble oportunidad y empate no válido."),
    ("handicap", "Hándicap asiático", "Ventaja o desventaja de goles. Las líneas de cuarto (,25 / ,75) reparten la apuesta en dos."),
    ("goles", "Goles", "Total de goles, goles de cada equipo y si marcan los dos."),
    ("exacto", "Resultado exacto", "Cuotas altas, probabilidad baja: úsalo para dutching (varios marcadores a la vez)."),
    ("mitades", "Primera y segunda parte", "Los mismos mercados, pero solo con lo que pase en esa mitad."),
    ("corners", "Córners", "Total de córners y de cada equipo."),
    ("tarjetas", "Tarjetas", "Total de tarjetas (amarilla = 1, roja = 1, salvo que la casa diga otra cosa)."),
    ("tiros", "Tiros", "Tiros totales y tiros a puerta."),
    ("estadisticas", "Faltas, fueras de juego, entradas y paradas", "Estadísticas de equipo con su modelo propio."),
    ("jugadores", "Jugadores", "Goleadores, asistencias, tiros, entradas, faltas, tarjetas y paradas de cada jugador, según sus últimos partidos y los minutos que se espera que juegue."),
    ("especiales", "Especiales", "Mercados que la casa ofrece pero que no se pueden calcular bien (se muestran con sus cuotas)."),
]


def fnum(x: float) -> str:
    s = f"{x:+g}" if x else "0"
    return s.replace(".", ",")


# ---------------------------------------------------------------------------
# Goles esperados
# ---------------------------------------------------------------------------
_DC: dict = {}


def dc_model():
    """Dixon-Coles de todos los equipos con los últimos 2 años (se reajusta una vez al día)."""
    from .models.dixon_coles import DCModel, fit
    day = time.strftime("%Y-%m-%d")
    if _DC.get("day") == day:
        return _DC["m"]
    cached = db.kv_get("dc_daily")
    if cached and cached.get("day") == day:
        m = DCModel.from_dict(cached["model"])
    else:
        now = int(time.time())
        rows = db.q("SELECT home_id, away_id, kickoff, hg, ag, comp FROM matches WHERE status='finished' AND hg IS NOT NULL "
                    "AND home_id IS NOT NULL AND away_id IS NOT NULL AND kickoff>?", (now - 730 * 86400,))
        m = fit(rows, now, neutral_comps=())
        if m.attack:
            db.kv_set("dc_daily", {"day": day, "model": m.to_dict()})
    _DC.update({"day": day, "m": m})
    return m


def rebuild_odds_now(mid: int) -> None:
    con = db.get_con()
    sel = ("SELECT match_id,bookmaker,market,selection,COALESCE(ROUND(line,2),-999),COALESCE(period,'FT'),line,odds,odds,ts "
           "FROM odds WHERE match_id=? AND phase!='live' ORDER BY ts")
    con.execute("INSERT OR IGNORE INTO odds_now(match_id,bookmaker,market,selection,line_k,period,line,odds,open_odds,ts) " + sel, (mid,))
    con.execute("INSERT INTO odds_now(match_id,bookmaker,market,selection,line_k,period,line,odds,open_odds,ts) " + sel +
                " ON CONFLICT(match_id,bookmaker,market,selection,line_k,period) DO UPDATE SET odds=excluded.odds, ts=excluded.ts", (mid,))
    con.commit()


STALE_HOURS = 8.0
OLD365_MAX_H = 48.0     # una cuota vieja de bet365 se puede usar hasta este límite si cuadra con el mercado de hoy


def book_age_h(mid: int, book: str = "Bet365") -> float | None:
    """Horas desde la última actualización de esa casa en Statshub para este partido."""
    from datetime import datetime
    t = (db.kv_get(f"book_upd:{mid}") or {}).get(book)
    try:
        return (time.time() - datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()) / 3600
    except Exception:
        return None


def stale_books(mid: int) -> set:
    """DESACTIVADO. Se usaba la fecha «bookmakerUpdatedAt» de Statshub para tirar cuotas viejas, pero esa fecha no es
    fiable: bet365 salía «de hace 22 h» cuando su 1X2 había cambiado hace 1 h, y se tiraban cuotas buenas (por eso
    desaparecieron las apuestas con valor). Las cuotas desfasadas de verdad (Feyenoord 1,53 cuando el resto daba 1,28)
    las caza el aviso de «bet365 muy por encima del resto», que deja fuera la apuesta."""
    return set()


def _stale_books_unused(mid: int) -> set:
    from datetime import datetime
    upd = db.kv_get(f"book_upd:{mid}") or {}
    tss = {}
    for b, t in upd.items():
        try:
            tss[b] = datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    if not tss:
        return set()
    # vieja = se ha quedado atrás frente a la casa más reciente (no frente al reloj: de noche nadie actualiza y eso
    # no hace viejas a todas; lo que delata una cuota desfasada es que las demás se han movido y ella no)
    freshest = max(tss.values())
    return {b for b, ts in tss.items() if freshest - ts > STALE_HOURS * 3600}


def _book_odds(mid: int) -> dict:
    """Última cuota y la de apertura por (market, period, selection, line) → {casa: cuota} (tabla odds_now: rápida)."""
    rows = db.q("SELECT bookmaker, market, selection, line, period, odds, open_odds, ts FROM odds_now WHERE match_id=?", (mid,))
    if not rows and db.q1("SELECT 1 FROM odds WHERE match_id=? AND phase!='live' LIMIT 1", (mid,)):
        rebuild_odds_now(mid)
        rows = db.q("SELECT bookmaker, market, selection, line, period, odds, open_odds, ts FROM odds_now WHERE match_id=?", (mid,))
    now: dict = defaultdict(dict)
    opn: dict = defaultdict(dict)
    tsm: dict = defaultdict(dict)
    mx = None
    stale = stale_books(mid)
    old365: dict = {}
    for r in rows:
        if r["bookmaker"] in stale:
            # Statshub a veces sirve cuotas de una casa de hace un día: no entran en el consenso. La de bet365 se guarda
            # aparte por si sigue cuadrando con el mercado actual (ver price_match).
            if r["bookmaker"] == "Bet365":
                old365[(r["market"], r["period"] or "FT", r["selection"], None if r["line"] is None else round(float(r["line"]), 2))] = r["odds"]
            continue
        k = (r["market"], r["period"] or "FT", r["selection"], None if r["line"] is None else round(float(r["line"]), 2))
        now[k][r["bookmaker"]] = r["odds"]
        opn[k][r["bookmaker"]] = r["open_odds"]
        tsm[k][r["bookmaker"]] = r["ts"]
        mx = r["ts"] if mx is None or (r["ts"] or 0) > mx else mx
    # mercados que solo tiene bet365 (jugadores casi siempre): sin otra casa con la que comparar, se usa su cuota
    # aunque sea de hace unas horas, marcada para que se compruebe en bet365 (ver price_match)
    old_only = set()
    for k, o in old365.items():
        if k not in now:
            now[k]["Bet365"] = o
            old_only.add(k)
    return {"now": now, "open": opn, "tsm": tsm, "ts": mx, "old365": old365, "old_only": old_only}


def _consensus_1x2(books: dict) -> tuple[float, float, float] | None:
    acc, wsum = np.zeros(3), 0.0
    keys = [("1X2", "FT", s, None) for s in "HDA"]
    names = set.intersection(*[set(books.get(k, {})) for k in keys]) if all(k in books for k in keys) else set()
    for bk in names:
        o = [books[k][bk] for k in keys]
        if min(o) <= 1.0:
            continue
        w = SHARP.get(bk, 1.0)
        acc += w * np.array(devig(o))
        wsum += w
    return tuple(acc / wsum) if wsum else None


def _consensus_ou25(books: dict) -> float | None:
    ko, ku = ("OU", "FT", "O", 2.5), ("OU", "FT", "U", 2.5)
    if ko not in books or ku not in books:
        return None
    acc = wsum = 0.0
    for bk in set(books[ko]) & set(books[ku]):
        w = SHARP.get(bk, 1.0)
        acc += w * devig([books[ko][bk], books[ku][bk]])[0]
        wsum += w
    return acc / wsum if wsum else None


def match_lambdas(m: dict, books: dict | None = None) -> dict:
    """Goles esperados de cada equipo, ajustados por bajas, noticias y rotaciones que el modelo aún no sabía."""
    lam = _match_lambdas_base(m, books)
    if not lam or not m.get("id"):
        return lam
    try:
        from .absences import factors, team_ratios
        f = factors(m["id"], lam.get("source", ""))
        if abs(f["H"] - 1) > 0.005 or abs(f["A"] - 1) > 0.005:
            lam = {**lam, "lh": lam["lh"] * f["H"], "la": lam["la"] * f["A"], "absence": {"f": f, "teams": team_ratios(m["id"])}}
    except Exception:
        pass
    try:      # lo que se juega cada uno y el tiempo (solo la parte del modelo: las casas ya lo saben)
        from .context import context_factors
        expo = {"ensemble": 0.75, "mix": 0.35, "market": 0.0}.get(lam.get("source", ""), 1.0)
        if expo:
            cf = context_factors(m)
            fh, fa = cf["H"] ** expo, cf["A"] ** expo
            if abs(fh - 1) > 0.003 or abs(fa - 1) > 0.003:
                lam = {**lam, "lh": lam["lh"] * fh, "la": lam["la"] * fa, "context": cf}
    except Exception:
        pass
    return lam


def _match_lambdas_base(m: dict, books: dict | None = None) -> dict:
    books = books if books is not None else _book_odds(m["id"])["now"]
    pred = db.q1("SELECT payload FROM predictions WHERE match_id=? AND model='prematch'", (m["id"],))
    rho = -0.05
    dcm = None
    try:
        dcm = dc_model()
        rho = dcm.rho or rho
    except Exception:
        pass
    if pred:
        p = json.loads(pred["payload"])
        return {"lh": p["lh"], "la": p["la"], "rho": rho, "source": "ensemble",
                "label": "Modelo propio (ensemble con validación temporal)"}
    mk = None
    c = _consensus_1x2(books)
    if c:
        mk = implied_lambdas(*c, _consensus_ou25(books), rho=rho)
    dc = dcm.lambdas(m["home_id"], m["away_id"]) if dcm and m.get("home_id") else None
    if mk and dc:
        lh = math.exp(0.65 * math.log(mk[0]) + 0.35 * math.log(dc[0]))
        la = math.exp(0.65 * math.log(mk[1]) + 0.35 * math.log(dc[1]))
        return {"lh": lh, "la": la, "rho": rho, "source": "mix", "market": mk, "dc": dc,
                "label": "Mercado afinado (Pinnacle/Betfair sin margen) 65 % + fuerza de los equipos 35 %"}
    if mk:
        return {"lh": mk[0], "la": mk[1], "rho": rho, "source": "market", "market": mk,
                "label": "Mercado afinado (Pinnacle/Betfair sin margen)"}
    if dc:
        return {"lh": dc[0], "la": dc[1], "rho": rho, "source": "dc", "dc": dc,
                "label": "Fuerza de los equipos (Dixon-Coles, 2 temporadas)"}
    return {}


def half_share() -> float:
    w = time_profile(InplayParams.from_dict(db.load_params("inplay")))
    return float(w[:45].sum())


# ---------------------------------------------------------------------------
# Probabilidades vectorizadas sobre la matriz de marcadores
# ---------------------------------------------------------------------------
def _parts(line: float) -> list[float]:
    q = round(line * 4) / 4
    if abs(q * 2 - round(q * 2)) > 1e-9:          # ,25 / ,75
        return [q - 0.25, q + 0.25]
    return [q]


def _asian(margin: np.ndarray, line: float, P: np.ndarray) -> tuple[float, float, float]:
    w = p = 0.0
    parts = _parts(line)
    for L in parts:
        v = margin + L
        w += P[v > 1e-9].sum()
        p += P[np.abs(v) <= 1e-9].sum()
    w, p = w / len(parts), p / len(parts)
    return float(w), float(p), float(max(0.0, 1 - w - p))


def goal_probs(market: str, sel: str, line: float | None, P: np.ndarray, base: tuple[int, int] = (0, 0)) -> tuple[float, float, float] | None:
    """`base` = marcador desde el que cuenta el hándicap (AH/DNB en directo, estilo bet365)."""
    n = P.shape[0]
    I = np.arange(n)[:, None] + np.zeros((1, n), int)
    J = np.zeros((n, 1), int) + np.arange(n)[None, :]
    if market == "1X2":
        mask = {"H": I > J, "D": I == J, "A": I < J}.get(sel)
    elif market == "DC":
        mask = {"1X": I >= J, "X2": I <= J, "12": I != J}.get(sel)
    elif market == "BTTS":
        mask = {"Y": (I > 0) & (J > 0), "N": ~((I > 0) & (J > 0))}.get(sel)
    elif market == "CS":
        try:
            x, y = (int(v) for v in sel.split("-"))
        except Exception:
            return None
        if x >= n or y >= n:
            return None
        mask = (I == x) & (J == y)
    elif market in ("AH", "DNB"):
        if sel not in ("H", "A"):
            return None
        d = (I - base[0]) - (J - base[1])
        return _asian(d if sel == "H" else -d, 0.0 if market == "DNB" else float(line or 0), P)
    elif market == "OU":
        if line is None or sel not in ("O", "U"):
            return None
        tot = I + J
        return _asian(tot.astype(float) if sel == "O" else -tot.astype(float), -line if sel == "O" else line, P)
    elif market == "TEAM_OU":
        if line is None or len(sel) != 2:
            return None
        g = (I if sel[0] == "H" else J).astype(float)
        return _asian(g if sel[1] == "O" else -g, -line if sel[1] == "O" else line, P)
    else:
        return None
    if mask is None:
        return None
    w = float(P[mask].sum())
    return w, 0.0, 1 - w


# ---------------------------------------------------------------------------
# Córners, tarjetas, tiros
# ---------------------------------------------------------------------------
def count_models(m: dict) -> dict:
    from .models.secondary import CountForecast, referee_factor, shrink, stat_value
    out = {}
    if not m.get("home_id") or not m.get("away_id"):
        return out
    since = m["kickoff"] - 400 * 86400

    def team_rows(tid):
        return db.q("SELECT mt.home_id, mt.away_id, ms.side, ms.stats FROM matches mt JOIN match_stats ms ON ms.match_id=mt.id "
                    "WHERE (mt.home_id=? OR mt.away_id=?) AND mt.kickoff BETWEEN ? AND ? AND mt.status='finished' "
                    "ORDER BY mt.kickoff DESC LIMIT 60", (tid, tid, since, m["kickoff"]))
    rows_h, rows_a = team_rows(m["home_id"]), team_rows(m["away_id"])
    extra = {"FOULS": "fouls", "OFFSIDES": "offsides", "TACKLES": "totalTackle", "SAVES": "goalkeeperSaves"}
    def getv(st, code, key):
        if code in COUNT_KEYS:
            return stat_value(st, key)
        v = st.get(key)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None
    parsed_h = [(r, json.loads(r["stats"])) for r in rows_h]
    parsed_a = [(r, json.loads(r["stats"])) for r in rows_a]
    proc = process_factors(m, parsed_h, parsed_a)          # C1: toques en el área (cómo llega cada equipo últimamente)
    wx = (db.kv_get(f"weather3:{m['id']}") or {}).get("w") or {}     # C4: viento y lluvia
    for code, key in list(COUNT_KEYS.items()) + list(extra.items()):
        def agg(rows, tid):
            f, a = [], []
            for r, st in rows:
                v = getv(st, code, key)
                if v is None:
                    continue
                mine = (r["side"] == "H") == (r["home_id"] == tid)
                (f if mine else a).append(v)
            return f[:20], a[:20]
        fh, ah = agg(parsed_h, m["home_id"])
        fa, aa = agg(parsed_a, m["away_id"])
        if len(fh) < 4 or len(fa) < 4:
            continue
        lg = (np.mean(fh + ah + fa + aa)) if (fh + ah + fa + aa) else None
        if not lg:
            continue
        mh = shrink(np.mean(fh), len(fh), lg) * shrink(np.mean(aa), len(aa), lg) / lg * 1.05
        ma = shrink(np.mean(fa), len(fa), lg) * shrink(np.mean(ah), len(ah), lg) / lg * 0.95
        mult = 1.0
        if code in ("CARDS", "FOULS") and m.get("referee_id"):
            # C2: el árbitro también en las faltas (unos pitan mucho más que otros)
            ref = db.q("SELECT ms.stats FROM matches mt JOIN match_stats ms ON ms.match_id=mt.id WHERE mt.referee_id=? "
                       "AND mt.status='finished' ORDER BY mt.kickoff DESC LIMIT 60", (m["referee_id"],))
            vals = [stat_value(json.loads(r["stats"]), "cards" if code == "CARDS" else "fouls") for r in ref]
            vals = [v for v in vals if v is not None]
            if vals:
                mult = referee_factor(np.mean(vals) * 2, len(vals) // 2, lg * 2)
                if code == "FOULS":
                    mult = 1 + (mult - 1) * 0.6        # en faltas el árbitro pesa algo menos que en tarjetas
        if code in ("CORNERS", "SHOTS", "SOT") and proc:
            mh, ma = mh * proc.get("H", 1.0), ma * proc.get("A", 1.0)
        if wx:
            w_, r_ = float(wx.get("wind") or 0), float(wx.get("rain") or 0)
            if code == "CORNERS" and w_ >= 30:
                mult *= 1.04                       # viento fuerte: más balones despejados a córner
            if code in ("FOULS", "CARDS") and r_ >= 3:
                mult *= 1.03                       # campo mojado: más entradas a destiempo
        disp = {"CARDS": 12.0, "OFFSIDES": 8.0, "SAVES": 15.0}.get(code, 20.0)
        from .postmatch import bias_factor, entity_factor
        bf = bias_factor("team", code)        # lo aprendido comparando lo esperado con lo que pasó en cada partido
        # y lo propio de cada equipo (no todos por igual)
        mh = mh * bf * entity_factor(f"tm:{m['home_id']}:{code}") * entity_factor(f"op:{m['away_id']}:{code}")
        ma = ma * bf * entity_factor(f"tm:{m['away_id']}:{code}") * entity_factor(f"op:{m['home_id']}:{code}")
        out[code] = {"model": CountForecast(mh * mult, ma * mult, disp),
                     "mu": [round(mh * mult, 2), round(ma * mult, 2)], "n": [len(fh), len(fa)], "ref": mult,
                     "proc": proc if code in ("CORNERS", "SHOTS", "SOT") else {}}
    return out


def process_factors(m: dict, parsed_h: list, parsed_a: list) -> dict:
    """C1: cómo llega cada equipo últimamente. Toques en el área rival (lo que más anticipa córners y tiros) de sus
    últimos 6 partidos frente a los últimos 20, y lo mismo de lo que concede el rival. Si un equipo ha empezado a pisar
    mucho más el área (cambio de entrenador, de sistema, de forma), sus córners y tiros lo notarán antes que su media."""
    def series(rows, tid, own):
        out = []
        for r, st in rows:
            v = st.get("touchesInOppBox")
            if v is None:
                continue
            mine = (r["side"] == "H") == (r["home_id"] == tid)
            if mine == own:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    pass
        return out
    def ratio(xs):
        if len(xs) < 8:
            return 1.0
        rec, lon = np.mean(xs[:6]), np.mean(xs[:20])
        return (rec / lon) ** 0.5 if lon > 0 else 1.0
    if not m.get("home_id"):
        return {}
    fh = ratio(series(parsed_h, m["home_id"], True)) * ratio(series(parsed_a, m["away_id"], False)) ** 0.5
    fa = ratio(series(parsed_a, m["away_id"], True)) * ratio(series(parsed_h, m["home_id"], False)) ** 0.5
    return {"H": float(min(1.15, max(0.85, fh))), "A": float(min(1.15, max(0.85, fa)))}


def count_probs(market: str, sel: str, line: float | None, cm: dict, period: str = "FT") -> tuple[float, float, float] | None:
    from .models.secondary import CountForecast
    code, kind = market.split("_")
    if code not in cm or line is None:
        return None
    f = cm[code]["model"]
    if period == "HT":
        sh = {"CORNERS": 0.46, "CARDS": 0.38}.get(code, 0.47)
        f = CountForecast(f.mu_h * sh, f.mu_a * sh, f.disp)
    elif period == "2H":
        return None
    if kind == "OU":
        side = sel[0] if len(sel) == 2 else None
        s = sel[-1]
        w, p, l = f.p_over(line, side)
        return (w, p, l) if s == "O" else (l, p, w)
    if kind == "AH" and sel in ("H", "A"):
        return f.p_handicap(sel, line)
    return None


# ---------------------------------------------------------------------------
# Etiquetas y explicaciones
# ---------------------------------------------------------------------------
def group_of(market: str, period: str) -> str:
    if period != "FT" and market in GOAL_MARKETS:
        return "mitades"
    if market in ("1X2", "DC", "DNB"):
        return "resultado"
    if market == "AH":
        return "handicap"
    if market in ("OU", "TEAM_OU", "BTTS"):
        return "goles"
    if market == "CS":
        return "exacto"
    return {"CORNERS": "corners", "CARDS": "tarjetas", "SHOTS": "tiros", "SOT": "tiros"}.get(market.split("_")[0], "otros")


def label(market: str, sel: str, line, period: str, home: str, away: str) -> str:
    team = {"H": home, "A": away}
    per = {"HT": " · 1ª parte", "2H": " · 2ª parte"}.get(period, "")
    L = None if line is None else float(line)
    if market == "1X2":
        t = {"H": f"Gana {home}", "D": "Empate", "A": f"Gana {away}"}.get(sel, sel)
    elif market == "DC":
        t = {"1X": f"{home} o empate", "X2": f"Empate o {away}", "12": "Gana cualquiera (no empate)"}.get(sel, sel)
    elif market == "DNB":
        t = f"{team.get(sel, sel)} · empate devuelve"
    elif market == "AH":
        t = f"{team.get(sel, sel)} {fnum(L)}"
    elif market == "OU":
        t = f"{'Más' if sel == 'O' else 'Menos'} de {fnum(abs(L)).lstrip('+')} goles"
    elif market == "TEAM_OU":
        t = f"{team.get(sel[0], sel[0])}: {'más' if sel[1] == 'O' else 'menos'} de {fnum(abs(L)).lstrip('+')} goles"
    elif market == "BTTS":
        t = "Marcan los dos" if sel == "Y" else "No marcan los dos"
    elif market == "CS":
        t = f"Resultado exacto {sel}"
    else:
        code, kind = market.split("_")
        word = COUNT_WORD.get(code, code.lower())
        if kind == "OU":
            who = f"{team[sel[0]]}: " if len(sel) == 2 else ""
            t = f"{who}{'más' if sel[-1] == 'O' else 'menos'} de {fnum(abs(L)).lstrip('+')} {word}"
            t = t[0].upper() + t[1:]
        else:
            t = f"{team.get(sel, sel)} {fnum(L)} en {word}"
    return t + per


def _outcome_txt(w: float, pu: float) -> str:
    if w == 1:
        return "cobras todo"
    if pu == 1:
        return "te devuelven el dinero"
    if w == 0.5 and pu == 0.5:
        return "cobras media apuesta y te devuelven la otra media"
    if pu == 0.5:
        return "pierdes media apuesta y te devuelven la otra media"
    if w == 0.5:
        return "cobras media y pierdes media"
    return "pierdes"


def _merge(items: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    out: list[list] = []
    for k, t in items:
        if out and out[-1][2] == t:
            out[-1][1] = k
        else:
            out.append([k, k, t])
    return [tuple(x) for x in out]


def explain(market: str, sel: str, line, home: str, away: str) -> str:
    """Qué pasa con la apuesta según el resultado (solo en líneas asiáticas y totales con cuartos/enteros)."""
    is_ah = market in ("AH", "DNB") or market.endswith("_AH")
    is_tot = market in ("OU", "TEAM_OU") or market.endswith("_OU")
    if not (is_ah or is_tot):
        return ""
    L = 0.0 if market == "DNB" else float(line or 0)
    parts = _parts(abs(L) if is_tot else L)
    if is_tot and len(parts) == 1 and abs(abs(L) - round(abs(L))) > 1e-9:
        return ""
    def res(vals):
        w = sum(1 for r in vals if r > 1e-9) / len(parts)
        pu = sum(1 for r in vals if abs(r) <= 1e-9) / len(parts)
        return _outcome_txt(w, pu)
    if is_ah:
        name = home if sel == "H" else away
        unit = "" if market in ("AH", "DNB") else " en " + COUNT_WORD.get(market.split("_")[0], "")
        lo, hi = -3, 3
        segs = _merge([(d, res([d + p for p in parts])) for d in range(lo, hi + 1)])
        def cond(a, b):
            if a == lo and b == hi:
                return "pase lo que pase"
            if a == lo:
                return {-1: f"si {name} pierde", 0: f"si {name} empata o pierde"}.get(b, f"si {name} pierde por {-b} o más" if b < 0 else f"si {name} no gana por más de {b}")
            if b == hi:
                return {1: f"si {name} gana", 0: f"si {name} gana o empata"}.get(a, f"si {name} gana por {a} o más" if a > 0 else f"si {name} no pierde por más de {-a}")
            one = lambda d: f"{name} gana por {d}" if d > 0 else "empatan" if d == 0 else f"{name} pierde por {-d}"
            return "si " + one(a) if a == b else f"si {one(a)} o {one(b)}"
        return " · ".join(f"{cond(a, b)}{unit}: {t}" for a, b, t in segs)
    over = sel[-1] == "O"
    Lb = abs(L)
    hi = int(math.ceil(Lb)) + 1
    segs = _merge([(k, res([(k - p) if over else (p - k) for p in parts])) for k in range(0, hi + 1)])
    word = "goles" if market in ("OU", "TEAM_OU") else COUNT_WORD.get(market.split("_")[0], "")
    def cond(a, b):
        if a == 0 and b == hi:
            return "siempre"
        if a == 0:
            return f"con {b} {word} o menos"
        if b == hi:
            return f"con {a} {word} o más"
        return f"con {a} {word}" if a == b else f"con {a} a {b} {word}"
    return " · ".join(f"{cond(a, b)}: {t}" for a, b, t in segs)


# ---------------------------------------------------------------------------
# Todo junto
# ---------------------------------------------------------------------------
def _complement(market: str, sel: str, line):
    if market in ("OU", "CORNERS_OU", "CARDS_OU", "SHOTS_OU", "SOT_OU", "TEAM_OU"):
        return [(sel[:-1] + ("U" if sel[-1] == "O" else "O"), line)]
    if market in ("BTTS",):
        return [("N" if sel == "Y" else "Y", line)]
    if market in ("AH", "CORNERS_AH", "CARDS_AH"):
        return [("A" if sel == "H" else "H", None if line is None else round(-float(line), 2))]
    if market == "DNB":
        return [("A" if sel == "H" else "H", line)]
    if market == "1X2":
        return [(s, None) for s in "HDA" if s != sel]
    return None


_PM_CACHE: dict = {}


def price_match(mid: int) -> dict:
    """Con caché en memoria mientras no cambien las cuotas ni la predicción."""
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {}
    ts = db.q1("SELECT MAX(ts) AS t FROM odds_now WHERE match_id=?", (mid,))["t"]
    pc = db.q1("SELECT created FROM predictions WHERE match_id=? AND model='prematch'", (mid,))
    key = (mid, ts, (pc or {}).get("created"), m.get("lineup_status"), time.strftime("%Y-%m-%d"), db.kv_get(f"abs_sig:{mid}"))
    hit = _PM_CACHE.get(mid)
    if hit and hit[0] == key:
        return hit[1]
    out = _price_match(m)
    if len(_PM_CACHE) > 150:
        _PM_CACHE.pop(next(iter(_PM_CACHE)))
    _PM_CACHE[mid] = (key, out)
    return out


def _price_match(m: dict) -> dict:
    mid = m["id"]
    bo = _book_odds(mid)
    books, opens = bo["now"], bo["open"]
    old365 = bo.get("old365") or {}
    old_only = bo.get("old_only") or set()
    age365 = book_age_h(mid) if old365 else None
    lineups_soon = m["kickoff"] - time.time() < 3 * 3600      # cerca del saque las cuotas viejas ya no valen (once, bajas)
    lam = match_lambdas(m, books)
    mats = {}
    if lam:
        s = half_share()
        mats["FT"] = score_matrix(lam["lh"], lam["la"], lam["rho"])
        mats["HT"] = score_matrix(lam["lh"] * s, lam["la"] * s, lam["rho"])
        mats["2H"] = score_matrix(lam["lh"] * (1 - s), lam["la"] * (1 - s), 0.0)
    cm = count_models(m)
    bank = settings.bankroll
    rows = []
    from .markets_all import Ctx, price_raw
    ctx = Ctx(m, lam, mats, cm, half_share() if lam else 0.45)
    # mercados raros con todas sus selecciones: consenso por "conjunto completo"
    raw_sets: dict = defaultdict(list)
    for k in books:
        if k[0].startswith("RAW:"):
            raw_sets[(k[0], k[1])].append(k)
    stale_set = stale_books(m["id"])
    for (mk, per, sel, line), bk in books.items():
        raw = mk.startswith("RAW:")
        rinfo = None
        cq = None
        nb = 0
        if raw:
            rinfo = price_raw(mk[4:], per, sel, line, ctx)
            swap = None
            if re.search(r"\bOver\b", sel):
                swap = sel.replace("Over", "Under")
            elif re.search(r"\bUnder\b", sel):
                swap = sel.replace("Under", "Over")
            elif sel in ("Yes", "No"):
                swap = "No" if sel == "Yes" else "Yes"
            if swap:
                k2 = (mk, per, swap, line)
                acc = wsum = 0.0
                for b, o in bk.items():
                    o2 = books.get(k2, {}).get(b)
                    if o2 and o2 > 1 and o > 1:
                        wgt = SHARP.get(b, 1.0)
                        acc += wgt * devig([o, o2])[0]; wsum += wgt; nb += 1
                cq = acc / wsum if wsum else None
            elif rinfo.get("group") != "jugadores" and not rinfo.get("nocons"):
                group = raw_sets[(mk, per)]
                if 2 <= len(group) <= 40:
                    acc = wsum = 0.0
                    for b, o in bk.items():
                        os_ = [books[g].get(b) for g in group]
                        if any(x is None or x <= 1 for x in os_):
                            continue
                        ovr = sum(1 / x for x in os_)
                        if not (1.0 <= ovr <= 1.45):
                            continue
                        pr = devig(os_)[group.index((mk, per, sel, line))]
                        wgt = SHARP.get(b, 1.0)
                        acc += wgt * pr; wsum += wgt; nb += 1
                    cq = acc / wsum if wsum else None
            if cq is None and rinfo.get("group") == "jugadores":
                # una sola cara (no hay "menos de"): cada casa sin un margen típico del 8 %, sin contar bet365
                ps_ = [(SHARP.get(b, 1.0), 1 / o / 1.08) for b, o in bk.items() if b not in ("Bet365", "Sofascore") and o > 1]
                if ps_:
                    cq = sum(w_ * p_ for w_, p_ in ps_) / sum(w_ for w_, _ in ps_)
                    nb = len(ps_)
            comp = None
        else:
            comp = _complement(mk, sel, line)
        if comp is not None:
            acc = wsum = 0.0
            ks = [(mk, per, s2, l2) for s2, l2 in comp]
            for b, o in bk.items():
                others = [books.get(k, {}).get(b) for k in ks]
                if any(x is None or x <= 1 for x in others) or o <= 1:
                    continue
                pr = devig([o] + others)[0]
                w = SHARP.get(b, 1.0)
                acc += w * pr
                wsum += w
                nb += 1
            cq = acc / wsum if wsum else None
        # modelo
        mp = None
        if raw:
            mp = rinfo.get("probs")
        elif mk in GOAL_MARKETS and per in mats:
            mp = goal_probs(mk, sel, line, mats[per])
        elif mk.split("_")[0] in COUNT_KEYS and per in ("FT", "HT"):
            mp = count_probs(mk, sel, line, cm, per)
        mq = None
        push = 0.0
        if mp:
            w_, p_, l_ = mp
            push = p_
            mq = w_ / max(w_ + l_, 1e-12)
        cands = {b: o for b, o in bk.items() if b not in EXCLUDE_BEST and o > 1}
        best_b = max(cands, key=cands.get) if cands else None
        if mq is None and cq is None:
            if raw:      # sin modelo ni consenso: se enseña igual con sus cuotas
                rows.append({"market": mk, "period": per, "selection": sel, "line": line, "group": rinfo["group"], "label": rinfo["label"],
                             "explain": "", "p": None, "push": 0, "p_model": None, "p_market": None, "fair": None,
                             "b365": bk.get("Bet365"), "b365_open": opens.get((mk, per, sel, line), {}).get("Bet365"),
                             "best": cands.get(best_b) if best_b else None, "best_book": best_b, "ev": None, "ev_best": None, "stake": 0,
                             "conf": "sin modelo", "n_books": len(bk), "warn": None, "player": rinfo.get("player"), "team": rinfo.get("team"), "meta": rinfo.get("meta"),
                             "books": {b: o for b, o in sorted(bk.items(), key=lambda x: -x[1])}})
            continue
        if mq is not None and cq is not None:
            wm = 0.35 if (mk.split("_")[0] in COUNT_KEYS or (raw and rinfo.get("group") in ("jugadores", "estadisticas", "corners", "tarjetas"))) else 0.5
            q = wm * mq + (1 - wm) * cq
        else:
            q = mq if mq is not None else cq
        q = min(max(q, 1e-6), 1 - 1e-6)
        w = q * (1 - push)
        l = (1 - q) * (1 - push)
        fair = 1 + l / w if w > 0 else None
        best = cands.get(best_b) if best_b else None
        b365 = bk.get("Bet365")
        b365_src = "bet365"
        if (mk, per, sel, line) in old_only:
            if age365 is not None and (age365 > OLD365_MAX_H or lineups_soon):
                b365 = None                                  # demasiado vieja o ya cerca del saque: no se usa
            else:
                b365_src = f"bet365 (hace {int(age365 or 0)} h)"
        if b365 is None and "Bet365" in stale_set and bk.get("Sofascore"):
            b365, b365_src = bk["Sofascore"], "bet365 (Sofascore)"     # Sofascore muestra las cuotas de bet365 y van al día
        if b365 is None and old365.get((mk, per, sel, line)) and age365 is not None and age365 <= OLD365_MAX_H and not lineups_soon:
            # cuota de bet365 de hace unas horas: solo se usa si sigue cuadrando con lo que pagan hoy las demás casas
            # (si el mercado se ha movido, como Feyenoord 1,53 → 1,28, no cuadra y se descarta)
            o_old = old365[(mk, per, sel, line)]
            fresh_o = sorted(o for b, o in bk.items() if b not in ("Sofascore", "Betfair Exchange", "Matchbook", "Smarkets") and o > 1)
            if fresh_o:
                med_o = fresh_o[len(fresh_o) // 2]
                if o_old <= med_o * (1.06 if len(fresh_o) >= 2 else 1.03):
                    b365, b365_src = o_old, f"bet365 (hace {int(age365)} h)"
        if b365 is None and "Bet365" in stale_set:
            # bet365 sin actualizar en Statshub: se usa la cuota mediana de las casas al día (sin exchanges) como referencia
            fresh = sorted(o for b, o in bk.items() if b not in ("Sofascore", "Betfair Exchange", "Matchbook", "Smarkets") and o > 1)
            if len(fresh) >= 3:
                b365, b365_src = fresh[len(fresh) // 2], "mercado"
        ev365 = w * (b365 - 1) - l if b365 else None
        evb = w * (best - 1) - l if best else None
        ref = b365 or best
        kelly = 0.0
        if ref and ref > 1:
            f = (w * (ref - 1) - l) / (ref - 1)
            risky = raw and rinfo.get("group") in ("jugadores", "especiales")
            # jugadores: precio menos fiable → 1/8 de Kelly y tope del 1 % de la banca
            kelly = max(0.0, min(0.01, 0.125 * f)) if risky else max(0.0, min(0.03, 0.25 * f))
        disagree = abs(mq - cq) if (mq is not None and cq is not None) else None
        conf = "baja"
        if raw and rinfo.get("group") == "jugadores":
            # jugador: solo se fía si hay al menos 2 casas más y el modelo no se aleja de ellas
            conf = "media" if (cq is not None and nb >= 2 and mq is not None and disagree < 0.08 and rinfo.get("conf") == "media") else "baja"
            # solo bet365 la ofrece (lo normal en jugadores): sin otra casa con la que contrastar, se fía del modelo solo en
            # la zona razonable (30–85 %, cuota ≤ 3,5) y con un modelo de jugador sólido; el umbral de valor sube aparte
            # solo en estadísticas que se repiten mucho (tiros, faltas, entradas…): en goles/asistencias el modelo de
            # jugador exagera con las estrellas (Salah «marca o asiste» al 77 % cuando bet365 dice 62 %)
            if conf == "baja" and set(bk) <= {"Bet365", "Sofascore"} and rinfo.get("conf") == "media" and mq is not None \
                    and (rinfo.get("meta") or {}).get("kind") in ("shots", "sot", "fouls", "fouled", "tackles") \
                    and 0.30 <= mq <= 0.85 and b365 and b365 <= 3.5 and not lineups_soon:
                conf = "media"
        elif raw:
            if cq is not None and nb >= 3 and (disagree is None or disagree < 0.06):
                conf = "media"
            elif rinfo.get("conf"):
                conf = rinfo["conf"]
            elif mq is not None and rinfo.get("group") in ("goles", "resultado", "mitades"):
                conf = "media"
            if disagree is not None and disagree > 0.09:
                conf = "baja"
        elif nb >= 4 and (disagree is None or disagree < 0.05):
            conf = "alta" if mk in MAIN or mk.endswith("_OU") else "media"
        elif nb >= 2 or (mq is not None and mk in MAIN):
            conf = "media"
        if not raw and disagree is not None and disagree > 0.09:
            conf = "baja"
        warn = None
        if ev365 is not None and ev365 > 0.25:
            warn = "Valor tan alto suele ser una cuota desfasada o un error de la casa: compruébala antes."
        if b365:
            others = sorted(o for b, o in bk.items() if b not in ("Bet365", "Sofascore") and o > 1)
            if len(others) >= 2:
                med = others[len(others) // 2] if len(others) % 2 else (others[len(others) // 2 - 1] + others[len(others) // 2]) / 2
                tol = 1.20 if (raw and rinfo.get("group") == "jugadores") else 1.12
                # solo es sospechosa si bet365 se va muy por encima de TODAS (también de la mejor de las demás): que
                # pague más que la mediana es justo donde está el valor (bet365 suele ir alta en jugadores)
                top = others[-1]
                if b365 > med * tol and b365 > top * 1.08:
                    # bet365 muy por encima del resto: casi siempre es otra línea, otro reglamento o una cuota sin actualizar
                    warn = (f"bet365 paga {b365:.2f} y las demás casas {med:.2f}: suele ser una cuota sin actualizar u otra línea. "
                            "Compruébala en bet365 antes; si ya no está, no la hagas.")
        rows.append({
            "market": mk, "period": per, "selection": sel, "line": line,
            "group": rinfo["group"] if raw else group_of(mk, per),
            "label": rinfo["label"] if raw else label(mk, sel, line, per, m["home_name"], m["away_name"]),
            "explain": "" if raw else explain(mk, sel, line, m["home_name"], m["away_name"]),
            "player": rinfo.get("player") if raw else None, "team": rinfo.get("team") if raw else None,
            "meta": rinfo.get("meta") if raw else None,
            "exp_min": rinfo.get("exp_min") if raw else None,
            "p": round(q * (1 - push), 4), "push": round(push, 4), "p_model": None if mq is None else round(mq, 4),
            "p_market": None if cq is None else round(cq, 4), "fair": round(fair, 3) if fair else None,
            "b365": b365, "b365_src": b365_src, "b365_open": opens.get((mk, per, sel, line), {}).get("Bet365"),
            "best": best, "best_book": best_b, "ev": None if ev365 is None else round(ev365, 4),
            "ev_best": None if evb is None else round(evb, 4),
            "stake": round(kelly * bank, 2), "kf": round(kelly, 5), "conf": conf, "n_books": len(bk), "warn": warn,
            "books": {b: o for b, o in sorted(bk.items(), key=lambda x: -x[1])},
        })
    gk = [g[0] for g in GROUPS]
    mo = ["1X2", "DC", "DNB", "AH", "OU", "TEAM_OU", "BTTS", "CS", "CORNERS_OU", "CORNERS_AH", "CARDS_OU", "CARDS_AH", "SHOTS_OU", "SOT_OU"]
    so = {"H": 0, "D": 1, "A": 2, "1X": 0, "X2": 1, "12": 2, "O": 0, "U": 1, "Y": 0, "N": 1, "HO": 0, "HU": 1, "AO": 2, "AU": 3}
    po = {"FT": 0, "HT": 1, "2H": 2}
    def cs_key(sel):
        try:
            x, y = (int(v) for v in sel.split("-"))
            return x * 20 + y
        except Exception:
            return 0
    rows.sort(key=lambda r: (gk.index(r["group"]) if r["group"] in gk else 99, po.get(r["period"], 3),
                             mo.index(r["market"]) if r["market"] in mo else 99,
                             (len(r["selection"]) == 2 and r["market"] != "DC", abs(r["line"]) if r["line"] is not None else 0)
                             if r["market"] != "AH" else (r["selection"] == "A", r["line"] or 0),
                             cs_key(r["selection"]) if r["market"] == "CS" else so.get(r["selection"], 9)))
    summ = None
    if "FT" in mats:
        P = mats["FT"]
        n = P.shape[0]
        tot = np.add.outer(np.arange(n), np.arange(n))
        sc = sorted(((float(P[i, j]), f"{i}-{j}") for i in range(6) for j in range(6)), reverse=True)[:5]
        summ = {"scores": [[k, round(v, 4)] for v, k in sc], "p_o25": round(float(P[tot > 2.5].sum()), 4),
                "p_btts": round(float(P[1:, 1:].sum()), 4), "xg": round(lam["lh"] + lam["la"], 2),
                "p_1x2": [round(float(np.tril(P, -1).sum()), 4), round(float(np.trace(P)), 4), round(float(np.triu(P, 1).sum()), 4)],
                "counts": {k: v["mu"] for k, v in cm.items()}}
    return {"match": m, "lambdas": lam, "rows": rows, "summary": summ, "surebets": surebets(books, m, bo["tsm"]), "counts": {k: {"mu": v["mu"], "n": v["n"]} for k, v in cm.items()},
            "odds_ts": bo["ts"], "groups": [{"key": k, "name": n, "help": h} for k, n, h in GROUPS]}


# casas conocidas y con licencia en España (o exchange); la que no tengas, no sirve para cubrir
SURE_BOOKS = {"Bet365", "Betfair Exchange", "Betfair", "William Hill", "Bwin", "Codere", "Sportium", "Betway", "888sport",
              "Unibet", "Marathonbet", "Pinnacle", "Betsson", "Leovegas", "Winamax", "Paf", "Luckia", "Retabet", "Kirolbet"}
EXCHANGES = {"Betfair Exchange", "Matchbook", "Smarkets"}
SURE_MARKETS = {"1X2", "AH", "OU", "BTTS", "DNB", "TEAM_OU"}   # en córners/tarjetas cada casa cuenta distinto: no son surebets reales


def surebets(books: dict, m: dict, tsm: dict | None = None) -> list[dict]:
    """Mercados donde, cogiendo la mejor cuota de cada lado en casas distintas, se gana pase lo que pase.
    Solo mercados de goles (mismas reglas en todas las casas) y cuotas de las últimas 8 horas."""
    out, seen = [], set()
    fresh = time.time() - 8 * 3600
    for (mk, per, sel, line) in list(books):
        if mk not in SURE_MARKETS:
            continue
        comp = _complement(mk, sel, line)
        if comp is None:
            continue
        legs = [(mk, per, sel, line)] + [(mk, per, s2, l2) for s2, l2 in comp]
        key = tuple(sorted(str(x) for x in legs))
        if key in seen or any(k not in books for k in legs):
            continue
        seen.add(key)
        best = []
        for k in legs:
            c = {b: (1 + (o - 1) * 0.95 if b in EXCHANGES else o) for b, o in books[k].items() if b in SURE_BOOKS and o > 1
                 and (tsm is None or (tsm.get(k, {}).get(b) or 0) >= fresh)}
            if not c:
                break
            b = max(c, key=c.get)
            best.append((k, b, c[b]))
        if len(best) != len(legs) or not any(b == "Bet365" for _, b, _ in best):
            continue
        inv = sum(1 / o for _, _, o in best)
        if inv < 0.99:      # al menos un 1 % neto (comisión del exchange ya descontada)
            total = 100.0
            out.append({"market": mk, "period": per, "profit_pct": round(1 / inv - 1, 4),
                        "legs": [{"label": label(k[0], k[2], k[3], k[1], m["home_name"], m["away_name"]), "book": b, "odds": round(o, 3),
                                  "stake": round(total / o / inv, 2)} for k, b, o in best]})
    out.sort(key=lambda s: -s["profit_pct"])
    return out[:20]
