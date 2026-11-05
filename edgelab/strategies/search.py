"""Generación de estrategias nuevas en tres capas:

1. Rejilla: variantes sistemáticas de cada semilla del catálogo (minutos,
   marcadores, bandas de cuota, ventaja mínima, competición, cobertura).
2. Algoritmo genético: cruza las mejores (disparador de una + salida de otra),
   muta umbrales y añade/quita filtros.
3. Minería con árboles de decisión: busca zonas donde el resultado real se
   separa del precio y las convierte en reglas legibles (nuevas estrategias
   que nadie ha escrito).
"""
from __future__ import annotations

import copy
import itertools
import random
from typing import Callable

import numpy as np
import pandas as pd

from .dsl import Strategy

LEAGUES = ["EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1"]
ODDS_BANDS = [(1.01, 1.6), (1.6, 2.2), (2.2, 3.2), (3.2, 5.0), (5.0, 12.0), (None, None)]
EDGES = [None, 0.0, 0.02, 0.04, 0.06, 0.08]


def grid_pre(seed: Strategy, max_variants: int = 120) -> list[Strategy]:
    out = []
    comps_opts = [seed.context.get("comps")] + [[c] for c in LEAGUES]
    for comps, band, edge in itertools.product(comps_opts, ODDS_BANDS, EDGES):
        s = seed.clone(origin="grid", parent=seed.id)
        if comps:
            s.context = {**s.context, "comps": comps}
        if band[0]:
            s.price = {**s.price, "min_odds": band[0], "max_odds": band[1]}
        if edge is not None:
            s.model_filter = {**s.model_filter, "min_edge": edge, "prob": s.model_filter.get("prob", "ens")}
        elif "min_edge" in s.model_filter and seed.model_filter:
            pass
        s.name = f"{seed.family} · var {_short(s)}"
        out.append(s)
    random.Random(seed.id).shuffle(out)
    return _dedupe(out)[:max_variants]


def grid_live(seed: Strategy, max_variants: int = 150) -> list[Strategy]:
    out = []
    lo, hi = seed.trigger.get("minute", [60, 85])
    minutes = sorted({(m, m) for m in range(max(1, lo - 15), min(89, hi + 12) + 1, 3)} | {(lo, hi)})
    exits = [seed.exit, {"type": "hold"}]
    if seed.market.get("market") == "1X2":
        exits += [{"type": "hedge", "on": "equalizer", "hedge": {"market": "AH", "selection": "original_leader", "line": 0.0}, "size": z}
                  for z in ("zero", "balance", "half")]
        exits += [{"type": "greenup", "on": "goal_for", "size": "balance"}]
    favs = [None, [0.5, 0.95], [0.3, 0.5]]
    min_odds = [None, 2.0, 4.0, 8.0]
    for (a, b), ex, fp, mo in itertools.product(minutes, exits, favs, min_odds):
        s = seed.clone(origin="grid", parent=seed.id)
        s.trigger = {**s.trigger, "minute": [a, b]}
        s.exit = copy.deepcopy(ex)
        if fp:
            s.context = {**s.context, "fav_prob": fp}
        if mo:
            s.price = {**s.price, "min_odds": mo}
        s.name = f"{seed.family} · var {_short(s)}"
        out.append(s)
    random.Random(seed.id).shuffle(out)
    return _dedupe(out)[:max_variants]


def _short(s: Strategy) -> str:
    return s.id[:6]


def _dedupe(lst: list[Strategy]) -> list[Strategy]:
    seen, out = set(), []
    for s in lst:
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Genético
# ---------------------------------------------------------------------------
def mutate(s: Strategy, rng: random.Random) -> Strategy:
    c = s.clone(origin="genetic", parent=s.id)
    choice = rng.random()
    if c.phase == "live":
        if choice < 0.35:
            lo, hi = c.trigger.get("minute", [60, 80])
            d = rng.choice([-6, -3, 3, 6])
            lo = int(np.clip(lo + d, 1, 89))
            hi = int(np.clip(max(lo, hi + d), lo, 89))
            c.trigger = {**c.trigger, "minute": [lo, hi]}
        elif choice < 0.55:
            c.price = {**c.price, "min_odds": rng.choice([None, 1.5, 2.0, 3.0, 5.0, 8.0])}
        elif choice < 0.75:
            c.context = {**c.context, "fav_prob": rng.choice([None, [0.5, 0.65], [0.65, 0.95], [0.35, 0.5]])}
        else:
            c.exit = rng.choice([{"type": "hold"},
                                 {"type": "hedge", "on": "equalizer", "hedge": {"market": "AH", "selection": "original_leader", "line": 0.0}, "size": rng.choice(["zero", "balance", "half"])},
                                 {"type": "greenup", "on": "goal_for", "size": "balance"}])
    else:
        if choice < 0.35:
            e = c.model_filter.get("min_edge")
            c.model_filter = {**c.model_filter, "min_edge": float(np.clip((e or 0.02) + rng.choice([-0.02, -0.01, 0.01, 0.02]), -0.02, 0.15)),
                              "prob": c.model_filter.get("prob", "ens")}
        elif choice < 0.65:
            band = rng.choice(ODDS_BANDS)
            c.price = {**c.price, "min_odds": band[0], "max_odds": band[1]}
        else:
            c.context = {**c.context, "comps": rng.choice([None] + [[l] for l in LEAGUES])}
    c.name = f"{s.family} · gen {c.id[:6]}"
    return c


