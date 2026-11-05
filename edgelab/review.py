"""Revisión semanal: mirar una a una las recomendaciones resueltas, entender por qué salieron o no, y reajustarse.

Para cada apuesta resuelta de los últimos días:
  · se rehace su análisis con los datos que había antes del partido (las señales que la apoyaban o la frenaban)
  · se mira qué pasó de verdad: minutos del jugador, su cifra, roja, goleada, si se quedó a una de la línea
Con eso:
  · cada tipo de señal (racha, rival, duelo, árbitro, rotación…) recibe un peso según si acertó más o menos de lo esperado
    cuando empujaba a favor; el análisis de cada apuesta usa esos pesos a partir de entonces
  · se resume por qué se fallaron las que se fallaron (para corregir lo que dependa de nosotros)
"""
from __future__ import annotations

import json
import time

from .core import db

K = 20.0   # cuántas apuestas "imaginarias" en la media hacen falta para mover un peso: con pocas, casi no se mueve


def _actual(b: dict, f: dict) -> dict:
    """Lo que pasó de verdad en el partido para esta apuesta."""
    out = {}
    m = db.q1("SELECT hg, ag FROM matches WHERE id=?", (b["match_id"],))
    if m and m["hg"] is not None:
        out["margin"] = abs(m["hg"] - m["ag"])
    reds = db.q1("SELECT COUNT(*) AS n FROM timeline WHERE match_id=? AND kind='red'", (b["match_id"],))
    out["red"] = bool(reds and reds["n"])
    if f.get("type") == "player" and (f.get("player") or {}).get("id"):
        from .insights import _val
        ps = db.q1("SELECT * FROM player_stats WHERE match_id=? AND player_id=?", (b["match_id"], f["player"]["id"]))
        if ps:
            out["minutes"] = ps["minutes"]
            out["value"] = _val(ps, f["player"].get("stat"))
        else:
            out["minutes"] = 0
    return out


def _reason(b: dict, f: dict, a: dict) -> str:
    if b["status"] in ("won", "halfwon"):
        return "acertada"
    if f.get("type") == "player":
        exp = (f.get("player") or {}).get("exp_min") or 70
        if (a.get("minutes") or 0) == 0:
            return "no jugó"
        if a["minutes"] < min(55, exp - 15):
            return "jugó menos de lo previsto"
        if a.get("value") is not None and f.get("line") is not None and abs(a["value"] - f["line"]) <= 0.5:
            return "se quedó a una"
    if a.get("red"):
        return "hubo roja"
    if (a.get("margin") or 0) >= 3:
        return "partido roto (goleada)"
    return "salió lo menos probable"


def weekly_review(days: int = 10, limit: int = 400, log=print) -> dict:
    from .insights import bet_insight
    since = int(time.time()) - days * 86400
    bets = db.q("SELECT b.* FROM bets b JOIN matches m ON m.id=b.match_id WHERE b.mode IN ('tip','scan') AND b.status NOT IN ('open','void') "
                "AND m.kickoff>=? ORDER BY m.kickoff DESC LIMIT ?", (since, limit))
    sig: dict = {}
    reasons: dict = {}
    verdicts: dict = {}
    groups: dict = {}
    done = 0
    for b in bets:
        try:
            f = bet_insight(b["match_id"], b["market"], b["period"] or "FT", b["selection"], b["line"])
        except Exception:
            f = None
        if not f:
            continue
        done += 1
        won = 1.0 if b["status"] == "won" else 0.5 if b["status"] == "halfwon" else 0.0
        p = float(b["fair_prob"] or (f.get("row") or {}).get("p") or 0.5)
        for s in f.get("signals", []):
            if not s.get("dir"):
                continue
            d = sig.setdefault(s["key"], {"n": 0, "exp": 0.0, "won": 0.0})
            # si la señal empujaba a favor, ¿salió más de lo esperado? si empujaba en contra, ¿salió menos?
            d["n"] += 1
            d["exp"] += p if s["dir"] > 0 else 1 - p
            d["won"] += won if s["dir"] > 0 else 1 - won
        a = _actual(b, f)
        r = _reason(b, f, a)
        reasons[r] = reasons.get(r, 0) + 1
        v = verdicts.setdefault(f.get("verdict", "?"), {"n": 0, "won": 0.0, "pnl": 0.0})
        v["n"] += 1
        v["won"] += won
        v["pnl"] += b["pnl"] or 0
        g = groups.setdefault((json.loads(b["notes"] or "{}")).get("group", "?"), {"n": 0, "won": 0.0, "exp": 0.0, "pnl": 0.0})
        g["n"] += 1
        g["won"] += won
        g["exp"] += p
        g["pnl"] += b["pnl"] or 0
    weights = {}
    for k, d in sig.items():
        ratio = (d["won"] + K * (d["exp"] / d["n"])) / (d["exp"] + K * (d["exp"] / d["n"])) if d["n"] else 1.0
        weights[k] = {"n": d["n"], "exp": round(d["exp"], 2), "won": round(d["won"], 2), "w": round(min(1.6, max(0.4, ratio ** 2)), 3)}
    res = {"ts": int(time.time()), "days": days, "reviewed": done, "weights": weights, "reasons": reasons,
           "verdicts": {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in verdicts.items()},
           "groups": {k: {**v, "exp": round(v["exp"], 2), "pnl": round(v["pnl"], 2)} for k, v in groups.items()}}
    db.kv_set("review", res)
    db.kv_set("signal_weights", {k: v["w"] for k, v in weights.items()})
    if log:
        log(f"revisión: {done} apuestas, {len(weights)} tipos de señal reajustados")
    return res


