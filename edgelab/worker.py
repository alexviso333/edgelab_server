"""Planificador: lo hace todo solo. Tres hilos para que nada pesado frene el directo.

  directo   cada 30 s con partidos en juego (60 s si no): marcador, incidencias, xG,
            cuotas en vivo de Sofascore, señales y coberturas
  rápido    al arrancar: calendario + cuotas + alineaciones de los próximos días
            cada 20 min cuotas pre-partido (más a menudo cerca del saque) y respaldo de Sofascore
            cada 15 min alineaciones · cada 30 min resultados y liquidación
            cada 6 h calendario de 7 días · cronologías de partidos jugados en tandas
            cada 45 min mapas de calor de los últimos partidos de quien juega pronto + entrenadores
            y "Sincronizar ahora" desde la web
  pesado    cada 3 h predicciones de los próximos partidos (si hay nuevos o cuotas nuevas)
            diario 06:10 stats de equipos, jugadores, dataset, ensemble, picks
            lunes 04:30 recalibrar el modelo en vivo + laboratorio completo + informe semanal
"""
from __future__ import annotations

import threading
import os
import time
import traceback
from datetime import datetime, timezone

from .core import db
from .learning import loop as learn
from .live import engine


def _run(name: str, fn) -> None:
    t0 = time.time()
    db.kv_set("worker_doing", {"job": name, "since": int(t0)})
    try:
        fn()
    except Exception as e:
        db.log_job(name, "error", f"{e}\n{traceback.format_exc()[-1500:]}")
    finally:
        dt = time.time() - t0
        if dt > 30:
            db.log_job(name, "ok", f"{dt:.0f}s")
        db.kv_set("worker_doing", None)


class Every:
    def __init__(self, seconds: float, fn, name: str, first_delay: float = 0, background: bool = False):
        self.s, self.fn, self.name, self.bg = seconds, fn, name, background
        self.next = time.time() + first_delay
        self.th: threading.Thread | None = None

    def due(self) -> bool:
        return time.time() >= self.next and not (self.th and self.th.is_alive())

    def run(self) -> None:
        self.next = time.time() + self.s
        if self.bg:      # lo lento (mapas de calor, balón parado, noticias) en su propio hilo: no frena las cuotas
            self.th = threading.Thread(target=_run, args=(self.name, self.fn), daemon=True, name=self.name)
            self.th.start()
        else:
            _run(self.name, self.fn)


def digest() -> None:
    """Por la mañana: resumen de las recomendaciones oficiales de hoy (cada una ya llegó sola al salir; aquí van
    juntas con su cuota actual). No se apuntan aparte: el repaso las resuelve desde las recomendaciones."""
    from .notify import send_telegram
    sent = db.kv_get("tips_sent") or {}
    now = time.time()
    today = [s for s in sent.values() if s.get("state") == "active" and now < s["kickoff"] < now + 20 * 3600]
    if not today:
        return True
    today.sort(key=lambda s: s["kickoff"])
    from .tipster import card
    tot = sum(float(s.get("stake") or 0) for s in today)
    lines = [f"<b>☀️ RECOMENDADAS PARA HOY</b> ({len(today)})", f"💰 En total: <b>{tot:g} €</b>", ""]
    for s in today:
        m = db.q1("SELECT home_name, away_name FROM matches WHERE id=?", (s["mid"],)) or {}
        k10 = max(0, min(10, round((s.get("p") or 0) * 10)))
        lines.append(card([f"🕘 {time.strftime('%H:%M', time.localtime(s['kickoff']))}  ⚽ {m.get('home_name')} – {m.get('away_name')}",
                           f"✅ <b>{s['label']}</b>",
                           f"💶 <b>{s['odds']:.2f}</b>  ·  apuesta <b>{s['stake']:g} €</b>" + (f"  ·  🎲 <b>{k10}/10</b>" if s.get("p") else "")]))
    return send_telegram("\n".join(lines)[:3990])


