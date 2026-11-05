"""Proyecciones en directo: lo que va a pasar en lo que queda de partido, con base estadística.

Para cada partido en juego, cada ~60 s:
  · equipo y total: tiros, tiros a puerta, córners, faltas y tarjetas. La tasa que se proyecta mezcla
    lo esperado antes del partido (como si fueran 45' ya jugados) con lo que está pasando, y se corrige
    por el estado del partido (quien pierde ataca más, quien gana menos; las rojas; las tarjetas suben
    en la segunda parte).
  · jugadores (estadísticas en vivo de Sofascore): tiros, tiros a puerta, faltas cometidas y recibidas,
    entradas, amarilla y gol. Su tasa de antes del partido (por 90') se actualiza con lo que lleva hoy y
    con los minutos que le quedan (si es titular, hasta su minuto habitual de cambio).
  · con eso, la probabilidad de cada línea y su cuota justa. Si Sofascore publica cuota en directo de ese
    mercado (goles, córners, tarjetas, hándicap, 1X2) se compara; si no, se da la cuota mínima para bet365.
"""
from __future__ import annotations

import math
import time

from ..core import db

# cuántos minutos "vale" lo que esperábamos antes del partido frente a lo que se ve hoy:
# en lo que pasa a menudo (tiros, faltas) lo de hoy pesa pronto; en lo raro (tarjetas) hace falta más partido
K_TEAM = {"shots": 90.0, "sot": 110.0, "corners": 150.0, "fouls": 90.0, "yellow": 160.0}
W_MODEL = 0.4       # en directo el mercado manda: nuestra probabilidad final = 40 % modelo + 60 % mercado sin margen
K_PLAYER = 300.0    # un jugador hace pocas cosas por partido: su media de 15 partidos manda hasta bien entrado el partido
MIN_SIGNAL_MINUTE = 20
MIN_ODDS = 1.45
TEAM_STATS = {"shots": ("SHOTS", "tiros"), "sot": ("SOT", "tiros a puerta"), "corners": ("CORNERS", "córners"),
              "fouls": ("FOULS", "faltas"), "yellow": ("CARDS", "tarjetas")}
PLAYER_STATS = {"shots": "tiros", "sot": "tiros a puerta", "fouls": "faltas cometidas", "was_fouled": "faltas recibidas",
                "tackles": "entradas"}


def pois_ge(lam: float, k: int) -> float:
    """P(X >= k) con X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    s, term = 0.0, math.exp(-lam)
    for i in range(k):
        s += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - s)


def nb_ge(lam: float, k: int, r: float) -> float:
    """P(X >= k) con X ~ binomial negativa de media lam (más dispersión que Poisson: córners y tarjetas van a rachas)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    p = r / (r + lam)
    term = p ** r
    s = 0.0
    for i in range(k):
        s += term
        term *= (i + r) / (i + 1) * (1 - p)
    return max(0.0, 1.0 - s)


DISP = {"corners": 9.0, "yellow": 7.0, "fouls": 25.0, "shots": 15.0, "sot": 12.0}


def market_anchor(match_id: int) -> dict:
    """Total esperado de cada estadística según las casas antes del partido (sin margen): el punto de partida en directo."""
    out = {}
    for code, stat in (("CORNERS", "corners"), ("CARDS", "yellow"), ("SHOTS", "shots"), ("SOT", "sot"), ("FOULS", "fouls")):
        rows = db.q("SELECT bookmaker, selection, line, odds FROM odds_now WHERE match_id=? AND market=? AND period='FT' AND selection IN ('O','U')",
                    (match_id, f"{code}_OU"))
        by: dict = {}
        for r in rows:
            by.setdefault((r["line"], r["bookmaker"]), {})[r["selection"]] = r["odds"]
        best = None
        for (ln, bk), d in by.items():
            if "O" in d and "U" in d and ln is not None:
                po = (1 / d["O"]) / (1 / d["O"] + 1 / d["U"])
                if best is None or abs(po - 0.5) < abs(best[1] - 0.5):
                    best = (ln, po)
        if not best:
            continue
        ln, po = best
        lo, hi = 0.1, 80.0
        for _ in range(40):           # media tal que P(total > línea) = lo que dicen las casas
            mid = (lo + hi) / 2
            if nb_ge(mid, int(math.floor(ln)) + 1, DISP.get(stat, 10.0)) < po:
                lo = mid
            else:
                hi = mid
        out[stat] = (lo + hi) / 2
    return out


