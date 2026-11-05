"""Ensemble pre-partido con validación walk-forward.

Miembros:
  - mercado (probabilidades sin margen; la referencia más fuerte que existe)
  - Dixon-Coles goles y Dixon-Coles xG
  - Gradient boosting y red neuronal (MLP) sobre variables "fundamentales"
    (Elo, forma xG, descanso, Europa, sándwich, ascendidos, tabla...)
Un apilador (regresión logística multinomial sobre log-probabilidades) aprende
cuánto pesar cada miembro. Todo se entrena solo con temporadas anteriores a la
que se predice, así que las métricas son fuera de muestra.

El objetivo honesto: en mercados líquidos el ensemble rara vez bate al cierre
de Pinnacle; su valor está en (1) detectar cuándo la cuota de bet365 de
APERTURA está lejos del consenso y (2) filtrar estrategias.
"""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.base import BaseEstimator, TransformerMixin

from .poisson import probs_1x2, prob_over, score_matrix

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)


class _DropDegenerate(BaseEstimator, TransformerMixin):
    """Quita columnas vacías (todo NaN) o constantes en el entrenamiento.
    Sin xG de Understat esas columnas existen pero están vacías y el
    gradient boosting de scikit-learn revienta con ellas."""

    def fit(self, X, y=None):
        X = np.asarray(X, float)
        keep = []
        for j in range(X.shape[1]):
            col = X[:, j]
            col = col[~np.isnan(col)]
            keep.append(len(col) > 0 and np.nanmax(col) > np.nanmin(col))
        self.keep_ = np.array(keep, dtype=bool)
        if not self.keep_.any():
            self.keep_[0] = True
        return self

    def transform(self, X):
        return np.asarray(X, float)[:, self.keep_]

FUND_FEATURES = ["elo_diff", "elo_h", "elo_a", "gf_h", "ga_h", "xgf_h", "xga_h", "gf_a", "ga_a", "xgf_a", "xga_a",
                 "rest_h", "rest_a", "rest_diff", "prev_euro_h", "prev_euro_a", "euro_hangover_h", "euro_hangover_a",
                 "next_big_h", "next_big_a", "days_next_h", "days_next_a", "promoted_h", "promoted_a",
                 "ppg_h", "ppg_a", "gp_h", "n_h", "n_a", "dc_lh", "dc_la", "xdc_lh", "xdc_la"]
COMP_CODES = {"EPL": 0, "LALIGA": 1, "SERIEA": 2, "BUNDES": 3, "LIGUE1": 4, "UCL": 5, "UEL": 6, "UECL": 7, "CDR": 8}


def _target(df: pd.DataFrame) -> np.ndarray:
    return np.where(df["hg"] > df["ag"], 0, np.where(df["hg"] == df["ag"], 1, 2))


def _X(df: pd.DataFrame) -> np.ndarray:
    X = df.reindex(columns=FUND_FEATURES).astype(float).to_numpy()
    comp = df["comp"].map(COMP_CODES).fillna(9).to_numpy()[:, None]
    return np.hstack([X, comp])


def _safe_log(p):
    return np.log(np.clip(np.asarray(p, float), 1e-4, 1))


def member_probs(df: pd.DataFrame, phase: str = "open") -> dict[str, np.ndarray]:
    out = {}
    mk = df.reindex(columns=[f"mkt_{phase}_H", f"mkt_{phase}_D", f"mkt_{phase}_A"]).to_numpy(float)
    out["mkt"] = mk
    out["dc"] = df.reindex(columns=["dc_pH", "dc_pD", "dc_pA"]).to_numpy(float)
    out["xdc"] = df.reindex(columns=["xdc_pH", "xdc_pD", "xdc_pA"]).to_numpy(float)
    return out