def daily():
    from .lab import build_datasets
    try:
        from .review import weekly_review
        weekly_review(days=10, log=lambda s: db.log_job("review", "ok", s))
    except Exception as e:
        db.log_job("review", "error", str(e))
    engine.backfill_team_stats(limit=12)
    engine.backfill_players(limit=15)
    build_datasets(log=lambda s: None)
    engine.record_prematch_odds(force=False)
    engine.prematch_picks()


def weekly():
    from .lab import LabConfig, run_lab
    run_lab(LabConfig(), log=lambda s: db.log_job("lab", "info", s))
    learn.review_paper_strategies()
    learn.weekly_report()


def results():
    from .plans import settle_raw_bets, tip_calibration, tips_clv
    from .postmatch import finish_recent
    engine.refresh_schedule(days_back=2, days_ahead=0)
    try:
        engine.fix_postponed()          # aplazados/suspendidos que alguna fuente dio por terminados: apuestas anuladas
    except Exception as e:
        db.log_job("results", "error", f"aplazados: {e}")
    finish_recent()
    learn.settle_open_bets()
    settle_raw_bets()
    tips_clv()
    tip_calibration()
    learn.review_paper_strategies()


def tactics() -> None:
    """Mapas de calor de los últimos partidos de quien juega pronto + entrenadores (para los análisis de cada apuesta)."""
    from .collectors import tactics as tc
    if db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"]:
        return      # con partidos en juego, Sofascore se reserva para el directo (si no, nos frena)
    tc.collect_managers()
    tc.collect_heatmaps(max_calls=120)
    tc.collect_set_pieces(max_calls=60)
    # partidos de todas las competiciones (liga incluida) de quien juega pronto: rachas y once habitual con contexto
    now = int(time.time())
    if time.time() - float(db.kv_get("last_games_ts", 0) or 0) > 12 * 3600:
        from .collectors import players as pl
        tids = {t for r in db.q("SELECT home_id, away_id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?",
                                (now, now + 96 * 3600)) for t in (r["home_id"], r["away_id"]) if t and t > 0}
        for t in tids:
            try:
                pl.store_last_games(pl.parse_last_games(engine.shub().team_last_games(t, 1, 40, ttl=6 * 3600)))
            except Exception as e:
                db.log_job("tactics", "error", f"last-games {t}: {e}")
        db.kv_set("last_games_ts", int(time.time()))


def news_job() -> None:
    from .collectors.news import refresh_upcoming
    refresh_upcoming(48)


def snapshot_job() -> None:
    from .postmatch import snapshot_upcoming
    snapshot_upcoming(45)