def remaining(minute: float, added: float | None) -> float:
    end = 90 + (added if added is not None else (4.0 if minute >= 45 else 5.0))
    return max(0.0, end - minute)


def state_factor(stat: str, side: str, score: tuple[int, int], reds: tuple[int, int], minute: float) -> float:
    """Cómo cambia el ritmo de una estadística por el marcador y las rojas."""
    me, op = (score if side == "H" else score[::-1])
    rme, rop = (reds if side == "H" else reds[::-1])
    f = 1.0
    if stat in ("shots", "sot", "corners"):
        if me < op:
            f *= 1.08 if minute < 60 else 1.18          # va perdiendo: empuja
        elif me > op:
            f *= 0.94 if minute < 60 else 0.86          # va ganando: se repliega
        f *= 0.75 ** rme * 1.12 ** rop
    elif stat in ("fouls", "yellow"):
        if me > op:
            f *= 1.10                                     # protege el resultado: más faltas
        f *= 0.9 ** rme
        if stat == "yellow" and minute >= 45:
            f *= 1.25                                     # las tarjetas se concentran en la 2.ª parte
    return f


def team_projection(pre: dict, live: dict, minute: float, rem: float, score, reds) -> list[dict]:
    """pre: {stat: (mu_h, mu_a)} por 90'; live: {'H': {...}, 'A': {...}} contadores actuales."""
    out = []
    for st, (code, word) in TEAM_STATS.items():
        if st not in pre:
            continue
        row = {"stat": st, "code": code, "word": word}
        lam_tot = 0.0
        now_tot = 0
        for side, mu in zip(("H", "A"), pre[st]):
            c = int((live.get(side) or {}).get(st) or 0)
            k0 = K_TEAM.get(st, 60.0)
            rate = (mu / 90 * k0 + c) / (k0 + max(minute, 1))      # por minuto
            sf_ = state_factor(st, side, score, reds, minute)
            lam = rate * rem * sf_
            row[side] = {"now": c, "exp_rem": round(lam, 2), "exp_final": round(c + lam, 1), "pre": round(mu, 1),
                         "prior_rem": round(mu / 90 * rem * sf_, 2)}
            lam_tot += lam
            now_tot += c
        row["T"] = {"now": now_tot, "exp_rem": round(lam_tot, 2), "exp_final": round(now_tot + lam_tot, 1),
                    "prior_rem": round(row["H"]["prior_rem"] + row["A"]["prior_rem"], 2)}
        out.append(row)
    return out


def lines_for(now: int, lam: float, lo: float = 0.25, hi: float = 0.88, n: int = 4, r: float | None = None) -> list[dict]:
    """Líneas «más de» interesantes: las que tienen entre un 25 % y un 88 % de salir."""
    out = []
    for k in range(now + 1, now + 25):
        p = nb_ge(lam, k - now, r) if r else pois_ge(lam, k - now)
        if p < lo:
            break
        if p <= hi:
            out.append({"line": k - 0.5, "need": k - now, "p": round(p, 4), "fair": round(1 / p, 2), "min_odds": round(1.06 / p, 2)})
        if len(out) >= n:
            break
    return out


