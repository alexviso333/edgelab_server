"""Aprendizaje continuo y autocomprobación.

Después de cada jornada:
  - liquida solas todas las apuestas (papel y reales) con el resultado oficial,
  - calcula el CLV de cada apuesta contra el cierre de Pinnacle grabado,
  - mide la calibración del modelo (prob. predicha vs frecuencia real por tramos),
  - revisa cada estrategia en papel: si su rendimiento real se degrada (prob.
    posterior de ser rentable < 20 % con ≥50 apuestas) se retira; si confirma
    (≥150 apuestas, yield contraído > 0, prob. > 80 %) se propone para dinero real.
Cada semana: recalibra el modelo en vivo, reentrena el ensemble, relanza la
búsqueda de estrategias con los datos nuevos y genera un informe.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..core import db
from ..core.markets import Outcome, Selection, devig, settle, status_from_fracs
from ..notify import alert, send_telegram
from ..validation import stats


def _outcome(m: dict) -> Outcome | None:
    if m["hg"] is None or m["ag"] is None:
        return None
    st = {r["side"]: json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (m["id"],))}
    from ..models.secondary import stat_value
    def g(side, key):
        v = stat_value(st.get(side, {}), key)
        return v
    return Outcome(int(m["hg"]), int(m["ag"]), m.get("hg_ht"), m.get("ag_ht"),
                   corners_h=g("H", "corners"), corners_a=g("A", "corners"), cards_h=g("H", "cards"), cards_a=g("A", "cards"),
                   shots_h=g("H", "shots"), shots_a=g("A", "shots"), sot_h=g("H", "sot"), sot_a=g("A", "sot"))


def closing_fair_prob(match_id: int, sel: Selection) -> float | None:
    """Probabilidad sin margen al cierre (Pinnacle; si no, Betfair; si no, media)."""
    for bk in ("Pinnacle", "Betfair Exchange", "Avg"):
        rows = db.q("SELECT selection, line, odds, ts, phase FROM odds WHERE match_id=? AND bookmaker=? AND market=? AND period=? "
                    "AND phase IN ('close','snap','open') ORDER BY ts", (match_id, bk, sel.market, sel.period))
        if not rows:
            continue
        last: dict = {}
        for r in rows:
            last[(r["selection"], r["line"])] = r["odds"]
        if sel.market == "1X2" and all((s, None) in last for s in "HDA"):
            p = devig([last[("H", None)], last[("D", None)], last[("A", None)]])
            return p["HDA".index(sel.selection)]
        if sel.market in ("OU",) and ("O", sel.line) in last and ("U", sel.line) in last:
            p = devig([last[("O", sel.line)], last[("U", sel.line)]])
            return p[0] if sel.selection == "O" else p[1]
        if sel.market == "AH":
            other = "A" if sel.selection == "H" else "H"
            if (sel.selection, sel.line) in last and (other, -sel.line) in last:
                p = devig([last[(sel.selection, sel.line)], last[(other, -sel.line)]])
                return p[0]
    return None


def settle_open_bets() -> int:
    rows = db.q("SELECT b.*, m.hg, m.ag, m.hg_ht, m.ag_ht, m.status AS mstatus FROM bets b JOIN matches m ON m.id=b.match_id "
                "WHERE b.status='open' AND m.status='finished' AND m.hg IS NOT NULL")
    n = 0
    for b in rows:
        o = _outcome({"id": b["match_id"], "hg": b["hg"], "ag": b["ag"], "hg_ht": b["hg_ht"], "ag_ht": b["ag_ht"]})
        sel = Selection(b["market"], b["selection"], b["line"], b["period"] or "FT", b["base_h"] or 0, b["base_a"] or 0)
        try:
            w, p = settle(sel, o)
        except Exception:
            continue  # faltan datos (córners, etc.): se reintenta más tarde
        lose = 1 - w - p
        pnl = b["stake"] * (w * (b["odds"] - 1) - lose)
        clv = None
        if b["minute"] is None:
            pc = closing_fair_prob(b["match_id"], sel)
            if pc:
                clv = b["odds"] * pc - 1
        db.execute("UPDATE bets SET status=?, pnl=?, settled=?, clv=? WHERE id=?",
                   (status_from_fracs(w, p), round(pnl, 2), int(time.time()), clv, b["id"]))
        n += 1
    if n:
        db.log_job("settle", "ok", f"{n} apuestas liquidadas")
    return n


def review_paper_strategies() -> list[dict]:
    """Promoción/retirada según el rendimiento REAL en papel."""
    changes = []
    for s in db.q("SELECT id, name, status FROM strategies WHERE status IN ('paper','live')"):
        bets = pd.DataFrame(db.q("SELECT stake, pnl, odds, clv FROM bets WHERE strategy_id=? AND status NOT IN ('open','void') "
                                 "AND (group_id IS NULL OR group_id NOT LIKE 'live:%' OR 1=1)", (s["id"],)))
        if bets.empty:
            continue
        summ = stats.summarize(bets)
        from ..lab import record_test
        record_test(s["id"], "paper", summ, "live_tracking")
        new = None
        if summ["n"] >= 50 and summ["p_profitable"] < 0.2:
            new = "retired"
        elif s["status"] == "paper" and summ["n"] >= 150 and summ["shrunk"] > 0 and summ["p_profitable"] > 0.8 \
                and (summ.get("clv") is None or summ["clv"] > 0):
            new = "live"
        if new and new != s["status"]:
            db.execute("UPDATE strategies SET status=?, updated=? WHERE id=?", (new, int(time.time()), s["id"]))
            changes.append({"id": s["id"], "name": s["name"], "from": s["status"], "to": new, "summary": summ})
            alert("strategy", f"Estrategia {'retirada' if new == 'retired' else 'lista para dinero real'}: {s['name']}",
                  f"{summ['n']} apuestas en papel · yield {summ['yield']:+.1%} · contraído {summ['shrunk']:+.1%} · "
                  f"P(rentable) {summ['p_profitable']:.0%}", strategy_id=s["id"], dedupe=f"status:{s['id']}:{new}")
    return changes


def calibration_report(model: str = "prematch") -> dict:
    """Probabilidad predicha vs frecuencia real por tramos (1X2)."""
    rows = db.q("SELECT p.payload, m.hg, m.ag FROM predictions p JOIN matches m ON m.id=p.match_id "
                "WHERE p.model=? AND m.status='finished' AND m.hg IS NOT NULL", (model,))
    pts = []
    for r in rows:
        pay = json.loads(r["payload"])
        ens = pay.get("ens") or {}
        res = "H" if r["hg"] > r["ag"] else ("D" if r["hg"] == r["ag"] else "A")
        for s in "HDA":
            p = ens.get(f"ens_open_p{s}") or pay.get(f"p{s}")
            if p is not None:
                pts.append((p, 1.0 if res == s else 0.0))
    if not pts:
        return {"bins": [], "n": 0}
    a = np.array(pts)
    bins = np.linspace(0, 1, 11)
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (a[:, 0] >= lo) & (a[:, 0] < hi)
        if m.sum() >= 20:
            out.append({"bin": f"{lo:.0%}-{hi:.0%}", "pred": round(float(a[m, 0].mean()), 4),
                        "real": round(float(a[m, 1].mean()), 4), "n": int(m.sum())})
    brier = float(((a[:, 0] - a[:, 1]) ** 2).mean())
    return {"bins": out, "n": int(len(a)), "brier": round(brier, 4)}


def import_real_bets(csv_text: str) -> dict:
    """Importa tus apuestas reales (CSV). Columnas: fecha,local,visitante,mercado,seleccion,linea,cuota,stake[,resultado].
    Se casan con los partidos por equipos y fecha; las que no se encuentran se devuelven para revisarlas."""
    from ..collectors import teams
    rd = csv.DictReader(io.StringIO(csv_text))
    ok, bad = 0, []
    for r in rd:
        try:
            day = datetime.strptime(r["fecha"].strip(), "%d/%m/%Y").replace(tzinfo=timezone.utc)
            hid = teams.resolve(r["local"].strip(), "user", create=False)
            aid = teams.resolve(r["visitante"].strip(), "user", create=False)
            m = db.find_match(hid, aid, int(day.timestamp()) + 12 * 3600, window_h=30) if hid and aid else None
            if not m:
                bad.append({**r, "error": "partido no encontrado"})
                continue
            line = float(r["linea"]) if r.get("linea") not in (None, "") else None
            db.execute("INSERT INTO bets(match_id,created,mode,market,selection,line,odds,odds_source,stake,status,notes) "
                       "VALUES(?,?,?,?,?,?,?,?,?,'open',?)",
                       (m["id"], int(time.time()), "real", r["mercado"].strip().upper(), r["seleccion"].strip().upper(), line,
                        float(r["cuota"]), "user", float(r["stake"]), "importada"))
            ok += 1
        except Exception as e:
            bad.append({**r, "error": str(e)})
    settle_open_bets()
    return {"imported": ok, "rejected": bad}


def weekly_report() -> str:
    now = int(time.time())
    week = now - 7 * 86400
    b = pd.DataFrame(db.q("SELECT b.*, s.name FROM bets b LEFT JOIN strategies s ON s.id=b.strategy_id WHERE b.settled>=?", (week,)))
    lines = [f"📊 Informe semanal edgelab ({datetime.now():%d/%m/%Y})"]
    if b.empty:
        lines.append("Sin apuestas liquidadas esta semana.")
    else:
        for mode in ("paper", "real"):
            sub = b[b["mode"] == mode]
            if sub.empty:
                continue
            s = stats.summarize(sub)
            lines.append(f"{'Papel' if mode == 'paper' else 'Real'}: {s['n']} apuestas · {s['profit']:+.2f} € · yield {s['yield']:+.1%}"
                         + (f" · CLV {s['clv']:+.1%}" if s.get("clv") is not None else ""))
        top = b.groupby("name")["pnl"].sum().sort_values(ascending=False)
        if len(top):
            lines.append("Mejor: " + f"{top.index[0]} ({top.iloc[0]:+.2f} €)")
            lines.append("Peor: " + f"{top.index[-1]} ({top.iloc[-1]:+.2f} €)")
    cal = calibration_report()
    if cal.get("n"):
        lines.append(f"Calibración del modelo: Brier {cal['brier']} sobre {cal['n']} probabilidades")
    rep = db.kv_get("last_lab_report") or {}
    if rep:
        lines.append(f"Laboratorio: {rep.get('trials', 0)} variantes probadas, {rep.get('validated', 0)} validadas")
    text = "\n".join(lines)
    db.kv_set("weekly_report", {"ts": now, "text": text})
    send_telegram(text)
    return text
