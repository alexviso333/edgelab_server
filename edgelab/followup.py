"""Repaso por Telegram al acabar cada partido: SOLO lo que el bot te envió de ese partido (apuestas del día, mercados
recién abiertos, picks de estrategias, avisos en directo de goles/equipo/jugadores) con si salió o no, lo que se
habría ganado o perdido con la apuesta que la web recomendaba (su Kelly con tu banca y riesgo de Ajustes) y el porqué."""
from __future__ import annotations

import json
import time

from .core import db
from .core.markets import Selection, settle, status_from_fracs

KINDS = ("digest", "mercado", "prematch", "live", "live_prop", "tip", "steam", "lineup")
WHERE = {"tip": "recomendada", "steam": "movimiento de casas afiladas", "lineup": "once confirmado", "digest": "apuesta del día", "mercado": "mercado recién abierto", "prematch": "estrategia",
         "live": "aviso en directo", "live_prop": "aviso en directo"}
PCOL = {"shots": "shots", "sot": "sot", "fouls": "fouls", "was_fouled": "was_fouled", "tackles": "tackles", "yellow": "yellow"}
ICON = {"won": "✅", "halfwon": "✅", "lost": "❌", "halflost": "❌", "push": "↩️", "void": "↩️", "pending": "⏳"}


def page_stake(p: float | None, odds: float | None, player: bool = False, kf: float | None = None) -> float:
    """Lo que la web recomienda apostar (igual que en las fichas): fracción de Kelly × tu banca × tu riesgo,
    con tope 5 % (1 % en jugadores), redondeado a 0,5 €; mínimo 1 €."""
    if kf is None:
        if not p or not odds or odds <= 1:
            return 1.0
        k = max(0.0, (p * odds - 1) / (odds - 1))
        kf = min(k * 0.125, 0.01) if player else min(k * 0.25, 0.05)
    from .config import settings
    u = db.kv_get("user_settings") or {}          # lo que pusiste en Ajustes (banca y riesgo)
    bank = float(u.get("bankroll") or settings.bankroll or 0)
    risk = float(u.get("risk") or 1)
    st = kf * bank * risk
    return max(1.0, round(st * 2) / 2)


def _is_player(market) -> bool:
    m = str(market or "")
    return m.startswith("RAW:") and any(w in m.lower() for w in ("player", "jugador", "shots", "fouls", "tackles", "assist", "goalscorer", "card"))


def _settle_key(m: dict, market: str, sel: str, line, period: str | None) -> tuple[float, float] | None:
    if not market or sel is None:
        return None
    if str(market).startswith("RAW:"):
        try:
            from .markets_all import settle_raw
            return settle_raw(m, market, period or "FT", sel, line)
        except Exception:
            return None
    try:
        from .learning.loop import _outcome
        o = _outcome(m)
        if o is None:
            return None
        return settle(Selection(market, sel, None if line is None else float(line), period or "FT"), o)
    except Exception:
        return None


def _bet_for(mid: int, market, sel, line, period) -> dict | None:
    return db.q1("SELECT * FROM bets WHERE match_id=? AND market=? AND selection=? AND COALESCE(line,-999)=COALESCE(?,-999) "
                 "AND COALESCE(period,'FT')=? ORDER BY CASE mode WHEN 'tip' THEN 0 WHEN 'opening' THEN 1 ELSE 2 END LIMIT 1",
                 (mid, market, sel, line, period or "FT"))


def _why(b: dict | None) -> str:
    if not b:
        return ""
    try:
        n = json.loads(b.get("notes") or "{}")
    except Exception:
        return ""
    w = n.get("why")
    if isinstance(w, list):
        w = "; ".join(str(x) for x in w[:2])
    return str(w or "")[:180]


def _item(status: str, odds: float | None, w: float | None = None, p: float | None = None) -> float | None:
    if odds is None or status in ("pending",):
        return None
    if w is None:
        w, p = {"won": (1, 0), "halfwon": (0.5, 0.5), "push": (0, 1), "void": (0, 1), "halflost": (0, 0.5), "lost": (0, 0)}.get(status, (0, 0))
    return round(w * (odds - 1) - (1 - w - (p or 0)), 2)