def player_projection(players: list[dict], pre_rates: dict, minute: float, rem: float, score, side_of: dict) -> list[dict]:
    """players: jugadores en el campo (Sofascore) con sus stats en vivo."""
    out = []
    for p in players:
        pid = p["id"]
        st = p["stats"]
        on = st.get("minutesPlayed")
        if on is None:
            continue
        if p.get("off"):        # ya sustituido
            continue
        on = float(on)
        pr = pre_rates.get(pid, {})
        exp_exit = pr.get("exit") or (90 if p.get("starter") else 90)
        my_rem = max(0.0, min(rem, (exp_exit - minute) if exp_exit < 90 and p.get("starter") else rem))
        item = {"id": pid, "name": p["name"], "side": side_of.get(pid), "pos": p.get("pos"), "on": on, "rem": round(my_rem), "stats": {}}
        for k, word in PLAYER_STATS.items():
            c = int(live_val(st, k))
            mu = pr.get(k)
            if mu is None:
                continue
            rate = (mu / 90 * K_PLAYER + c) / (K_PLAYER + max(on, 1))
            lam = rate * my_rem * (state_factor(k, item["side"] or "H", score, (0, 0), minute) if k in ("shots", "sot") else 1.0)
            sfp = (state_factor(k, item["side"] or "H", score, (0, 0), minute) if k in ("shots", "sot") else 1.0)
            item["stats"][k] = {"now": c, "exp_rem": round(lam, 2), "pre90": round(mu, 2), "lines": lines_for(c, lam, 0.22, 0.9, 2), "word": word,
                                "prior_rem": round(mu / 90 * my_rem * sfp, 3)}
        # tiros a puerta de cabeza / desde fuera del área (ESPN, jugada a jugada): su ritmo a puerta × lo que suele
        # rematar así según su puesto
        from ..markets_all import _share
        for k, key, how, word in (("sot_head", "_headSot", "header", "tiros a puerta de cabeza"),
                                  ("sot_out", "_outSot", "outside the box", "tiros a puerta desde fuera del área")):
            if pr.get("sot") is None or st.get(key) is None:
                continue
            mu = pr["sot"] * _share({"pos": p.get("pos"), "shares": pr.get("shares")}, how, sot=True)   # lo suyo (Statshub)
            c = int(st.get(key) or 0)
            rate = (mu / 90 * K_PLAYER + c) / (K_PLAYER + max(on, 1))
            sfp = state_factor("sot", item["side"] or "H", score, (0, 0), minute)
            lam = rate * my_rem * sfp
            item["stats"][k] = {"now": c, "exp_rem": round(lam, 2), "pre90": round(mu, 2), "lines": lines_for(c, lam, 0.22, 0.9, 2),
                                "word": word, "prior_rem": round(mu / 90 * my_rem * sfp, 3)}
        # tarjeta: su tasa de amarillas, más alta si va acumulando faltas
        yr = pr.get("yellow")
        if yr is not None and not live_val(st, "yellow"):
            fouls = live_val(st, "fouls")
            exp_f = (pr.get("fouls") or 1.0) * on / 90
            boost = 1 + 0.35 * max(0.0, fouls - exp_f)
            lam = yr / 90 * my_rem * boost * (1.25 if minute >= 45 else 1.0)
            p = 1 - math.exp(-lam)
            item["card"] = {"p": round(p, 4), "fair": round(1 / p, 2) if p > 0.02 else None, "fouls": fouls}
        # gol: su xG por 90 actualizado con el xG que lleva
        xg90 = pr.get("xg")
        if xg90 is not None and not live_val(st, "goals"):
            xg_now = float(st.get("expectedGoals") or 0)
            rate = (xg90 / 90 * K_PLAYER + xg_now) / (K_PLAYER + max(on, 1))
            p = 1 - math.exp(-rate * my_rem)
            item["goal"] = {"p": round(p, 4), "fair": round(1 / p, 2) if p > 0.02 else None, "xg_now": round(xg_now, 2)}
        out.append(item)
    return out