def _fill(p: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    bad = ~np.isfinite(p).all(axis=1)
    p = p.copy()
    p[bad] = fallback[bad]
    return p


def walk_forward(ds: pd.DataFrame, phase: str = "open", min_train_seasons: int = 2,
                 use_mlp: bool = True) -> pd.DataFrame:
    """Predicciones fuera de muestra temporada a temporada. Devuelve columnas ens_*."""
    ds = ds.sort_values("kickoff").reset_index(drop=True)
    done = ds[(ds["status"] == "finished") & ds["hg"].notna()].copy()
    seasons = sorted(done["season"].dropna().unique())
    out_parts = []
    prior = np.array([0.45, 0.27, 0.28])
    target_seasons = seasons[min_train_seasons:] + ["__future__"]
    for s in target_seasons:
        if s == "__future__":
            test = ds[~((ds["status"] == "finished") & ds["hg"].notna())]
            train = done
        else:
            test = ds[ds["season"] == s]
            train = done[done["season"] < s]
        if test.empty or len(train) < 300:
            continue
        mem_tr, mem_te = member_probs(train, phase), member_probs(test, phase)
        base_tr = np.tile(prior, (len(train), 1))
        base_te = np.tile(prior, (len(test), 1))
        mk_tr = _fill(mem_tr["mkt"], base_tr)
        mk_te = _fill(mem_te["mkt"], base_te)
        y = _target(train)
        Xtr, Xte = _X(train), _X(test)
        hgb = make_pipeline(_DropDegenerate(),
                            HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=15,
                                                           l2_regularization=1.0, early_stopping=True, random_state=0))
        hgb.fit(Xtr, y)
        p_hgb_tr = _oof_proba(hgb, Xtr, y)
        p_hgb_te = hgb.predict_proba(Xte)
        comps = {"mkt": (mk_tr, mk_te), "dc": (_fill(mem_tr["dc"], mk_tr), _fill(mem_te["dc"], mk_te)),
                 "xdc": (_fill(mem_tr["xdc"], mk_tr), _fill(mem_te["xdc"], mk_te)), "hgb": (p_hgb_tr, p_hgb_te)}
        if use_mlp:
            mlp = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                MLPClassifier(hidden_layer_sizes=(32, 16), alpha=1e-2, max_iter=400,
                                              early_stopping=True, random_state=0))
            mlp.fit(Xtr, y)
            comps["mlp"] = (_oof_proba(mlp, Xtr, y), mlp.predict_proba(Xte))
        Ztr = np.hstack([_safe_log(v[0]) for v in comps.values()])
        Zte = np.hstack([_safe_log(v[1]) for v in comps.values()])
        stk = LogisticRegression(C=0.5, max_iter=2000)
        stk.fit(Ztr, y)
        p = stk.predict_proba(Zte)
        part = pd.DataFrame({"id": test["id"].values, f"ens_{phase}_pH": p[:, 0], f"ens_{phase}_pD": p[:, 1],
                             f"ens_{phase}_pA": p[:, 2]})
        for name, (_, te) in comps.items():
            part[f"{name}_{phase}_pH"], part[f"{name}_{phase}_pD"], part[f"{name}_{phase}_pA"] = te[:, 0], te[:, 1], te[:, 2]
        # goles: mezcla geométrica de λ de mercado y de DC (peso aprendido en train)
        part = part.merge(_goal_blend(train, test, phase), on="id", how="left")
        part["trained_on"] = len(train)
        part["season_pred"] = s
        out_parts.append(part)
    if not out_parts:
        return pd.DataFrame()
    return pd.concat(out_parts, ignore_index=True)


def _oof_proba(model, X, y, folds: int = 4) -> np.ndarray:
    """Probabilidades fuera de pliegue (cronológicas) para entrenar el apilador sin sobreajuste."""
    n = len(y)
    out = np.tile(np.bincount(y, minlength=3) / n, (n, 1)).astype(float)
    edges = np.linspace(0, n, folds + 1).astype(int)
    from sklearn.base import clone
    for k in range(1, folds):
        tr = slice(0, edges[k])
        te = slice(edges[k], edges[k + 1])
        if edges[k] < 200 or len(np.unique(y[tr])) < 3:
            continue
        m = clone(model)
        m.fit(X[tr], y[tr])
        out[te] = m.predict_proba(X[te])
    return out