def lineup_watch() -> None:
    """Cada 5 min: si cambian las bajas publicadas, una noticia da a alguien como baja o sale/cambia el once (previsto o
    confirmado) de un partido de las próximas 48 h, se recalcula ESE partido al momento (sin esperar a las 3 h del
    modelo) y se revisan las recomendaciones ya enviadas (si ya no tienen valor, llega el «ya no la recomendamos»)."""
    from .absences import signature, team_ratios
    from .collectors import news as nw
    from .plans import analyze, tip_alerts
    now = int(time.time())
    changed = []
    for m in db.q("SELECT id, kickoff FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now + 300, now + 48 * 3600)):
        mid = m["id"]
        if m["kickoff"] - now < 6 * 3600:          # cerca del partido, las noticias cada hora
            cur = db.kv_get(f"news:{mid}") or {}
            if now - (cur.get("ts") or 0) > 3600:
                try:
                    nw.refresh(mid, force=True)
                except Exception:
                    pass
        # bajas de FotMob (segunda fuente): cada 2 h, cada hora en las 6 últimas
        if now - int(db.kv_get(f"fm_missing_ts:{mid}") or 0) > (3600 if m["kickoff"] - now < 6 * 3600 else 7200):
            db.kv_set(f"fm_missing_ts:{mid}", now)
            try:
                from .collectors.fotmob import merge_missing
                merge_missing(mid)
            except Exception as e:
                db.log_job("lineups", "error", f"bajas FotMob {mid}: {e}")
        sig = signature(mid)
        old = db.kv_get(f"abs_sig:{mid}")
        if old == sig:
            continue
        db.kv_set(f"abs_sig:{mid}", sig)
        if old is None:
            continue                               # primera vez: solo se apunta
        try:
            team_ratios(mid, fresh=True)
            analyze(mid, use_cache=False)
            changed.append(mid)
        except Exception as e:
            db.log_job("lineups", "error", f"recalcular {mid}: {e}")
    if changed:
        db.log_job("lineups", "ok", f"{len(changed)} partidos recalculados por cambios de once/bajas/noticias")
        try:
            from .plans import refresh_verdicts
            refresh_verdicts()
            tip_alerts(fast=set(changed))
        except Exception as e:
            db.log_job("lineups", "error", f"recomendaciones: {e}")


def sources_watch() -> None:
    """C6: si una fuente lleva más de 30 min fallando, aviso (una vez cada 6 h por fuente) y queda en el registro.
    Así nada vuelve a fallar en silencio, como pasó con Telegram."""
    from .notify import send_telegram
    now = time.time()
    bad = []
    last_odds = db.q1("SELECT MAX(ts) AS t FROM jobs_log WHERE job='odds' AND status='ok'")["t"] or 0
    if db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + 86400))["n"] \
            and now - last_odds > 45 * 60:
        bad.append(("statshub", f"Statshub: no se descargan cuotas desde hace {int((now - last_odds) / 60)} min"))
    try:
        from .collectors.sofascore import paused_left
        if paused_left() > 30 * 60:
            bad.append(("sofascore", f"Sofascore en pausa {int(paused_left() / 60)} min más (las cuotas en directo salen de Oddspedia)"))
    except Exception:
        pass
    for name, mod in (("fotmob", "fotmob"), ("oddspedia", "oddspedia"), ("365scores", "s365")):
        try:
            m_ = __import__(f"edgelab.collectors.{mod}", fromlist=["x"])
            fs = getattr(m_, "_FAIL", {})
            if fs.get("since") and now - fs["since"] > 30 * 60:
                bad.append((name, f"{name}: falla desde hace {int((now - fs['since']) / 60)} min"))
        except Exception:
            pass
    tg = db.kv_get("tg_last_error") or {}
    if tg.get("ts") and now - tg["ts"] < 15 * 60:
        db.log_job("fuentes", "error", f"Telegram: {tg.get('msg')}")
    db.kv_set("sources_status", {"ts": int(now), "bad": [b[1] for b in bad]})
    for name, msg in bad:
        db.log_job("fuentes", "error", msg)
        k = f"src_alert:{name}"
        if now - float(db.kv_get(k) or 0) > 6 * 3600:
            db.kv_set(k, now)
            send_telegram(f"⚠️ <b>Fuente con problemas</b>\n{msg}\nLa web sigue con las demás fuentes; te aviso si dura.")


def extras_job() -> None:
    """Remates de cabeza y tiros desde fuera del área por jugador (Statshub), primero de quien juega pronto."""
    if db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"]:
        return
    from .collectors.players import backfill_extra
    n = backfill_extra(40)
    if n:
        db.log_job("extras", "ok", f"{n} filas de cabeza/fuera del área")


def finish_job() -> None:
    from .postmatch import finish_recent
    finish_recent()
    try:
        from .followup import followup_recent
        followup_recent()            # repaso por Telegram de todo lo que se avisó de cada partido ya acabado
    except Exception as e:
        db.log_job("followup", "error", str(e))


def verdicts_job() -> None:
    from .plans import migrate_tips, record_official, refresh_verdicts
    migrate_tips()
    refresh_verdicts()
    record_official()
    try:
        from .plans import tip_alerts
        tip_alerts()              # Telegram: las recomendaciones oficiales nuevas, subidas de cuota y retiradas
    except Exception as e:
        db.log_job("verdicts", "error", f"avisos de recomendaciones: {e}")
    from .plans import refresh_ladders
    refresh_ladders()