def match_items(mid: int) -> tuple[dict, list[dict]]:
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        return {}, []
    items, seen = [], set()
    canceled = m["status"] in ("postponed", "canceled")
    for a in db.q("SELECT * FROM alerts WHERE match_id=? AND sent=1 AND kind IN (%s) ORDER BY ts" % ",".join("?" * len(KINDS)), (mid, *KINDS)):
        try:
            pl = json.loads(a["payload"] or "{}")
        except Exception:
            pl = {}
        k = a["kind"]
        if k == "tip":
            continue                  # las recomendadas se repasan desde lo que se guardó al enviarlas (abajo)
        cands = []
        if k in ("digest", "mercado", "steam", "lineup"):
            for it in pl.get("items") or []:
                pll = it.get("group") == "jugadores" if it.get("group") else _is_player(it.get("market"))
                cands.append({"label": it.get("label"), "market": it.get("market"), "sel": it.get("selection"), "line": it.get("line"),
                              "period": it.get("period"), "odds": it.get("b365"),
                              "stake": it.get("stake_eur") or page_stake(it.get("p"), it.get("b365"), pll, it.get("kf"))})
        elif k == "prematch":
            b = db.q1("SELECT * FROM bets WHERE match_id=? AND strategy_id=? ORDER BY ABS(created-?) LIMIT 1", (mid, a["strategy_id"], a["ts"]))
            if b:
                cands.append({"label": pl.get("selection"), "bet": b, "odds": b["odds"], "stake": pl.get("stake") or b["stake"]})
        elif k == "live":
            b = db.q1("SELECT * FROM bets WHERE group_id=? LIMIT 1", (a["dedupe"],))
            cands.append({"label": pl.get("selection") or a["title"], "bet": b, "odds": (b or {}).get("odds") or pl.get("live_odds") or pl.get("min_odds"),
                          "minute": None, "stake": pl.get("stake") or (b or {}).get("stake") or 1.0})
        elif k == "live_prop":
            if not (pl.get("market") or pl.get("pid")):
                continue                 # avisos antiguos sin datos para comprobarlos
            o_ = pl.get("odds") or pl.get("min_odds")
            cands.append({"label": pl.get("title") or a["title"], "prop": pl, "odds": o_,
                          "minute": pl.get("minute"), "at_min": not pl.get("odds"),
                          "stake": pl.get("stake_eur") or page_stake(pl.get("p"), o_, pl.get("kind") == "player")})
        for c in cands:
            key = (c.get("label"), c.get("market"), c.get("sel"), c.get("line"), k == "live_prop")
            if key in seen:
                continue
            seen.add(key)
            c["where"] = WHERE.get(k, k)
            st, w, p, b = "pending", None, None, c.get("bet")
            if canceled:
                st = "void"
            elif b is None and c.get("market"):
                b = _bet_for(mid, c["market"], c["sel"], c["line"], c.get("period"))
            if not canceled and b and b["status"] not in ("open",):
                st = b["status"]
            elif not canceled and c.get("market"):
                r = _settle_key(m, c["market"], c["sel"], c["line"], c.get("period"))
                if r:
                    w, p = r
                    st = status_from_fracs(w, p)
            elif not canceled and c.get("prop"):
                pr = c["prop"]
                if pr.get("market"):
                    r = _settle_key(m, pr["market"], pr.get("sel"), pr.get("line"), "FT")
                    if r:
                        w, p = r
                        st = status_from_fracs(w, p)
                elif pr.get("pid") and PCOL.get(pr.get("stat")):
                    row = db.q1(f"SELECT {PCOL[pr['stat']]} AS v, minutes FROM player_stats WHERE player_id=? AND match_id=?", (pr["pid"], mid))
                    if row and row["v"] is not None:
                        st = "won" if float(row["v"]) > float(pr.get("line") or 0) else "lost"
                        c["actual"] = f"hizo {int(row['v'])} en {int(row['minutes'] or 0)}'"
            c["status"] = st
            u = _item(st, c.get("odds"), w, p)
            c["pnl"] = None if u is None else round(u * float(c.get("stake") or 1), 2)
            c["why"] = _why(b) if b else ""
            items.append(c)
    # recomendaciones oficiales enviadas (con la última cuota avisada); las retiradas antes del partido no cuentan
    for kk, t in (db.kv_get("tips_sent") or {}).items():
        if t.get("mid") != mid or not t.get("sent"):
            continue
        it = {"label": t["label"], "where": "recomendada", "odds": t["odds"], "stake": t["stake"], "status": "pending", "pnl": None, "why": ""}
        if t.get("state") == "retracted" or canceled:
            it["status"] = "void"
            if t.get("state") == "retracted":
                it["label"] += " (retirada antes del partido, no cuenta)"
        else:
            b = _bet_for(mid, t["market"], t["selection"], t["line"], t.get("period"))
            w = p = None
            if b and b["status"] != "open":
                it["status"] = b["status"]
            else:
                r = _settle_key(m, t["market"], t["selection"], t["line"], t.get("period"))
                if r:
                    w, p = r
                    it["status"] = status_from_fracs(w, p)
            u = _item(it["status"], t["odds"], w, p)
            it["pnl"] = None if u is None else round(u * float(t["stake"]), 2)
            it["why"] = _why(b)
        items.insert(0, it)
    return m, items


