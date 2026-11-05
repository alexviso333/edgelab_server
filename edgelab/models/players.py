"""Factor alineación y duelos entre jugadores.

Valor de un jugador ANTES de cada partido (sin fuga): media de sus últimas 10
apariciones con minutos de
    impacto = 1.6·(xG+xA)/90 + 0.12·(entradas+intercepciones)/90 + 0.6·(nota−6.7)
Fuerza de un once = suma del impacto de los titulares. Se compara con el once
habitual del equipo (media de sus 6 partidos anteriores):
    lu_gap  < 0  → sale debilitado (bajas, rotaciones)
    rotation     → titulares que no fueron titulares en el partido anterior.

Duelos: se emparejan por zona los atacantes de un equipo con los defensores
del otro (extremo izquierdo vs lateral derecho, delantero vs centrales, mediapunta
vs pivotes…) y se compara lo que cada uno hace por 90': regates, xG, faltas
recibidas frente a entradas, duelos ganados, faltas cometidas y tarjetas. De ahí
salen la ventaja de cada duelo, las faltas esperadas y el riesgo de tarjeta del
defensor (útil para mercados de jugador de bet365: tarjeta, faltas).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..core import db

PER90 = ["xg", "xa", "won_contest", "was_fouled", "shots", "tackles", "interceptions", "fouls", "yellow",
         "duel_won", "duel_lost", "key_pass", "crosses"]

ZONE_ATT = {"LW": "L", "LM": "L", "LWB": "L", "LF": "L", "RW": "R", "RM": "R", "RWB": "R", "RF": "R",
            "ST": "C", "CF": "C", "LST": "C", "RST": "C", "F": "C", "CAM": "M", "LCAM": "M", "RCAM": "M"}
ZONE_DEF = {"RB": "L", "RWB": "L", "LB": "R", "LWB": "R", "CB": "C", "LCB": "C", "RCB": "C",
            "CDM": "M", "LCDM": "M", "RCDM": "M"}
ZONE_NAME = {"L": "la banda izquierda", "R": "la banda derecha", "C": "el área", "M": "la zona de mediapunta"}


def player_history() -> pd.DataFrame:
    rows = db.q("SELECT ps.*, m.kickoff FROM player_stats ps JOIN matches m ON m.id=ps.match_id WHERE ps.minutes>0")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values(["player_id", "kickoff"])
    mins = df["minutes"].clip(lower=1)
    for c in PER90:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["impact_raw"] = (1.6 * (df["xg"] + df["xa"]) / mins * 90 + 0.12 * (df["tackles"] + df["interceptions"]) / mins * 90
                        + 0.6 * (pd.to_numeric(df["rating"], errors="coerce").fillna(6.7) - 6.7))
    g = df.groupby("player_id", sort=False)
    # medias móviles PREVIAS (shift) de 10 apariciones, ponderadas por minutos
    for c in PER90:
        num = g[c].transform(lambda s: s.shift(1).rolling(10, min_periods=1).sum())
        den = g["minutes"].transform(lambda s: s.shift(1).rolling(10, min_periods=1).sum())
        df[f"{c}_p90"] = num / den.replace(0, np.nan) * 90
    df["impact"] = g["impact_raw"].transform(lambda s: s.shift(1).rolling(10, min_periods=2).mean())
    df["apps"] = g.cumcount()
    return df


def lineup_features(ph: pd.DataFrame | None = None) -> pd.DataFrame:
    """Por partido: fuerza del once, diferencia con el habitual, rotaciones y ventaja en duelos."""
    ph = player_history() if ph is None else ph
    if ph.empty:
        return pd.DataFrame()
    st = ph[ph["starter"] == 1].copy()
    st["impact"] = st["impact"].fillna(0.0)
    agg = st.groupby(["match_id", "side", "team_id", "kickoff"]).agg(strength=("impact", "sum"), n=("impact", "size")).reset_index()
    agg = agg.sort_values(["team_id", "kickoff"])
    agg["usual"] = agg.groupby("team_id")["strength"].transform(lambda s: s.shift(1).rolling(6, min_periods=2).mean())
    agg["gap"] = agg["strength"] - agg["usual"]
    # rotaciones respecto al partido anterior del equipo
    starters = st.groupby(["team_id", "match_id", "kickoff"])["player_id"].apply(frozenset).reset_index().sort_values(["team_id", "kickoff"])
    starters["prev"] = starters.groupby("team_id")["player_id"].shift(1)
    starters["rotation"] = [len(a - b) if isinstance(b, frozenset) else np.nan for a, b in zip(starters["player_id"], starters["prev"])]
    agg = agg.merge(starters[["team_id", "match_id", "rotation"]], on=["team_id", "match_id"], how="left")
    duel = duel_edges(st)
    out = {}
    for r in agg.itertuples(index=False):
        d = out.setdefault(r.match_id, {"id": r.match_id})
        s = "h" if r.side == "H" else "a"
        d[f"lu_strength_{s}"], d[f"lu_gap_{s}"], d[f"rotation_{s}"] = r.strength, r.gap, r.rotation
    for mid, v in duel.items():
        out.setdefault(mid, {"id": mid}).update(v)
    F = pd.DataFrame(list(out.values()))
    if F.empty:
        return F
    for c in ("lu_gap_h", "lu_gap_a", "rotation_h", "rotation_a", "duel_edge"):
        if c not in F.columns:
            F[c] = np.nan
    F["key_absences_gap"] = (F["lu_gap_h"] - F["lu_gap_a"]).abs()
    F["rotation_gap"] = (F["rotation_h"] - F["rotation_a"]).abs()
    return F


def _att_score(r) -> float:
    return (0.5 * (r.get("won_contest_p90") or 0) + 2.0 * (r.get("xg_p90") or 0) + 1.2 * (r.get("xa_p90") or 0)
            + 0.25 * (r.get("was_fouled_p90") or 0))


def _def_score(r) -> float:
    dw, dl = r.get("duel_won_p90") or 0, r.get("duel_lost_p90") or 0
    pct = dw / (dw + dl) if dw + dl > 0 else 0.5
    return 0.35 * (r.get("tackles_p90") or 0) + 0.3 * (r.get("interceptions_p90") or 0) + 2.0 * (pct - 0.5)


def duel_edges(st: pd.DataFrame) -> dict[int, dict]:
    """Ventaja agregada en duelos (local − visitante) por partido, con stats previas al partido."""
    out: dict[int, dict] = {}
    cols = ["match_id", "side", "position"] + [f"{c}_p90" for c in PER90]
    st = st[[c for c in cols if c in st.columns]]
    for mid, g in st.groupby("match_id"):
        recs = g.to_dict("records")
        edge = {"H": 0.0, "A": 0.0}
        for side, opp in (("H", "A"), ("A", "H")):
            att = [r for r in recs if r["side"] == side and r["position"] in ZONE_ATT]
            dfn = [r for r in recs if r["side"] == opp and r["position"] in ZONE_DEF]
            for a in att:
                ds = [d for d in dfn if ZONE_DEF[d["position"]] == ZONE_ATT[a["position"]]]
                if ds:
                    edge[side] += _att_score(a) - np.mean([_def_score(d) for d in ds])
        out[mid] = {"duel_edge": edge["H"] - edge["A"]}
    return out


# ------------------------------------------------------------------ partido concreto (web / informe)
def latest_player_form(player_ids: list[int], before_ts: int | None = None) -> dict[int, dict]:
    """Stats por 90' de las últimas 10 apariciones de cada jugador."""
    if not player_ids:
        return {}
    q = ("SELECT ps.*, m.kickoff FROM player_stats ps JOIN matches m ON m.id=ps.match_id WHERE ps.minutes>0 AND ps.player_id IN (%s)"
         % ",".join(str(int(p)) for p in player_ids))
    if before_ts:
        q += f" AND m.kickoff < {int(before_ts)}"
    df = pd.DataFrame(db.q(q + " ORDER BY m.kickoff DESC"))
    out: dict[int, dict] = {}
    if df.empty:
        return out
    for pid, g in df.groupby("player_id"):
        g = g.head(10)
        mins = max(float(g["minutes"].sum()), 1.0)
        rec = {f"{c}_p90": float(pd.to_numeric(g[c], errors="coerce").fillna(0).sum()) / mins * 90 for c in PER90}
        rec["apps"] = int(len(g))
        rec["minutes"] = float(g["minutes"].mean())
        rec["rating"] = float(pd.to_numeric(g["rating"], errors="coerce").dropna().mean()) if g["rating"].notna().any() else None
        rec["impact"] = (1.6 * (rec["xg_p90"] + rec["xa_p90"]) + 0.12 * (rec["tackles_p90"] + rec["interceptions_p90"])
                         + 0.6 * ((rec["rating"] or 6.7) - 6.7))
        rec["goals"] = int(g["goals"].sum())
        out[int(pid)] = rec
    return out