def light_reports(hours: float = 48) -> None:
    """Modo ligero: el planificador prepara los informes (que necesitan el dataset grande) y libera la memoria."""
    if os.environ.get("EDGELAB_LIGHT") != "1":
        return
    import gc
    from . import analysis
    now = int(time.time())
    for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + int(hours * 3600))):
        try:
            analysis.report(r["id"])
        except Exception:
            pass
    analysis._DS.clear()
    gc.collect()


def analyze_upcoming(hours: float = 168, start_h: float = 0) -> None:
    """Precalcula mercados, planes y combinadas de los próximos partidos (para la página Oportunidades)."""
    from .plans import analyze, record_tips, tip_calibration
    try:
        tip_calibration()          # lo aprendido, al día antes de analizar
    except Exception:
        pass
    now = int(time.time())
    for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? ORDER BY kickoff",
                  (now + int(start_h * 3600), now + int(hours * 3600))):
        try:
            analyze(r["id"])
            record_tips(r["id"])
            new_market_alerts(r["id"])
        except Exception as e:
            db.log_job("analysis", "error", f"{r['id']}: {e}")


def _pstake(v: dict) -> float:
    from .followup import page_stake
    return page_stake(v.get("p"), v.get("b365"), v.get("group") == "jugadores", v.get("kf"))


def new_market_alerts(mid: int) -> int:
    """Aviso cuando ABRE un mercado (cuotas de bet365 que no estaban antes) y ya de salida tiene valor: al abrir, las
    cuotas suelen estar desajustadas. Solo avisa si hay valor; la primera vez que se ve un partido se apunta sin avisar
    salvo que sus cuotas acaben de aparecer."""
    from .plans import vkey
    from .notify import alert
    c = db.kv_get(f"analysis:{mid}") or {}
    vals = c.get("values") or []
    seen_key = f"mk_seen:{mid}"
    seen = db.kv_get(seen_key)
    now = int(time.time())
    if seen is None:
        first = db.q1("SELECT MIN(ts) AS t FROM odds WHERE match_id=? AND bookmaker='Bet365'", (mid,)) or {}
        fresh = first.get("t") and now - int(first["t"]) < 3 * 3600      # el partido acaba de salir con cuotas
        seen = {"keys": [], "ts": now}
        if not fresh:
            # primera vez que lo miramos y ya tenía cuotas de antes: se apunta todo sin avisar
            seen["keys"] = sorted({"|".join(map(str, (v["market"], v.get("period") or "FT", v["selection"], v.get("line")))) for v in
                                   _all_b365_keys(mid) + vals})
            db.kv_set(seen_key, seen)
            return 0
    keys = set(seen.get("keys") or [])
    cur = _all_b365_keys(mid)
    cur_keys = {"|".join(map(str, (v["market"], v.get("period") or "FT", v["selection"], v.get("line")))) for v in cur}
    new_vals = [v for v in vals if "|".join(map(str, (v["market"], v.get("period") or "FT", v["selection"], v.get("line")))) not in keys]
    n = 0
    if new_vals:
        try:
            from .plans import record_opening
            record_opening(mid, new_vals)          # se apuntan a la cuota de la apertura para saber si estas salen rentables
        except Exception as e:
            db.log_job("analysis", "error", f"apertura {mid}: {e}")
        m = db.q1("SELECT home_name, away_name, kickoff FROM matches WHERE id=?", (mid,)) or {}
        new_vals.sort(key=lambda v: -(v.get("ev") or 0))
        ko = time.strftime("%d/%m %H:%M", time.localtime(m.get("kickoff") or 0))
        # TODAS las que tienen valor (antes solo las 5 mejores); si son muchas, en varios mensajes de 25
        dd0 = f"mk:{mid}:" + vkey(mid, new_vals[0]["market"], new_vals[0].get("period"), new_vals[0]["selection"], new_vals[0].get("line"))
        parts = [new_vals[i:i + 6] for i in range(0, len(new_vals), 6)]
        from .tipster import _e, card, factors, fold, short, ten_dots
        for j, part in enumerate(parts):
            full: list[str] = []

            def _ln(v):
                vv = {**v, "match": {"id": mid, **m}}
                try:
                    pro, con = factors(vv, 6, 3)
                except Exception:
                    pro, con = [], []
                rk = "🟢 bajo" if v["p"] >= 0.62 else "🟡 medio" if v["p"] >= 0.45 else "🔴 alto"
                k10 = max(0, min(10, round(v["p"] * 10)))
                st = _pstake(v)
                full.extend([f"<b>{_e(v['label'])}</b>"] + [f"👍 {_e(t)}" for t in pro] + [f"👎 {_e(t)}" for t in con] + [""])
                return card([f"✅ <b>{_e(v['label'])}</b>",
                             f"💶 <b>{v['b365']:.2f}</b>  ·  apuesta <b>{st:g} €</b>  ·  valor <b>+{round(v['ev'] * 100)} %</b>",
                             f"🎲 {ten_dots(v['p'])}  <b>{k10}/10</b>  ·  riesgo {rk}"]
                            + ([f"👍 {_e(short(pro[0]))}"] if pro else []))
            lines = "\n".join(_ln(v) for v in part)
            if any(full):
                lines += "\n" + fold(full, limit=max(600, 3700 - len(lines)))
            alert("mercado", f"Mercado nuevo con valor: {m.get('home_name')} – {m.get('away_name')}"
                  + (f" ({j + 1}/{len(parts)})" if len(parts) > 1 else ""),
                  (f"{ko}\nAcaban de salir estas cuotas en bet365 y ya pagan más de lo que deberían:\n" if j == 0 else "")
                  + lines + ("\nCompruébalas rápido: al abrir se ajustan pronto." if j == len(parts) - 1 else ""),
                  match_id=mid, payload={"n": len(new_vals), "items": [
                      {**{k: v.get(k) for k in ("label", "b365", "p", "ev", "market", "selection", "line", "period", "group", "kf")},
                       "stake_eur": _pstake(v)} for v in part]},
                  dedupe=dd0 + (f":{j}" if j else ""))
        n = len(new_vals)
    val_keys = {"|".join(map(str, (v["market"], v.get("period") or "FT", v["selection"], v.get("line")))) for v in vals}
    db.kv_set(seen_key, {"keys": sorted(keys | cur_keys | val_keys), "ts": now})
    return n