def live_val(st: dict, k: str) -> float:
    if k == "shots":
        if st.get("totalShots") is not None:
            return float(st["totalShots"])
        return float((st.get("onTargetScoringAttempt") or 0) + (st.get("shotOffTarget") or 0) + (st.get("blockedScoringAttempt") or 0)
                     + (st.get("hitWoodwork") or 0))
    key = {"sot": "onTargetScoringAttempt", "fouls": "fouls", "was_fouled": "wasFouled", "tackles": "totalTackle",
           "goals": "goals", "yellow": "yellowCard"}.get(k, k)
    v = st.get(key)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def pre_player_rates(pids: list[int], before: int) -> dict:
    """Por 90' de los últimos 15 partidos de cada jugador + su minuto habitual de cambio."""
    from ..insights import player_rate, _player_rows
    out = {}
    for pid in pids:
        r = {}
        try:
            from ..markets_all import player_shares
            r["shares"] = player_shares(pid, None, before)
        except Exception:
            pass
        for k in ("shots", "sot", "fouls", "was_fouled", "tackles", "yellow", "xg"):
            v, n, mins = player_rate(pid, before, k, 15)
            if v is not None:
                r[k] = v
        rows = _player_rows(pid, before, 10)
        starts = [x["minutes"] for x in rows if x["starter"] and (x["minutes"] or 0) > 0]
        if starts:
            r["exit"] = sorted(starts)[len(starts) // 2]
        out[pid] = r
    return out


def build(m: dict, minute: float, st: dict, cm: dict, lu_raw: dict | None, incs_raw: list | None, prerates: dict) -> dict:
    """Instantánea de proyecciones de un partido en juego."""
    from ..collectors.sofascore import parse_lineups
    score = tuple(st.get("score") or (0, 0))
    reds = tuple(st.get("reds") or (0, 0))
    rem = remaining(minute, st.get("added"))
    ls = st.get("stats") or {"H": {}, "A": {}}
    pre = {}
    anchor = st.get("anchor") or {}
    for s_, (code, _) in TEAM_STATS.items():
        f = (cm.get(code) or {}).get("model")
        if f is not None:
            h, a = float(f.mu_h), float(f.mu_a)
            if anchor.get(s_) and h + a > 0:          # se reparte el total de las casas según nuestro modelo
                k = anchor[s_] / (h + a)
                h, a = h * k, a * k
            pre[s_] = (h, a)
    live = {side: dict(ls.get(side) or {}) for side in ("H", "A")}
    teams = team_projection(pre, live, minute, rem, score, reds)
    # dominio territorial: quién pisa más el área rival (toques en el área) empuja un poco sus tiros y córners
    bh, ba = (live["H"].get("box_touches") or 0), (live["A"].get("box_touches") or 0)
    terr = None
    mom = st.get("mom") or {}
    if bh + ba >= 8 or mom.get("share_h") is not None:
        # toques en el área (Sofascore) o, si no, la presión de los últimos 10' según ESPN (xG de sus tiros y córners)
        terr = bh / (bh + ba) if bh + ba >= 8 else 0.5 + (mom["share_h"] - 0.5) * 0.6
        for t in teams:
            if t["stat"] in ("shots", "sot", "corners"):
                tot = 0.0
                for side, sh_ in (("H", terr), ("A", 1 - terr)):
                    f = min(1.15, max(0.87, (sh_ / 0.5) ** 0.35))
                    t[side]["exp_rem"] = round(t[side]["exp_rem"] * f, 2)
                    t[side]["exp_final"] = round(t[side]["now"] + t[side]["exp_rem"], 1)
                    tot += t[side]["exp_rem"]
                t["T"]["exp_rem"] = round(tot, 2)
                t["T"]["exp_final"] = round(t["T"]["now"] + tot, 1)
    for t in teams:
        r = DISP.get(t["stat"])
        t["lines"] = lines_for(t["T"]["now"], t["T"]["exp_rem"], r=r)
        for side in ("H", "A"):
            t[side]["lines"] = lines_for(t[side]["now"], t[side]["exp_rem"], 0.25, 0.88, 2, r=r)
    players = []
    if lu_raw:
        lu = parse_lineups(lu_raw)
        subs_off, booked = set(), set()
        for i in incs_raw or []:
            if i.get("incidentType") == "substitution":
                po = (i.get("playerOut") or {}).get("id")
                if po:
                    subs_off.add(po)
            if i.get("incidentType") == "card" and not i.get("rescinded"):
                pc = (i.get("player") or {}).get("id")
                if pc:
                    booked.add(pc)
        for side in ("H", "A"):
            for p in lu[side]:
                if p["id"] in booked:
                    p["stats"] = {**(p.get("stats") or {}), "yellowCard": 1}
        on_pitch, side_of = [], {}
        for side in ("H", "A"):
            for p in lu[side]:
                side_of[p["id"]] = side
                mp = (p.get("stats") or {}).get("minutesPlayed")
                if mp is None or p["id"] in subs_off or (p.get("stats") or {}).get("_off"):
                    continue
                on_pitch.append({**p, "off": False})
        players = player_projection(on_pitch, prerates, minute, rem, score, side_of)
    return {"match_id": m["id"], "minute": round(minute, 1), "rem": round(rem, 1), "score": list(score), "reds": list(reds),
            "teams": teams, "players": players, "ts": int(time.time()), "territory": terr,
            "live": {side: {k: live[side].get(k) for k in ("poss", "xg", "box_touches", "final_third", "big_chances", "offsides", "saves",
                                                            "crosses", "tackles", "blocked")} for side in ("H", "A")},
            "mom": mom or None}


SHARP_ORDER = ("Pinnacle", "Betfair Exchange", "Bet365", "Sofascore", "Kambi", "Paddy Power")
EXCH = {"Betfair Exchange", "Matchbook", "Smarkets"}


class LiveOdds(dict):
    """clave → cuota típica de una casa normal (mediana de las casas al día, sin exchanges), para no prometer
    precios que bet365 no da. `by_book` guarda todas; `books[clave]` cuántas casas la ofrecen."""
    def __init__(self):
        super().__init__()
        self.by_book: dict = {}
        self.books: dict = {}


def live_odds(match_id: int, since: int) -> LiveOdds:
    """Últimas cuotas en directo (Statshub: Pinnacle, Betfair, Kambi, Paddy Power… y Sofascore) por (mercado, selección, línea)."""
    rows = db.q("SELECT bookmaker, market, selection, line, odds, ts FROM odds WHERE match_id=? AND phase='live' AND ts>=? ORDER BY ts",
                (match_id, since))
    sh = db.kv_get(f"live_book_odds:{match_id}") or {}
    if (sh.get("ts") or 0) >= since:
        rows = list(rows) + [{"bookmaker": b, "market": mk, "selection": sel, "line": ln, "odds": o, "ts": sh["ts"]}
                             for b, mk, sel, ln, per, o in sh.get("rows", [])]
    out = LiveOdds()
    per_key: dict = {}
    for r in rows:
        k = (r["market"], r["selection"], None if r["line"] is None else round(float(r["line"]), 2))
        out.by_book.setdefault(r["bookmaker"], {})[k] = r["odds"]
        per_key.setdefault(k, {})[r["bookmaker"]] = r["odds"]
    for k, d in per_key.items():
        if "Bet365" in d:
            out[k] = d["Bet365"]         # bet365 en directo (Oddspedia)
        elif "Sofascore" in d:
            out[k] = d["Sofascore"]      # las cuotas de Sofascore son las de bet365 (comprobado: coinciden al céntimo)
        else:
            soft = sorted(o for b, o in d.items() if b not in EXCH)
            vals = soft or sorted(d.values())
            out[k] = vals[(len(vals) - 1) // 2]          # mediana (si son 2, la más baja: prudente)
        out.books[k] = {("bet365" if b in ("Sofascore", "Bet365") else b): o for b, o in d.items()}
    return out


def market_prob(odds: dict, key: tuple) -> float | None:
    """Probabilidad sin margen según la casa más afinada que tenga las dos (o tres) opciones a la vez."""
    mk, sel, ln = key
    if key not in odds:
        return None
    by_book = getattr(odds, "by_book", None) or {"_": dict(odds)}
    order = [b for b in SHARP_ORDER if b in by_book] + [b for b in by_book if b not in SHARP_ORDER]
    for b in order:
        bo = by_book[b]
        if key not in bo:
            continue
        if mk == "1X2":
            group = [bo.get(("1X2", s_, None)) for s_ in ("H", "D", "A")]
            if not all(group):
                continue
            return (1 / bo[key]) / sum(1 / o for o in group)
        other = {"O": "U", "U": "O", "H": "A", "A": "H"}.get(sel)
        oln = -ln if mk == "AH" and ln is not None else ln
        o2 = bo.get((mk, other, oln))
        if not o2:
            continue
        a, c = 1 / bo[key], 1 / o2
        return a / (a + c)
    return None


def blended(p_model: float, odds: dict, key: tuple) -> tuple[float, float | None]:
    pm = market_prob(odds, key)
    if pm is None:
        return p_model * W_MODEL + (1 / odds[key]) / 1.05 * (1 - W_MODEL), None
    return W_MODEL * p_model + (1 - W_MODEL) * pm, pm


def signals(snap: dict, m: dict, odds: dict, fair: dict | None = None) -> list[dict]:
    """Avisos: líneas con cuota en directo que paga más de la justa, y apuestas de jugador claras con su cuota mínima."""
    out = []
    # goles, hándicap y resultado: cuota justa del modelo en directo frente a la cuota en directo publicada
    names = {"H": m["home_name"], "A": m["away_name"], "D": "empate"}
    for mk, d in (fair or {}).items():
        if mk not in ("OU", "AH", "1X2") or not isinstance(d, dict):
            continue
        for k, fo in d.items():
            if mk == "1X2" and k not in ("H", "D", "A"):
                continue
            if isinstance(fo, (list, tuple)):
                fo = fo[-1] if fo and isinstance(fo[-1], (int, float)) else None
            if not isinstance(fo, (int, float)) or fo <= 1:
                continue
            fo = float(fo)
            if mk == "1X2":
                key, title = ("1X2", k, None), (f"Gana {names[k]}" if k != "D" else "Empate")
            else:
                try:
                    ln = float(k[1:])
                except ValueError:
                    continue
                key = (mk, k[0], ln)
                title = (f"{'Más' if k[0] == 'O' else 'Menos'} de {str(ln).replace('.', ',')} goles" if mk == "OU"
                         else f"{names.get(k[0], k[0])} {'+' if ln > 0 else ''}{str(ln).replace('.', ',')} (hándicap)")
            o = odds.get(key)
            if not o:
                continue
            pmod = 1 / fo
            p, pmk = blended(pmod, odds, key)
            ev = p * o - 1
            if pmk is None or abs(pmod - pmk) > 0.15:
                continue        # sin cuota contraria para quitar el margen, o tan lejos del mercado que es más fácil que el error sea nuestro
            if ev > 0.04 and 0.25 <= p <= 0.85:
                out.append({"kind": "goals", "title": title, "market": key[0], "sel": key[1], "line": key[2],
                            "why": f"{snap['score'][0]}-{snap['score'][1]} en el {int(snap['minute'])}': nuestro modelo en directo (xG, rojas, ritmo) da {round(pmod * 100)} %"
                                   + (f" y el mercado {round(pmk * 100)} %" if pmk else ""),
                            "p": round(p, 4), "fair": round(1 / p, 2), "odds": o, "ev": round(ev, 3), "min_odds": round(1.04 / p, 2),
                            "books": sorted((getattr(odds, "books", {}) or {}).get(key, {}).keys())})
    code_mk = {"CORNERS": "CORNERS_OU", "CARDS": "CARDS_OU"}
    for t in snap["teams"]:
        mk = code_mk.get(t["code"])
        for ln in t.get("lines", []):
            key = (mk, "O", ln["line"])
            o = odds.get(key) if mk else None
            if not o:
                continue
            p, pmk = blended(ln["p"], odds, key)
            ev = p * o - 1
            # solo si lo que está pasando HOY empuja: si con la media de antes ya saldría igual, la diferencia con el
            # mercado es sesgo nuestro, no información
            p_prior = nb_ge(t["T"]["prior_rem"], ln["need"], DISP.get(t["stat"], 10.0))
            if ln["p"] - p_prior < 0.07 or (pmk is not None and ln["p"] - pmk > 0.25):
                continue
            if ev > 0.04 and 0.35 <= p <= 0.85:
                out.append({"kind": "team", "title": f"Más de {str(ln['line']).replace('.', ',')} {t['word']} en el partido",
                            "market": mk, "sel": "O", "line": ln["line"],
                            "why": f"Van {t['T']['now']}; con el ritmo de hoy y lo que esperábamos faltan {t['T']['exp_rem']:.1f} de media. Modelo {round(ln['p'] * 100)} %"
                                   + (f", mercado {round(pmk * 100)} %" if pmk else ""),
                            "p": round(p, 4), "fair": round(1 / p, 2), "odds": o, "ev": round(ev, 3), "min_odds": round(1.04 / p, 2),
                            "books": sorted((getattr(odds, "books", {}) or {}).get(key, {}).keys())})
    late_enough = snap["minute"] >= MIN_SIGNAL_MINUTE
    for p in (snap["players"] if late_enough else []):
        for k, s in p["stats"].items():
            for ln in s["lines"][:1]:
                p_prior = pois_ge(s.get("prior_rem", 0), ln["need"])
                # solo si hoy está por encima de lo suyo (lleva más de lo normal a estas alturas)
                if 0.45 <= ln["p"] <= 0.75 and s["exp_rem"] >= 0.6 and ln["p"] - p_prior >= 0.06 and s["now"] >= 1:
                    # lo que pondrá bet365: tira hacia el «sí» (usa el ritmo más alto entre el de hoy y el suyo, algo
                    # inflado) y le quita su margen. Con 1 que le falta y mucho tiempo, la cuota sale muy corta: si ni
                    # así llega a la mínima que haría falta, no se avisa (antes salían avisos imposibles)
                    lam_b = max(s["exp_rem"], s.get("prior_rem", 0)) * 1.15
                    p_book = min(0.97, pois_ge(lam_b, ln["need"]))
                    est = round(0.93 / p_book, 2) if p_book > 0 else None
                    if not est or est < ln["min_odds"]:
                        continue
                    team = m["home_name"] if p["side"] == "H" else m["away_name"]
                    out.append({"kind": "player", "title": f"{p['name']}: {ln['need'] + s['now']} o más {s['word']}",
                                "pid": p["id"], "stat": k, "line": ln["need"] + s["now"] - 0.5,
                                "why": f"Lleva {s['now']} en {int(p['on'])}' ({team}); su media es {s['pre90']:.1f} por partido y le quedan unos {p['rem']}'. "
                                       f"bet365 suele dar ≈{est:.2f}; solo vale si paga {ln['min_odds']:.2f} o más".replace(".", ","),
                                "p": ln["p"], "fair": ln["fair"], "odds": None, "est_odds": est, "ev": None, "min_odds": ln["min_odds"]})
        c = p.get("card")
        if c and c["p"] >= 0.30 and c["fouls"] >= 2:
            out.append({"kind": "player", "title": f"{p['name']}: le sacan tarjeta", "why": f"Lleva {int(c['fouls'])} faltas y suele ver amarillas",
                        "pid": p["id"], "stat": "yellow", "line": 0.5,
                        "p": c["p"], "fair": c["fair"], "odds": None, "ev": None, "min_odds": round(1.08 / c["p"], 2)})
    out = [x for x in out if x["min_odds"] >= MIN_ODDS or (x["odds"] and x["odds"] >= MIN_ODDS)]
    out.sort(key=lambda s: (-(s["ev"] or 0), -s["p"]))
    return out[:12]