def match_lineups(match_id: int) -> dict:
    """Alineaciones del partido (confirmada > prevista > jugada) con forma de cada jugador."""
    m = db.q1("SELECT * FROM matches WHERE id=?", (match_id,))
    rows = db.q("SELECT * FROM lineups WHERE match_id=?", (match_id,))
    pref = {"confirmed": 0, "played": 1, "predicted": 2}
    best_src = min((pref.get(r["source"], 9) for r in rows), default=None)
    rows = [r for r in rows if pref.get(r["source"], 9) == best_src] if rows else []
    form = latest_player_form([r["player_id"] for r in rows], m["kickoff"] if m else None)
    out = {"source": next((k for k, v in pref.items() if v == best_src), None), "H": [], "A": [],
           "formation": {"H": m.get("formation_h") if m else None, "A": m.get("formation_a") if m else None}}
    for r in rows:
        f = form.get(r["player_id"], {})
        out[r["side"]].append({"id": r["player_id"], "name": r["name"], "pos": r["position"], "jersey": r["jersey"],
                               "starter": bool(r["starter"]), "form": f})
        if r.get("formation") and not out["formation"].get(r["side"]):
            out["formation"][r["side"]] = r["formation"]
    # once habitual: titulares más frecuentes en los 8 partidos anteriores del equipo
    for side, tid in (("H", m and m["home_id"]), ("A", m and m["away_id"])):
        if not tid:
            continue
        # once habitual con TODAS sus competiciones (liga incluida, vía Statshub), últimos 8 partidos
        games = "(SELECT id, kickoff, home_id, away_id FROM matches UNION SELECT match_id, kickoff, home_id, away_id FROM game_meta " \
                "WHERE match_id NOT IN (SELECT id FROM matches))"
        prev = db.q(f"SELECT l.player_id, l.name, COUNT(*) AS n FROM lineups l JOIN {games} mm ON mm.id=l.match_id "
                    "WHERE l.source='played' AND l.starter=1 AND ((mm.home_id=? AND l.side='H') OR (mm.away_id=? AND l.side='A')) "
                    f"AND mm.kickoff < ? AND mm.id IN (SELECT id FROM {games} WHERE (home_id=? OR away_id=?) AND kickoff<? "
                    "AND id IN (SELECT match_id FROM lineups WHERE source='played') ORDER BY kickoff DESC LIMIT 8) "
                    "GROUP BY l.player_id ORDER BY n DESC LIMIT 11",
                    (tid, tid, m["kickoff"], tid, tid, m["kickoff"]))
        # solo cuenta quien sigue en el equipo: fuera los traspasados (su último equipo es otro) y los partidos de hace meses
        recent_cut = m["kickoff"] - 75 * 86400
        cur = {r["id"]: r["team_id"] for r in db.q(
            f"SELECT id, team_id FROM players WHERE id IN ({','.join(str(int(p['player_id'])) for p in prev) or '0'})")}
        last_seen = {r["player_id"]: r["k"] for r in db.q(
            f"SELECT l.player_id, MAX(mm.kickoff) AS k FROM lineups l JOIN {games} mm ON mm.id=l.match_id WHERE l.source='played' "
            f"AND l.player_id IN ({','.join(str(int(p['player_id'])) for p in prev) or '0'}) AND mm.kickoff<? AND "
            "((mm.home_id=? AND l.side='H') OR (mm.away_id=? AND l.side='A')) GROUP BY l.player_id", (m["kickoff"], tid, tid))}
        prev = [p for p in prev if (cur.get(p["player_id"]) in (None, tid)) and (last_seen.get(p["player_id"]) or 0) >= recent_cut]
        usual = {p["player_id"]: p for p in prev}
        starters = {p["id"] for p in out[side] if p["starter"]}
        uf = latest_player_form(list(usual), m["kickoff"])
        out[f"missing_{side}"] = [{"id": pid, "name": usual[pid]["name"], "starts": usual[pid]["n"],
                                   "impact": (uf.get(pid) or {}).get("impact")}
                                  for pid in usual if pid not in starters] if starters else []
        real = [p for p in ((db.kv_get(f"missing:{match_id}") or {}).get("players") or []) if p["side"] == side and p.get("id")]
        if real:      # bajas publicadas (lesión / sanción / duda): mandan sobre las deducidas
            rf = latest_player_form([p["id"] for p in real], m["kickoff"])
            out[f"missing_{side}"] = [{"id": p["id"], "name": p["name"], "starts": (usual.get(p["id"]) or {}).get("n", 0),
                                       "impact": (rf.get(p["id"]) or {}).get("impact"), "status": p["status"], "reason": p["reason"]}
                                      for p in real]
        out[f"strength_{side}"] = sum((p["form"].get("impact") or 0) for p in out[side] if p["starter"])
        out[f"usual_{side}"] = sum((uf.get(pid) or {}).get("impact") or 0 for pid in usual)
    return out