def _all_b365_keys(mid: int) -> list[dict]:
    rows = db.q("SELECT DISTINCT market, period, selection, line FROM odds_now WHERE match_id=? AND bookmaker='Bet365'", (mid,))
    return [{"market": r["market"], "period": r["period"] or "FT", "selection": r["selection"],
             "line": None if r["line"] is None else round(float(r["line"]), 2)} for r in rows]


def initial_sync(log=lambda s: db.log_job("sync", "info", s)) -> None:
    """Lo mínimo para que la web tenga algo que enseñar: calendario, cuotas y onces."""
    if not db.kv_get("odds_now_built"):
        log("preparando la tabla rápida de cuotas (solo la primera vez)…")
        db.ensure_odds_now()
    log("calendario…")
    engine.refresh_schedule(1, 7)
    try:
        n = engine.fix_postponed()
        if n:
            log(f"{n} partidos aplazados/suspendidos: sus apuestas, anuladas")
    except Exception as e:
        db.log_job("sync", "error", f"aplazados: {e}")
    if db.kv_get("last_sync") and int(time.time()) - int(db.kv_get("last_sync")) < 6 * 3600:
        log("análisis rápido con las cuotas que ya hay…")   # la web tiene recomendaciones en segundos tras reiniciar
        analyze_upcoming()
    try:
        from .postmatch import finish_recent
        log("cerrando partidos acabados mientras estaba apagado…")
        finish_recent(24 * 7)
    except Exception as e:
        log(f"cierre de partidos: {e}")
    log("cuotas Statshub (7 días)…")
    engine.record_prematch_odds(hours_ahead=168, force=False)   # respeta el ritmo normal: no repite lo recién descargado
    log("cuotas Sofascore de respaldo…")
    engine.sofascore_prematch_odds(hours_ahead=72)
    log("alineaciones…")
    engine.refresh_lineups(hours_ahead=48)
    log("picks…")
    engine.prematch_picks()
    log("análisis de mercados y planes…")
    analyze_upcoming(72 if os.environ.get("EDGELAB_LIGHT") == "1" else 168)
    db.kv_set("last_sync", int(time.time()))
    log("listo")