def _eur(x: float, sign: bool = True) -> str:
    t = f"{abs(x):.2f}".replace(".", ",").replace(",00", "")
    return (("+" if x >= 0 else "−") if sign else "") + t + " €"


def message(m: dict, items: list[dict]) -> str:
    score = f"{m['hg']}-{m['ag']}" if m.get("hg") is not None else ""
    head = (f"📋 <b>Repaso: {m['home_name']} {score} {m['away_name']}</b>" if m["status"] not in ("postponed", "canceled")
            else f"📋 <b>{m['home_name']} – {m['away_name']}: {'aplazado' if m['status'] == 'postponed' else 'suspendido'}</b>\nTodo lo que te enviamos queda anulado (se devuelve lo apostado).")
    lines = [head]
    done = [i for i in items if i["status"] not in ("pending", "void", "push")]
    for i in items:
        o = f" a {i['odds']:.2f}" if i.get("odds") else ""
        if i.get("at_min") and i.get("odds"):
            o = f" (a la mínima {i['odds']:.2f})"
        when = f", {i['minute']}'" if i.get("minute") is not None else ""
        st = f" · {_eur(float(i.get('stake') or 1), False)}"
        pnl = "" if i["pnl"] is None else f" → {_eur(i['pnl'])}"
        extra = f"\n   {i['actual']}" if i.get("actual") else ""
        why = f"\n   Por qué: {i['why']}" if i.get("why") and i["status"] in ("lost", "halflost", "won") else ""
        lines.append(f"{ICON.get(i['status'], '•')} {i['label']}{o}{st}{pnl} <i>({i['where']}{when})</i>{extra}{why}")
    if done:
        won = sum(1 for i in done if i["status"] in ("won", "halfwon"))
        tot = sum(i["pnl"] or 0 for i in done)
        staked = sum(float(i.get("stake") or 1) for i in done)
        lines.append(f"\n<b>Balance:</b> {won} de {len(done)} acertadas · apostado {_eur(staked, False)} · resultado <b>{_eur(tot)}</b>"
                     f" (con lo que recomendaba la web)")
    pend = sum(1 for i in items if i["status"] == "pending")
    if pend:
        lines.append(f"⏳ {pend} sin poder comprobar todavía (faltan las estadísticas finales).")
    return "\n".join(lines)


def followup_recent(hours: float = 36) -> int:
    """Cuando un partido acaba (y sus apuestas se han resuelto), manda el repaso. Si a las 8 h aún falta algún dato,
    se manda igual con lo que haya."""
    from .notify import send_telegram
    now = int(time.time())
    sent = 0
    rows = db.q("SELECT DISTINCT a.match_id AS mid FROM alerts a JOIN matches m ON m.id=a.match_id WHERE a.sent=1 AND a.kind IN (%s) "
                "AND m.status IN ('finished','postponed','canceled') AND m.kickoff BETWEEN ? AND ?" % ",".join("?" * len(KINDS)),
                (*KINDS, now - int(hours * 3600), now - 100 * 60))
    for r in rows:
        mid = r["mid"]
        if db.kv_get(f"followup:{mid}"):
            continue
        m, items = match_items(mid)
        if not items:
            db.kv_set(f"followup:{mid}", {"ts": now, "n": 0})
            continue
        pend = sum(1 for i in items if i["status"] == "pending")
        if pend and now - m["kickoff"] < 8 * 3600 and m["status"] == "finished":
            continue
        if send_telegram(message(m, items)):
            db.kv_set(f"followup:{mid}", {"ts": now, "n": len(items)})
            sent += 1
        elif not db.kv_get("telegram") and not __import__("os").environ.get("TELEGRAM_TOKEN"):
            db.kv_set(f"followup:{mid}", {"ts": now, "n": len(items), "no_tg": True})
    return sent