def crossover(a: Strategy, b: Strategy, rng: random.Random) -> Strategy:
    if a.phase != b.phase:
        return mutate(a, rng)
    c = a.clone(origin="genetic", parent=f"{a.id}x{b.id}")
    if a.phase == "live":
        c.exit = copy.deepcopy(b.exit)
        if rng.random() < 0.5:
            c.context = copy.deepcopy(b.context)
    else:
        c.context = copy.deepcopy(b.context) if rng.random() < 0.5 else c.context
        c.price = copy.deepcopy(b.price) if rng.random() < 0.5 else c.price
    c.family = f"{a.family}+{b.family}"
    c.name = f"{c.family} · cruce {c.id[:6]}"
    return c


def evolve(population: list[tuple[Strategy, float]], evaluate: Callable[[Strategy], float],
           generations: int = 6, size: int = 40, seed: int = 0) -> list[tuple[Strategy, float]]:
    """population: [(estrategia, fitness)]. Devuelve todos los evaluados ordenados."""
    rng = random.Random(seed)
    seen = {s.id: f for s, f in population}
    pop = sorted(population, key=lambda x: -x[1])[:size]
    for _ in range(generations):
        children = []
        elite = pop[: max(4, size // 4)]
        attempts = 0
        # tope de intentos: si el espacio de mutaciones ya está agotado (todo
        # lo que se genera ya se evaluó) el bucle original no terminaba nunca
        while len(children) < size and attempts < size * 40:
            attempts += 1
            if rng.random() < 0.4 and len(elite) > 1:
                a, b = rng.sample(elite, 2)
                ch = crossover(a[0], b[0], rng)
            else:
                ch = mutate(rng.choice(elite)[0], rng)
            if ch.id in seen:
                continue
            f = evaluate(ch)
            seen[ch.id] = f
            children.append((ch, f))
        pop = sorted(pop + children, key=lambda x: -x[1])[:size]
    return pop


# ---------------------------------------------------------------------------
# Minería de reglas con árboles
# ---------------------------------------------------------------------------
TREE_FEATURES = ["fav_prob", "exp_goals", "elo_diff", "rest_diff", "rest_gap_abs", "prev_euro_h", "prev_euro_a",
                 "next_big_h", "next_big_a", "promoted_h", "promoted_a", "xg_total_form", "overperf_gap",
                 "xg_table_mismatch", "win_streak_max", "rank_h", "rank_a", "gp_h", "odds_b365_open", "p_ens"]


def mine_rules(bt: pd.DataFrame, market: str, selection_col: str = "selection", min_leaf: int = 150,
               max_depth: int = 4, top: int = 12) -> list[Strategy]:
    """Árbol de regresión sobre el retorno por apuesta → hojas rentables → estrategias."""
    from sklearn.tree import DecisionTreeRegressor
    out: list[Strategy] = []
    sub = bt[(bt["market"] == market) & bt["odds_b365_open"].notna()].copy()
    if len(sub) < min_leaf * 4:
        return out
    for sel, g in sub.groupby(selection_col):
        feats = [f for f in TREE_FEATURES if f in g.columns]
        X = g[feats].astype(float).fillna(-999).to_numpy()
        y = (g["win"] * (g["odds_b365_open"] - 1) - g["lose"]).to_numpy(float)
        if len(y) < min_leaf * 4:
            continue
        tree = DecisionTreeRegressor(max_depth=max_depth, min_samples_leaf=min_leaf, random_state=0)
        tree.fit(X, y)
        leaves = tree.apply(X)
        stats = pd.DataFrame({"leaf": leaves, "y": y}).groupby("leaf")["y"].agg(["mean", "count"])
        good = stats[stats["mean"] > 0.0].sort_values("mean", ascending=False).head(top)
        for leaf_id in good.index:
            conds = _leaf_path(tree, feats, leaf_id)
            filters = [{"f": f, "op": op, "v": round(float(v), 4)} for f, op, v in conds if v > -900]
            if not filters:
                continue
            s = Strategy(name=f"Árbol {market} {sel} · {len(filters)} condiciones", phase="pre", family="TREE",
                         hypothesis="Regla descubierta por el árbol: zona donde el resultado real se separa del precio",
                         market={"market": market, "selection": sel, **({"line": 2.5} if market == "OU" else {})},
                         context={"filters": filters}, price={"source": "b365_open"}, origin="tree")
            out.append(s)
    return out


def _leaf_path(tree, feats, leaf_id):
    t = tree.tree_
    path = []
    def rec(node, acc):
        if node == leaf_id:
            path.extend(acc)
            return True
        if t.children_left[node] == -1:
            return False
        f, thr = feats[t.feature[node]], t.threshold[node]
        return rec(t.children_left[node], acc + [(f, "<=", thr)]) or rec(t.children_right[node], acc + [(f, ">", thr)])
    rec(0, [])
    # simplifica: por variable, el umbral más restrictivo
    best: dict = {}
    for f, op, v in path:
        k = (f, op)
        if k not in best:
            best[k] = v
        else:
            best[k] = min(best[k], v) if op == "<=" else max(best[k], v)
    return [(f, op, v) for (f, op), v in best.items()]