def _goal_blend(train: pd.DataFrame, test: pd.DataFrame, phase: str) -> pd.DataFrame:
    def lams(df, src):
        if src == "mkt":
            return df.get(f"mkt_{phase}_lh"), df.get(f"mkt_{phase}_la")
        return df.get(f"{src}_lh"), df.get(f"{src}_la")
    best_w, best_dev = (1.0, 0.0, 0.0), math.inf
    tr = train.dropna(subset=[c for c in (f"mkt_{phase}_lh",) if c in train.columns])
    if len(tr) > 200 and "dc_lh" in tr.columns:
        g_h, g_a = tr["hg"].to_numpy(float), tr["ag"].to_numpy(float)
        L = {s: [np.log(np.clip(np.nan_to_num(np.asarray(x, float), nan=np.nan), 1e-3, None)) for x in lams(tr, s)]
             for s in ("mkt", "dc", "xdc")}
        for w1 in np.linspace(0, 0.5, 6):
            for w2 in np.linspace(0, 0.5, 6):
                if w1 + w2 > 0.8:
                    continue
                w0 = 1 - w1 - w2
                dev = 0.0
                for side, g in ((0, g_h), (1, g_a)):
                    ll = w0 * L["mkt"][side] + w1 * np.nan_to_num(L["dc"][side], nan=0) + w2 * np.nan_to_num(L["xdc"][side], nan=0)
                    ll = np.where(np.isnan(L["dc"][side]) | np.isnan(L["xdc"][side]), L["mkt"][side], ll)
                    lam = np.exp(ll)
                    m = np.isfinite(lam)
                    dev += float((lam[m] - g[m] * np.log(lam[m])).sum())
                if dev < best_dev:
                    best_dev, best_w = dev, (w0, w1, w2)
    w0, w1, w2 = best_w
    res = []
    for _, r in test.iterrows():
        lm = (r.get(f"mkt_{phase}_lh"), r.get(f"mkt_{phase}_la"))
        ld = (r.get("dc_lh"), r.get("dc_la"))
        lx = (r.get("xdc_lh"), r.get("xdc_la"))
        out = []
        for k in (0, 1):
            parts = [(w0, lm[k]), (w1, ld[k]), (w2, lx[k])]
            parts = [(w, v) for w, v in parts if v is not None and pd.notna(v) and v > 0]
            if not parts:
                out.append(np.nan)
                continue
            tw = sum(w for w, _ in parts) or 1
            out.append(math.exp(sum(w * math.log(v) for w, v in parts) / tw))
        res.append({"id": r["id"], f"ens_{phase}_lh": out[0], f"ens_{phase}_la": out[1],
                    "goal_w": f"{w0:.2f}/{w1:.2f}/{w2:.2f}"})
    df = pd.DataFrame(res)
    df[f"ens_{phase}_pO25"] = [prob_over(score_matrix(a, b, -0.05), 2.5) if pd.notna(a) and pd.notna(b) else np.nan
                               for a, b in zip(df[f"ens_{phase}_lh"], df[f"ens_{phase}_la"])]
    return df


def scores(df: pd.DataFrame, phase: str = "open") -> pd.DataFrame:
    """Log-loss y Brier de cada miembro (para la página de aprendizaje)."""
    d = df[(df["status"] == "finished") & df["hg"].notna()]
    if d.empty:
        return pd.DataFrame()
    y = _target(d)
    rows = []
    for name in ("ens", "mkt", "dc", "xdc", "hgb", "mlp"):
        cols = [f"{name}_{phase}_pH", f"{name}_{phase}_pD", f"{name}_{phase}_pA"]
        if not all(c in d.columns for c in cols):
            continue
        p = d[cols].to_numpy(float)
        m = np.isfinite(p).all(axis=1)
        if m.sum() < 50:
            continue
        pp = np.clip(p[m], 1e-6, 1)
        yy = y[m]
        ll = -np.mean(np.log(pp[np.arange(len(yy)), yy]))
        onehot = np.eye(3)[yy]
        brier = np.mean(((pp - onehot) ** 2).sum(axis=1))
        rows.append({"model": name, "n": int(m.sum()), "logloss": round(float(ll), 4), "brier": round(float(brier), 4)})
    return pd.DataFrame(rows)
