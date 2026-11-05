"""Movimientos de las casas afiladas (B1) y hora de las alineaciones (B2): ver antes que bet365 lo que bet365 aún no
ha reflejado en su cuota.

B1 · Pinnacle y Betfair reaccionan antes que nadie a noticias y dinero informado. Si su probabilidad (sin margen) de
una selección sube ≥ 3 puntos en las últimas horas y bet365 sigue pagando lo mismo, bet365 va por detrás: su cuota
vale más de lo que debería. Aviso inmediato.

B2 · Entre 80 y 25 minutos antes del saque se mira el once cada minuto. En cuanto es oficial se recalcula el partido
y se avisa de las apuestas de jugador con valor con el once ya confirmado, antes de que bet365 reajuste.
"""
from __future__ import annotations

import json
import time

from .core import db

SHARPS = ("Pinnacle", "Betfair Exchange")
MKTS = ("1X2", "OU", "AH")


def _devig(odds: list[float]) -> list[float] | None:
    if any(o is None or o <= 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def _at(hist: list[tuple[int, float]], t: int) -> float | None:
    v = None
    for ts, o in hist:
        if ts <= t:
            v = o
        else:
            break
    return v


def _groups(market: str, sel: str, line):
    """Selecciones que forman el mercado completo (para quitar el margen)."""
    if market == "1X2":
        return [("H", None), ("D", None), ("A", None)]
    if market == "OU":
        return [("O", line), ("U", line)]
    if market == "AH" and line is not None:
        return [("H", line), ("A", -line)] if sel == "H" else [("H", -line), ("A", line)]
    return None


def steam_signals(mid: int, now: int | None = None, back_min: int = 45, window_h: float = 3.0, min_move: float = 0.03) -> list[dict]:
    """Selecciones en las que una casa afilada ha subido su probabilidad ≥ min_move y bet365 no se ha movido."""
    now = now or int(time.time())
    rows = db.q("SELECT bookmaker, market, selection, line, odds, ts FROM odds WHERE match_id=? AND period='FT' AND market IN (?,?,?) "
                "AND bookmaker IN ('Pinnacle','Betfair Exchange','Bet365') AND phase IN ('open','snap','close') AND ts>=? ORDER BY ts",
                (mid, *MKTS, now - int(48 * 3600)))
    hist: dict = {}
    for r in rows:
        ln = None if r["line"] is None else round(float(r["line"]), 2)
        hist.setdefault((r["bookmaker"], r["market"], r["selection"], ln), []).append((r["ts"], r["odds"]))
    t_old_max, t_old_min = now - back_min * 60, now - int(window_h * 3600)
    out = []
    seen = set()
    for (bk, mk, sel, ln), _ in hist.items():
        if bk not in SHARPS or (mk, sel, ln) in seen:
            continue
        grp = _groups(mk, sel, ln)
        if not grp:
            continue
        def probs(t):
            return _devig([_at(hist.get((bk, mk, s2, l2), []), t) for s2, l2 in grp])
        p_new = probs(now)
        # la foto «de antes»: la más antigua dentro de la ventana (o la última antes de ella)
        p_old = probs(t_old_min) or probs(t_old_max)
        if not p_new or not p_old:
            continue
        i = [s2 for s2, _ in grp].index(sel) if sel in [s2 for s2, _ in grp] else None
        if i is None:
            continue
        move = p_new[i] - p_old[i]
        if move < min_move:
            continue
        h365 = hist.get(("Bet365", mk, sel, ln), [])
        o_now, o_old = _at(h365, now), _at(h365, t_old_min) or _at(h365, t_old_max)
        if not o_now or not o_old or o_now < o_old * 0.985:
            continue                     # bet365 ya ha bajado su cuota: ha reaccionado
        fair = 1 / p_new[i]
        ev = p_new[i] * o_now - 1
        if ev < 0.03:
            continue
        seen.add((mk, sel, ln))
        out.append({"book": bk, "market": mk, "selection": sel, "line": ln, "period": "FT", "p_old": round(p_old[i], 4),
                    "p": round(p_new[i], 4), "move": round(move, 4), "b365": o_now, "b365_old": o_old, "fair": round(fair, 3),
                    "ev": round(ev, 4)})
    out.sort(key=lambda x: -x["ev"])
    return out


def steam_alerts(mid: int) -> int:
    """Aviso por Telegram (una vez por selección cada 6 h) y se apunta para el repaso y para medir si funciona."""
    from .followup import page_stake
    from .notify import alert
    from .pricing import label
    from .tipster import _when, card, fold, risk_line, ten_dots
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m or m["status"] != "notstarted":
        return 0
    n = 0
    for s in steam_signals(mid):
        lab = label(s["market"], s["selection"], s["line"], "FT", m["home_name"], m["away_name"])
        stake = page_stake(s["p"], s["b365"], False, min(0.03, 0.25 * s["ev"] / (s["b365"] - 1)))
        k10 = max(0, min(10, round(s["p"] * 10)))
        rk = "🟢 Riesgo bajo" if s["p"] >= 0.62 else "🟡 Riesgo medio" if s["p"] >= 0.45 else "🔴 Riesgo alto"
        msg = (f"⚽ <b>{m['home_name']} – {m['away_name']}</b>\n🕘 {_when(m['kickoff'])}\n\n"
               + card([f"✅ <b>{lab}</b>",
                       f"💶 bet365 sigue a <b>{s['b365']:.2f}</b>  ·  apuesta <b>{stake:g} €</b>",
                       f"📉 {s['book']}: del {round(s['p_old'] * 100)} % al <b>{round(s['p'] * 100)} %</b>",
                       f"🎲 {ten_dots(s['p'])}  <b>{k10}/10</b>  ·  {rk}",
                       f"📈 Valor <b>+{round(s['ev'] * 100)} %</b>  ·  vale hasta <b>{s['fair'] * 1.02:.2f}</b>"])
               + "\n👉 Estos desfases duran poco: si la quieres, ahora.\n\n"
               + fold([f"{s['book']} (la casa que antes reacciona a noticias y dinero informado) ha pasado esta apuesta de un "
                       f"{round(s['p_old'] * 100)} % a un {round(s['p'] * 100)} % en las últimas horas: ahí ha entrado dinero o una noticia.",
                       f"bet365 no se ha movido (antes {s['b365_old']:.2f}, ahora {s['b365']:.2f}), así que paga más de lo que debería.",
                       f"Cuota justa ahora {s['fair']:.2f}. Vale mientras pague {s['fair'] * 1.02:.2f} o más.",
                       risk_line(s["p"])]))
        aid = alert("steam", "⚡ SE HAN MOVIDO LAS CASAS AFILADAS", msg,
                    match_id=mid, payload={"items": [{"label": lab, "market": s["market"], "selection": s["selection"], "line": s["line"],
                                                      "period": "FT", "b365": s["b365"], "p": s["p"], "ev": s["ev"], "stake_eur": stake}],
                                           "move": s},
                    dedupe=f"steam:{mid}:{s['market']}:{s['selection']}:{s['line']}:{int(time.time() // (6 * 3600))}")
        if aid:
            n += 1
            # se apunta como apuesta de prueba para saber, con el tiempo, si estos avisos baten el cierre
            db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,odds,odds_source,fair_prob,edge,"
                       "stake,status,notes) VALUES(NULL,NULL,?,?,'steam',?,?,?,'FT',?,'bet365',?,?,10,'open',?)",
                       (mid, int(time.time()), s["market"], s["selection"], s["line"], s["b365"], s["p"], s["ev"],
                        json.dumps({"label": lab, "book": s["book"], "move": s["move"]})))
    return n


