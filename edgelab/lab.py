"""Laboratorio: el ciclo completo de descubrimiento y validación.

  1. Dataset de partidos con variables pre-partido (sin fuga).
  2. Ensemble walk-forward → predicciones fuera de muestra y para próximos partidos.
  3. Tabla de apuestas candidatas (pre) y cronologías (directo).
  4. Búsqueda en la ventana de ENTRENAMIENTO (todas las temporadas menos las 2 últimas):
     catálogo → rejilla → genético → árboles. Se registra cada variante probada.
  5. Las mejores se prueban UNA vez en las temporadas reservadas (fuera de muestra).
  6. Benjamini-Hochberg sobre todos los p-valores fuera de muestra + filtros de
     robustez → estado (validated / candidate / rejected).
  7. Las validadas pasan a seguimiento en papel con partidos reales.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .core import db
from .backtest import inplay as bt_live
from .backtest import prematch as bt_pre
from .models import ensemble, features
from .models.inplay import InplayParams, calibrate
from .strategies import search
from .strategies.catalog import catalog
from .strategies.dsl import Strategy
from .validation import stats


@dataclass
class LabConfig:
    oos_seasons: int = 2
    grid_per_seed: int = 60
    live_grid_per_seed: int = 60
    genetic_generations: int = 5
    genetic_size: int = 30
    top_k_oos: int = 60
    min_n_train: int = 60
    min_n_oos: int = 100
    use_mlp: bool = True


def save_strategy(s: Strategy, status: str | None = None, st: dict | None = None) -> None:
    now = int(time.time())
    ex = db.q1("SELECT status FROM strategies WHERE id=?", (s.id,))
    db.execute("INSERT INTO strategies(id,name,family,origin,parent,spec,status,stats,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET name=excluded.name, stats=COALESCE(excluded.stats, stats), "
               "status=COALESCE(?, status), updated=excluded.updated",
               (s.id, s.name, s.family, s.origin, s.parent, json.dumps(s.to_dict(), default=str),
                status or (ex["status"] if ex else "candidate"), json.dumps(st, default=str) if st else None, now, now, status))


def record_test(sid: str, split: str, summ: dict, dataset: str) -> None:
    db.execute("INSERT INTO strategy_tests(strategy_id,ts,dataset,split,n,staked,profit,yield,ci_lo,ci_hi,pvalue,clv,shrunk,price_source,extra) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, int(time.time()), dataset, split, summ.get("n", 0), summ.get("staked"), summ.get("profit"), summ.get("yield"),
                summ.get("ci_lo"), summ.get("ci_hi"), summ.get("pvalue"), summ.get("clv"), summ.get("shrunk"),
                summ.get("price_source", "real"), json.dumps({k: v for k, v in summ.items() if k in ("by_season", "p_profitable", "max_drawdown", "top1pct_share", "hit_rate", "avg_odds")})))


def fitness(summ: dict, min_n: int) -> float:
    if summ.get("n", 0) < min_n:
        return -1.0 + summ.get("n", 0) / 1e4
    return summ["shrunk"] + 0.25 * summ["shrunk_sd"] * 0  # contracción ya penaliza muestras pequeñas


def build_datasets(log=print) -> tuple[pd.DataFrame, pd.DataFrame]:
    t0 = time.time()
    ds = features.build()
    log(f"dataset: {len(ds)} partidos ({time.time() - t0:.0f}s)")
    if ds.empty:
        return ds, pd.DataFrame()
    preds = []
    for ph in ("open", "close"):
        p = ensemble.walk_forward(ds, phase=ph, use_mlp=True)
        if not p.empty:
            preds.append(p.set_index("id"))
    if preds:
        P = pd.concat(preds, axis=1)
        P = P.loc[:, ~P.columns.duplicated()]
        ds = ds.merge(P.reset_index(), on="id", how="left")
    log(f"ensemble walk-forward listo ({time.time() - t0:.0f}s)")
    store_predictions(ds)
    try:
        from .config import settings as _s
        ds.to_pickle(_s.data_dir / "dataset.pkl")
    except Exception:
        pass
    bt = bt_pre.bet_table(ds)
    log(f"tabla de apuestas: {len(bt)} filas ({time.time() - t0:.0f}s)")
    return ds, bt


def store_predictions(ds: pd.DataFrame) -> int:
    n = 0
    now = int(time.time())
    feat_cols = ["exp_goals", "rest_diff", "prev_euro_h", "prev_euro_a", "elo_diff"]
    rows = []
    for _, r in ds.iterrows():
        lh = r.get("mkt_close_lh") if pd.notna(r.get("mkt_close_lh")) else r.get("ens_open_lh")
        la = r.get("mkt_close_la") if pd.notna(r.get("mkt_close_la")) else r.get("ens_open_la")
        ph = r.get("mkt_close_H") if pd.notna(r.get("mkt_close_H")) else r.get("ens_open_pH")
        pa = r.get("mkt_close_A") if pd.notna(r.get("mkt_close_A")) else r.get("ens_open_pA")
        pdw = r.get("mkt_close_D") if pd.notna(r.get("mkt_close_D")) else r.get("ens_open_pD")
        if lh is None or pd.isna(lh) or ph is None or pd.isna(ph):
            continue
        pay = {"lh": float(lh), "la": float(la), "pH": float(ph), "pD": float(pdw), "pA": float(pa),
               "ens": {k: (float(r[k]) if pd.notna(r.get(k)) else None) for k in
                       ("ens_open_pH", "ens_open_pD", "ens_open_pA", "ens_open_lh", "ens_open_la", "ens_open_pO25")},
               "feats": {k: (float(r[k]) if k in r and pd.notna(r[k]) else None) for k in feat_cols}}
        pay["feats"]["exp_goals"] = float(lh + la)
        rows.append((int(r["id"]), now, "prematch", json.dumps(pay)))
    db.executemany("INSERT INTO predictions(match_id,created,model,payload) VALUES(?,?,?,?) "
                   "ON CONFLICT(match_id,model) DO UPDATE SET created=excluded.created, payload=excluded.payload", rows)
    return len(rows)


def calibrate_inplay(log=print) -> InplayParams:
    tls = bt_live.load_timelines()
    data = [{"lh": m.lh, "la": m.la, "events": m.events} for m in tls]
    old = InplayParams.from_dict(db.load_params("inplay"))
    p, info = calibrate(data, old)
    db.save_params("inplay", p.to_dict())
    db.save_params("inplay_info", info)
    log(f"modelo en vivo calibrado con {info.get('n')} partidos")
    return p


def run_lab(cfg: LabConfig | None = None, log=print, live_only: bool = False, pre_only: bool = False) -> dict:
    cfg = cfg or LabConfig()
    t0 = time.time()
    report: dict = {"started": int(t0)}
    ds, bt = (pd.DataFrame(), pd.DataFrame()) if live_only else build_datasets(log)
    seasons = sorted(set(bt["season"].dropna())) if not bt.empty else []
    params = calibrate_inplay(log) if not pre_only else InplayParams()
    tls = bt_live.load_timelines() if not pre_only else []
    live_seasons = sorted({m.season for m in tls})
    oos_pre = set(seasons[-cfg.oos_seasons:]) if len(seasons) > cfg.oos_seasons else set()
    oos_live = set(live_seasons[-cfg.oos_seasons:]) if len(live_seasons) > cfg.oos_seasons else set()
    bt_tr = bt[~bt["season"].isin(oos_pre)] if not bt.empty else bt
    bt_te = bt[bt["season"].isin(oos_pre)] if not bt.empty else bt
    tl_tr = [m for m in tls if m.season not in oos_live]
    tl_te = [m for m in tls if m.season in oos_live]
    pe = bt_live.PriceEngine(params)
    report.update({"seasons_pre": seasons, "oos_pre": sorted(oos_pre), "seasons_live": live_seasons,
                   "oos_live": sorted(oos_live), "n_bet_rows": int(len(bt)), "n_timelines": len(tls)})

    evaluated: dict[str, tuple[Strategy, dict]] = {}

    def eval_train(s: Strategy) -> float:
        if s.phase == "pre":
            if bt_tr.empty:
                return -1
            bets = bt_pre.run(bt_tr, s)
        else:
            if not tl_tr or s.data_needs == "A":
                return -1
            bets = bt_live.run(s, tl_tr, pe)
        summ = stats.summarize(bets, n_boot=200)
        evaluated[s.id] = (s, summ)
        record_test(s.id, "in_sample", summ, "train")
        return fitness(summ, cfg.min_n_train)

    seeds = [s for s in catalog() if (s.phase == "pre" and not live_only) or (s.phase == "live" and not pre_only)]
    pop = []
    for s in seeds:
        save_strategy(s)
        f = eval_train(s)
        pop.append((s, f))
        variants = search.grid_pre(s, cfg.grid_per_seed) if s.phase == "pre" else search.grid_live(s, cfg.live_grid_per_seed)
        for v in variants:
            pop.append((v, eval_train(v)))
    log(f"catálogo + rejilla: {len(evaluated)} variantes ({time.time() - t0:.0f}s)")
    for phase in ("pre", "live"):
        ph_pop = [(s, f) for s, f in pop if s.phase == phase and f > -1]
        if len(ph_pop) >= 6:
            search.evolve(ph_pop, eval_train, generations=cfg.genetic_generations, size=cfg.genetic_size, seed=len(evaluated))
    log(f"genético: {len(evaluated)} variantes ({time.time() - t0:.0f}s)")
    if not bt_tr.empty:
        for mk in ("1X2", "OU", "AH"):
            for s in search.mine_rules(bt_tr, mk):
                eval_train(s)
    log(f"árboles: {len(evaluated)} variantes en total ({time.time() - t0:.0f}s)")
    if not bt_tr.empty:
        from .strategies.cross import factor_grid, grid_to_strategies
        g_tr = factor_grid(bt_tr)
        for s in grid_to_strategies(g_tr):
            eval_train(s)
        db.kv_set("factor_grid", [{k: v for k, v in g.items()} for g in factor_grid(bt)[:80]])
        log(f"cruce de factores: {len(g_tr)} pares × mercados analizados ({time.time() - t0:.0f}s)")

    # ---------- fuera de muestra: solo las mejores de cada familia ----------
    ranked = sorted(evaluated.values(), key=lambda x: -fitness(x[1], cfg.min_n_train))
    chosen, per_family = [], {}
    for s, summ in ranked:
        if summ.get("n", 0) < cfg.min_n_train:
            continue
        fam = s.family.split("+")[0]
        if per_family.get(fam, 0) >= 4:
            continue
        per_family[fam] = per_family.get(fam, 0) + 1
        chosen.append(s)
        if len(chosen) >= cfg.top_k_oos:
            break
    # las semillas del catálogo siempre se miden fuera de muestra (para tu panel)
    for s in seeds:
        if s.id not in {c.id for c in chosen}:
            chosen.append(s)
    oos_results = []
    for s in chosen:
        if s.phase == "pre":
            bets = bt_pre.run(bt_te, s) if not bt_te.empty else pd.DataFrame()
        else:
            bets = bt_live.run(s, tl_te, pe) if tl_te else pd.DataFrame()
        summ = stats.summarize(bets)
        record_test(s.id, "oos", summ, "holdout")
        oos_results.append((s, summ, evaluated.get(s.id, (s, {}))[1]))
    pv = [r[1].get("pvalue", 1.0) if r[1].get("n", 0) > 2 else 1.0 for r in oos_results]
    bh = stats.benjamini_hochberg(pv, 0.10)
    adj = stats.bh_adjusted(pv)
    summary_rows = []
    for (s, oos, ins), ok, padj in zip(oos_results, bh, adj):
        verdict, reasons = stats.promotion_verdict(oos, ins, ok, cfg.min_n_oos)
        if s.control and verdict == "validated":
            reasons.append("¡ALERTA! un control negativo ha pasado: revisar datos/fugas")
            verdict = "rejected"
        st_payload = {"in_sample": ins, "oos": oos, "bh_ok": ok, "p_adj": padj, "reasons": reasons,
                      "description": s.describe(), "hypothesis": s.hypothesis, "data_needs": s.data_needs,
                      "control": s.control, "trials_total": len(evaluated)}
        prev = db.q1("SELECT status FROM strategies WHERE id=?", (s.id,))
        prev_status = prev["status"] if prev else None
        if verdict == "validated":
            status = prev_status if prev_status in ("paper", "live") else "paper"
        elif prev_status in ("paper", "live"):
            status = prev_status if verdict == "candidate" else "retired"
        else:
            status = verdict
        save_strategy(s, status, st_payload)
        summary_rows.append({"id": s.id, "name": s.name, "phase": s.phase, "verdict": verdict,
                             "is_yield": ins.get("yield"), "is_n": ins.get("n"),
                             "oos_yield": oos.get("yield"), "oos_n": oos.get("n"), "oos_shrunk": oos.get("shrunk"),
                             "p_adj": padj, "clv": oos.get("clv"), "price_source": oos.get("price_source")})
    report.update({"trials": len(evaluated), "oos_tested": len(oos_results),
                   "validated": sum(1 for r in summary_rows if r["verdict"] == "validated"),
                   "seconds": int(time.time() - t0), "table": summary_rows})
    db.kv_set("last_lab_report", report)
    db.log_job("lab", "ok", f"{report['trials']} variantes, {report['validated']} validadas")
    log(f"laboratorio terminado: {report['trials']} variantes probadas, {report['validated']} validadas ({report['seconds']}s)")
    return report