def live_loop(stop: threading.Event) -> None:
    tracker = engine.LiveTracker()
    while not stop.is_set():
        try:
            tracker.tick()
            db.kv_set("live_heartbeat", int(time.time()))
        except Exception as e:
            db.log_job("live", "error", f"{e}\n{traceback.format_exc()[-800:]}")
        live = db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"]
        stop.wait(30 if live else 60)


def fast_loop(stop: threading.Event) -> None:
    _run("sync", initial_sync)
    jobs = [
        Every(6 * 3600, lambda: engine.refresh_schedule(1, 7), "schedule", first_delay=6 * 3600),
        Every(5 * 60, engine.record_prematch_odds, "odds", first_delay=5 * 60),
        Every(40 * 60, lambda: engine.sofascore_prematch_odds(72), "odds_sofa", first_delay=5 * 60, background=True),
        Every(30 * 60, results, "results", first_delay=60),
        Every(10 * 60, engine.refresh_lineups, "lineups", first_delay=10 * 60),
        Every(30 * 60, lambda: (None if db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"]
                                else engine.backfill_timelines(limit=15)), "timelines", first_delay=600, background=True),
        # todo lo que tenga cuotas en los próximos 7 días (los mercados abren con días de antelación). En el servidor
        # pequeño: lo cercano cada hora y lo lejano cada 3 horas
        Every((60 if os.environ.get("EDGELAB_LIGHT") == "1" else 15) * 60,
              (lambda: analyze_upcoming(72)) if os.environ.get("EDGELAB_LIGHT") == "1" else analyze_upcoming, "analysis", first_delay=150),
        *([Every(3 * 3600, lambda: analyze_upcoming(168, 72), "analysis_far", first_delay=2400)] if os.environ.get("EDGELAB_LIGHT") == "1" else []),
        Every(20 * 60, verdicts_job, "verdicts", first_delay=60, background=True),
        Every(3 * 3600, light_reports, "reports", first_delay=1800),
        Every(10 * 60, snapshot_job, "prematch_snapshot", first_delay=30),
        Every(10 * 60, finish_job, "postmatch", first_delay=45, background=True),
        Every((180 if os.environ.get("EDGELAB_LIGHT") == "1" else 45) * 60, tactics, "tactics", first_delay=900, background=True),
        Every(3 * 3600, news_job, "news", first_delay=300, background=True),
        Every(10 * 60, extras_job, "extras", first_delay=600, background=True),
        Every(5 * 60, lineup_watch, "lineup_watch", first_delay=420, background=True),
        Every(10 * 60, sources_watch, "fuentes", first_delay=900, background=True),
        Every(60, lambda: __import__("edgelab.steam", fromlist=["x"]).lineup_sprint(), "lineup_sprint", first_delay=300, background=True),
    ]
    while not stop.is_set():
        if db.kv_get("sync_request"):
            db.kv_set("sync_request", None)
            _run("sync", initial_sync)
        for j in jobs:
            if j.due() and not stop.is_set():
                j.run()
        stop.wait(10)