def match_duels(lu: dict, card_avg_league: float = 0.18) -> list[dict]:
    """Duelos clave del partido con ventaja, faltas esperadas y riesgo de tarjeta."""
    duels = []
    for side, opp in (("H", "A"), ("A", "H")):
        atts = [p for p in lu.get(side, []) if p["starter"] and p["pos"] in ZONE_ATT]
        defs = [p for p in lu.get(opp, []) if p["starter"] and p["pos"] in ZONE_DEF]
        for a in atts:
            for d in defs:
                if ZONE_DEF[d["pos"]] != ZONE_ATT[a["pos"]]:
                    continue
                fa, fd = a["form"], d["form"]
                if not fa or not fd:
                    continue
                adv = _att_score(fa) - _def_score(fd)
                fouls_exp = (fa.get("was_fouled_p90", 0) * 0.5 + fd.get("fouls_p90", 0) * 0.5)
                # riesgo de tarjeta del defensor: su tasa de amarillas escalada por lo que provoca el atacante
                base = fd.get("yellow_p90", 0.15) or 0.15
                provoke = (fa.get("was_fouled_p90", 1.2) or 1.2) / 1.2
                lam = max(0.02, base * (0.6 + 0.4 * provoke)) * (fd.get("minutes", 80) or 80) / 90
                card = 1 - math.exp(-lam)
                duels.append({"side": side, "zone": ZONE_NAME[ZONE_ATT[a["pos"]]],
                              "attacker": {"name": a["name"], "pos": a["pos"], "dribbles": fa.get("won_contest_p90"),
                                           "xg": fa.get("xg_p90"), "xa": fa.get("xa_p90"), "fouled": fa.get("was_fouled_p90")},
                              "defender": {"name": d["name"], "pos": d["pos"], "tackles": fd.get("tackles_p90"),
                                           "duels_pct": (fd.get("duel_won_p90", 0) / max(fd.get("duel_won_p90", 0) + fd.get("duel_lost_p90", 0), 1e-9)),
                                           "fouls": fd.get("fouls_p90"), "yellow": fd.get("yellow_p90")},
                              "advantage": round(adv, 3), "fouls_expected": round(fouls_exp, 2), "card_risk": round(card, 3)})
    duels.sort(key=lambda x: -abs(x["advantage"]))
    return duels