def lineup_sprint() -> int:
    """Cada minuto entre 80 y 25 min antes del saque: ¿ya es oficial el once? En cuanto lo es, se baja, se recalcula el
    partido y se avisa de las apuestas de jugador con valor con el once confirmado."""
    from .live.engine import shub
    from .collectors import players as pl
    now = int(time.time())
    ms = db.q("SELECT id, home_name, away_name, kickoff, lineup_status FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? "
              "AND COALESCE(lineup_status,'') != 'confirmed'", (now + 25 * 60, now + 80 * 60))
    if not ms:
        return 0
    try:
        status = shub().lineup_status([m["id"] for m in ms])
    except Exception:
        return 0
    n = 0
    for m in ms:
        if status.get(m["id"]) != "confirmed":
            continue
        try:
            data = shub().predicted_lineup(m["id"])
            try:
                forms = shub().formations(m["id"])
            except Exception:
                forms = {}
            pl.store_predicted(m["id"], data, forms)
            db.execute("UPDATE matches SET lineup_status='confirmed' WHERE id=?", (m["id"],))
            db.execute("DELETE FROM reports WHERE match_id=?", (m["id"],))
        except Exception as e:
            db.log_job("lineups", "error", f"once {m['id']}: {e}")
            continue
        n += 1
        try:
            from .absences import team_ratios
            from .plans import analyze, tip_alerts
            team_ratios(m["id"], fresh=True)
            r = analyze(m["id"], use_cache=False) or {}
            tip_alerts(fast={m["id"]})
            lineup_alert(m, r)
        except Exception as e:
            db.log_job("lineups", "error", f"recalcular {m['id']}: {e}")
    return n


def lineup_alert(m: dict, r: dict) -> int:
    """Las mejores apuestas de jugador con el once ya confirmado (titulares, valor real, confianza media o alta)."""
    from .followup import page_stake
    from .notify import alert
    from .tipster import card, ten_dots
    from .plans import is_value
    vals = [v for v in (r.get("rows") or []) if is_value(v) and v.get("group") == "jugadores" and v.get("conf") in ("alta", "media")
            and (v.get("ev") or 0) >= 0.05 and v.get("b365") and not v.get("warn")]
    vals.sort(key=lambda v: -(v["ev"] / max(v["b365"] - 1, 0.2)))
    vals = vals[:6]
    if not vals:
        return 0
    lines = []
    items = []
    for v in vals:
        st = page_stake(v.get("p"), v["b365"], True, v.get("kf"))
        k10 = max(0, min(10, round(v["p"] * 10)))
        lines.append(card([f"✅ <b>{v['label']}</b>", f"💶 <b>{v['b365']:.2f}</b>  ·  apuesta <b>{st:g} €</b>  ·  valor <b>+{round(v['ev'] * 100)} %</b>",
                           f"🎲 {'●' * k10}{'○' * (10 - k10)}  <b>{k10}/10</b>"]))
        items.append({k: v.get(k) for k in ("label", "b365", "p", "ev", "market", "selection", "line", "period", "group", "kf")} | {"stake_eur": st})
    alert("lineup", "🧾 ONCE CONFIRMADO",
          f"⚽ {m['home_name']} – {m['away_name']}\nAcaba de salir el once oficial. Con él, estas apuestas de jugador tienen valor "
          f"(bet365 suele tardar en reajustarlas):\n\n" + "\n".join(lines) + "\n\n👉 Compruébalas rápido: en minutos se ajustan.",
          match_id=m["id"], payload={"items": items}, dedupe=f"lineup:{m['id']}")
    return len(vals)