def refresh_predictions() -> None:
    """Rehace el dataset y las predicciones si hay partidos próximos sin predicción o cuotas nuevas."""
    from .lab import build_datasets
    now = int(time.time())
    last = db.kv_get("last_predict", 0) or 0
    missing = db.q1("SELECT COUNT(*) AS n FROM matches m WHERE status='notstarted' AND kickoff BETWEEN ? AND ? "
                    "AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.match_id=m.id AND p.model='prematch')",
                    (now, now + 72 * 3600))["n"]
    new_odds = db.q1("SELECT COUNT(*) AS n FROM odds_now WHERE ts>?", (last,))["n"]
    if not missing and new_odds < 200:
        return
    build_datasets(log=lambda s: None)
    db.kv_set("last_predict", now)
    try:          # con qué once y bajas se han hecho estas predicciones (para no contarlas dos veces después)
        from .absences import snapshot_for_predictions
        snapshot_for_predictions([r["match_id"] for r in db.q(
            "SELECT p.match_id FROM predictions p JOIN matches m ON m.id=p.match_id WHERE p.model='prematch' "
            "AND m.status='notstarted' AND m.kickoff BETWEEN ? AND ?", (now, now + 7 * 86400))])
    except Exception as e:
        db.log_job("predict", "error", f"bajas: {e}")
    engine.prematch_picks()


def heavy_loop(stop: threading.Event) -> None:
    stop.wait(240)   # deja que lo rápido termine primero
    last_pred = float(db.kv_get("last_predict", 0) or 0)   # reiniciar no obliga a rehacer el modelo (tarda ~15 min)
    while not stop.is_set():
        light = os.environ.get("EDGELAB_LIGHT") == "1"      # servidores con poca memoria (Render gratis: 512 MB)
        hour = datetime.now(timezone.utc).astimezone().hour
        # en el servidor pequeño no se rehace el modelo (carga años de partidos y tumbaba la web); las cuotas y los
        # análisis siguen al día con el modelo que había y los goles esperados que dan las casas
        if not light and time.time() - last_pred > 3 * 3600:
            last_pred = time.time()
            _run("predict", refresh_predictions)
        local = datetime.now(timezone.utc).astimezone()
        day = local.strftime("%Y-%m-%d")
        if local.hour >= 6 and db.kv_get("last_daily", "") != day:
            db.kv_set("last_daily", day)
            _run("daily", daily)
        # resumen de las recomendadas del día por Telegram a las 9:30 (hora de España, también en Render); si el
        # ordenador se enciende más tarde, llega al encenderlo (hasta las 21:00)
        try:
            from zoneinfo import ZoneInfo
            mad = datetime.now(ZoneInfo("Europe/Madrid"))
        except Exception:
            mad = local
        mday = mad.strftime("%Y-%m-%d")
        if (mad.hour, mad.minute) >= (9, 30) and mad.hour < 21 and db.kv_get("last_digest2", "") != mday \
                and time.time() - float(db.kv_get("digest_try", 0) or 0) > 900:
            db.kv_set("digest_try", time.time())
            try:
                if digest():
                    db.kv_set("last_digest2", mday)       # si Telegram falla, se reintenta cada 15 min
            except Exception as e:
                db.log_job("daily", "error", f"resumen Telegram: {e}")
        week = local.strftime("%G-%V")
        if not light and local.weekday() == 0 and local.hour >= 4 and db.kv_get("last_weekly", "") != week:
            db.kv_set("last_weekly", week)
            _run("weekly", weekly)
        if db.kv_get("lab_request"):
            db.kv_set("lab_request", None)
            _run("weekly", weekly)
        stop.wait(60)


def main() -> None:
    if os.environ.get("EDGELAB_LIGHT") == "1":
        try:
            os.nice(19)        # servidor pequeño: la web va primero; el planificador usa la CPU que sobre
        except Exception:
            pass
    db.log_job("worker", "start", "")
    db.kv_set("worker_started", int(time.time()))
    stop = threading.Event()
    threads = [threading.Thread(target=f, args=(stop,), name=n, daemon=True)
               for n, f in (("live", live_loop), ("fast", fast_loop), ("heavy", heavy_loop))]
    for t in threads:
        t.start()
    try:
        while True:
            db.kv_set("worker_heartbeat", int(time.time()))
            for t in threads:
                if not t.is_alive():
                    db.log_job("worker", "error", f"hilo {t.name} caído")
            time.sleep(30)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