def signal_weight(key: str) -> float:
    w = _SW.get("v")
    if w is None or time.time() - _SW.get("t", 0) > 600:
        _SW["v"] = w = db.kv_get("signal_weights") or {}
        _SW["t"] = time.time()
    return float(w.get(key, 1.0))


_SW: dict = {}


# ---------------------------------------------------------------------------- por qué salió o no CADA apuesta
_PK = {"shots": ("shots", "tiros"), "shots on target": ("sot", "tiros a puerta"), "fouls": ("fouls", "faltas"),
       "fouls committed": ("fouls", "faltas"), "fouled": ("was_fouled", "faltas recibidas"), "tackles": ("tackles", "entradas")}
_TM = {"CORNERS": "córners", "CARDS": "tarjetas", "SHOTS": "tiros", "SOT": "tiros a puerta", "FOULS": "faltas"}


def _fmt(x) -> str:
    return f"{x:.1f}".replace(".", ",") if isinstance(x, float) else str(x)


def explain_bet(b: dict, m: dict, rep: dict, tl: list[dict]) -> list[str]:
    """Frases concretas con lo que pasó de verdad frente a lo que esperábamos, para esta apuesta."""
    why = []
    hn, an = m["home_name"], m["away_name"]
    goals = [g for g in tl if g["kind"] == "goal"]
    if goals:
        g0 = goals[0]
        why.append(f"Marcó primero {hn if g0['side'] == 'H' else an} en el {int(g0['minute'])}′")
    elif m["hg"] == 0 and m["ag"] == 0:
        why.append("No hubo goles")
    for r in [x for x in tl if x["kind"] == "red"][:2]:
        why.append(f"Roja para {hn if r['side'] == 'H' else an} en el {int(r['minute'])}′")
    if m["hg"] is not None and abs(m["hg"] - m["ag"]) >= 3:
        why.append(f"Partido roto: {m['hg']}-{m['ag']}")
    mk, sel = b["market"] or "", b["selection"] or ""
    if mk.startswith("RAW:Player"):
        kind = next((v for k, v in _PK.items() if mk.lower().endswith(k)), None)
        name = sel.rsplit(" Over", 1)[0].rsplit(" Under", 1)[0].strip()
        p = next((x for x in rep.get("players", []) if x["name"] and (x["name"] in name or name in x["name"])), None)
        if p:
            if p["min"] <= 0:
                why.insert(0, f"{p['name']} no jugó")
            else:
                if p["min"] < p["exp_min"] - 15:
                    why.insert(0, f"{p['name']} jugó {p['min']}′ (le dábamos {p['exp_min']}′)" + (f", salió en el {p['sub']['out']}′" if (p.get("sub") or {}).get("out") else ""))
                if kind:
                    col, word = kind
                    a, e = p["act"].get(col), p["exp_at_min"].get(col)
                    if a is not None and e is not None:
                        txt = f"Hizo {a} {word}; con sus minutos esperábamos {_fmt(e)}"
                        if b["line"] is not None and abs(a - float(b["line"])) <= 0.5:
                            txt += " (se quedó a una)"
                        why.insert(1 if len(why) and why[0].startswith(p["name"]) else 0, txt)
                du = p.get("duel")
                if du and du.get("real"):
                    if du.get("exp") and du["exp"] != du["real"]:
                        why.append(f"Esperábamos que se cruzara con {du['exp']}, pero su par real fue {du['real']}")
                    else:
                        why.append(f"Su par fue {du['real']}, como esperábamos")
                dr = p.get("dribbles")
                if dr and dr.get("exp") is not None and kind and kind[0] in ("was_fouled", "fouls") and abs(dr["act"] - dr["exp"]) >= 1:
                    why.append(f"Regateó {dr['act']} veces (suele {_fmt(dr['exp'])} con esos minutos)")
                h = p.get("heat")
                if h and abs(h["x"] - h["px"]) >= 6:
                    dx = h["x"] - h["px"]
                    why.append(f"Jugó {abs(round(dx))} m más {'adelantado' if dx > 0 else 'atrás'} de lo habitual (mapa de calor)")
    elif mk.split("_")[0] in _TM and mk.endswith("_OU"):
        code = mk.split("_")[0]
        t = next((x for x in rep.get("teams", []) if x["code"] == code), None)
        if t:
            why.insert(0, f"Hubo {sum(t['act']):g} {_TM[code]} ({t['act'][0]:g}-{t['act'][1]:g}); esperábamos {_fmt(sum(t['exp']))}")
    elif mk in ("OU", "BTTS", "1X2", "DC", "AH", "DNB", "TEAM_OU"):
        g = rep.get("goals") or {}
        if g.get("exp") and g["exp"][0] is not None:
            why.insert(0, f"Goles esperados {_fmt(g['exp'][0])}-{_fmt(g['exp'][1])}; acabó {m['hg']}-{m['ag']}")
    return why[:5]


def explain_match_bets(mid: int) -> int:
    rep = db.kv_get(f"post:{mid}") or {}
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m or m["hg"] is None:
        return 0
    tl = db.q("SELECT minute, kind, side FROM timeline WHERE match_id=? AND kind IN ('goal','red') ORDER BY minute", (mid,))
    n = 0
    for b in db.q("SELECT * FROM bets WHERE match_id=? AND status NOT IN ('open','void')", (mid,)):
        notes = json.loads(b["notes"] or "{}") if (b["notes"] or "").startswith("{") else {"txt": b["notes"]}
        why = explain_bet(b, m, rep, tl)
        if why:
            notes["why"] = why
            db.execute("UPDATE bets SET notes=? WHERE id=?", (json.dumps(notes, ensure_ascii=False), b["id"]))
            n += 1
    return n
