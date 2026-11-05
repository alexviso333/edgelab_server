"""Informe de partido: todos los factores cruzados en un solo sitio.

Cada factor se traduce a una señal con dirección (a favor de quién, o hacia más/
menos goles), fuerza (0-1) y una frase. El texto final lo escribe:
  - por defecto, un redactor por reglas (gratis, sin inventar nada);
  - opcionalmente una IA (Gemini o Claude) si pones su clave en .env. La IA
    solo recibe los factores calculados y se le prohíbe añadir datos nuevos.
"""
from __future__ import annotations

import json
import math
import time

import httpx
import numpy as np
import pandas as pd

from .config import ROOT, _env, settings
from .core import db
from .models.players import match_duels, match_lineups

LLM_PROVIDER = _env("LLM_PROVIDER", "")          # gemini | anthropic
GEMINI_KEY = _env("GEMINI_API_KEY", "")
ANTHROPIC_KEY = _env("ANTHROPIC_API_KEY", "")


_DS: dict = {}


def _row(match_id: int) -> dict:
    """Fila del dataset del partido. El dataset (decenas de MB) se lee una vez y queda en memoria."""
    cache = settings.data_dir / "dataset.pkl"
    if not cache.exists():
        return {}
    mt = cache.stat().st_mtime
    if _DS.get("mt") != mt:
        try:
            ds = pd.read_pickle(cache)
        except Exception:
            return {}
        _DS.update({"mt": mt, "ds": ds.set_index("id", drop=False)})
    ds = _DS["ds"]
    if match_id not in ds.index:
        return {}
    r = ds.loc[[match_id]]
    if r.empty:
        return {}
    d = r.iloc[0].to_dict()
    return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in d.items()}


def _clip(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def team_form(team_id: int, before: int, n: int = 6) -> list[dict]:
    rows = db.q("SELECT id, kickoff, comp, home_id, away_id, home_name, away_name, hg, ag FROM matches "
                "WHERE (home_id=? OR away_id=?) AND kickoff<? AND status='finished' AND hg IS NOT NULL ORDER BY kickoff DESC LIMIT ?",
                (team_id, team_id, before, n))
    out = []
    for m in rows:
        home = m["home_id"] == team_id
        gf, ga = (m["hg"], m["ag"]) if home else (m["ag"], m["hg"])
        xs = {r["side"]: json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (m["id"],))}
        from .models.secondary import stat_value
        xf = stat_value(xs.get("H" if home else "A", {}), "xg")
        xa = stat_value(xs.get("A" if home else "H", {}), "xg")
        out.append({"id": m["id"], "kickoff": m["kickoff"], "comp": m["comp"], "home": home,
                    "opp": m["away_name"] if home else m["home_name"], "gf": gf, "ga": ga, "xgf": xf, "xga": xa,
                    "res": "W" if gf > ga else ("D" if gf == ga else "L")})
    return out


def head_to_head(h: int, a: int, before: int, n: int = 10) -> list[dict]:
    rows = db.q("SELECT id, kickoff, comp, home_id, home_name, away_name, hg, ag FROM matches WHERE "
                "((home_id=? AND away_id=?) OR (home_id=? AND away_id=?)) AND kickoff<? AND status='finished' AND hg IS NOT NULL "
                "ORDER BY kickoff DESC LIMIT ?", (h, a, a, h, before, n))
    out = []
    for m in rows:
        st = {r["side"]: json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (m["id"],))}
        from .models.secondary import stat_value
        cards = [stat_value(st.get(s, {}), "cards") for s in ("H", "A")]
        out.append({**m, "cards": sum(c for c in cards if c is not None) if any(c is not None for c in cards) else None,
                    "same_venue": m["home_id"] == h})
    return out


def factors(match_id: int) -> dict:
    m = db.q1("SELECT * FROM matches WHERE id=?", (match_id,))
    if not m:
        return {}
    r = _row(match_id)
    pred = db.q1("SELECT payload FROM predictions WHERE match_id=? AND model='prematch'", (match_id,))
    pay = json.loads(pred["payload"]) if pred else {}
    F: list[dict] = []
    H, A = m["home_name"], m["away_name"]

    def add(key, label, side, strength, text, group):
        F.append({"key": key, "label": label, "side": side, "strength": round(_clip(strength), 3), "text": text, "group": group})

    # mercado vs modelo
    ens = pay.get("ens") or {}
    if pay.get("pH") and ens.get("ens_open_pH"):
        for s, nm in (("H", f"a {H}"), ("D", "al empate"), ("A", f"a {A}")):
            diff = ens[f"ens_open_p{s}"] - pay[f"p{s}"]
            if abs(diff) >= 0.025:
                add(f"model_{s}", "Modelo vs mercado", s if s != "D" else "D", abs(diff) * 8,
                    f"El ensemble da {nm} {ens[f'ens_open_p{s}']:.0%} frente al {pay[f'p{s}']:.0%} del mercado.", "Mercado")
    if r:
        ed = r.get("elo_diff")
        if ed is not None:
            add("elo", "Nivel (Elo)", "H" if ed > 0 else "A", abs(ed) / 300,
                f"Diferencia de Elo de {ed:+.0f} puntos con el factor campo incluido.", "Nivel")
        for side, nm in (("h", H), ("a", A)):
            xf, xa_ = r.get(f"xgf_{side}"), r.get(f"xga_{side}")
            if xf is not None and xa_ is not None:
                net = xf - xa_
                add(f"xg_{side}", f"Forma xG {nm}", "H" if (side == "h") == (net > 0) else "A", abs(net) / 1.2,
                    f"{nm} genera {xf:.2f} xG y concede {xa_:.2f} por partido (forma reciente ponderada).", "Forma")
            op = r.get(f"gf_{side}")
            if op is not None and xf is not None and abs(op - xf) > 0.35:
                add(f"luck_{side}", f"Suerte de cara a gol {nm}", ("A" if side == "h" else "H") if op > xf else ("H" if side == "h" else "A"),
                    abs(op - xf) / 0.8, f"{nm} marca {op:.2f} goles con {xf:.2f} xG: {'por encima' if op > xf else 'por debajo'} de lo que merece; tiende a corregirse.", "Forma")
        rd = r.get("rest_diff")
        if rd is not None and abs(rd) >= 1.5:
            add("rest", "Descanso", "H" if rd > 0 else "A", abs(rd) / 4,
                f"{H if rd > 0 else A} llega con {abs(rd):.1f} días más de descanso.", "Calendario")
        for side, nm, opp in (("h", H, "A"), ("a", A, "H")):
            if r.get(f"prev_euro_{side}"):
                add(f"euro_{side}", "Viene de Europa", opp, 0.35 + 0.3 * (r.get(f"euro_hangover_{side}") or 0),
                    f"{nm} jugó competición europea hace pocos días" + (" y perdió." if r.get(f"euro_hangover_{side}") else "."), "Calendario")
            if r.get(f"next_big_{side}"):
                add(f"sandwich_{side}", "Partido sándwich", opp, 0.35, f"{nm} tiene un partido europeo en menos de 4 días: posible rotación.", "Calendario")
            if r.get(f"promoted_{side}") and (r.get("gp_h") or 0) <= 8:
                add(f"promoted_{side}", "Recién ascendido", opp, 0.25, f"{nm} es recién ascendido y su rating arrastra la categoría inferior.", "Contexto")
        if (r.get("h2h_n") or 0) >= 3:
            ph = r.get("h2h_pts_h")
            if ph is not None and abs(ph - 1.4) > 0.5:
                add("h2h", "Cara a cara", "H" if ph > 1.4 else "A", abs(ph - 1.4) / 1.6,
                    f"En los últimos {int(r['h2h_n'])} duelos directos {H} saca {ph:.1f} puntos de media.", "Cara a cara")
            hg_ = r.get("h2h_goals")
            if hg_ is not None and abs(hg_ - 2.7) > 0.6:
                add("h2h_goals", "Cara a cara: goles", "O" if hg_ > 2.7 else "U", abs(hg_ - 2.7) / 2,
                    f"Sus enfrentamientos promedian {hg_:.1f} goles.", "Cara a cara")
        de = r.get("duel_edge")
        if de is not None and abs(de) > 0.4:
            add("duels", "Duelos individuales", "H" if de > 0 else "A", abs(de) / 2.5,
                f"Los atacantes de {H if de > 0 else A} superan a los defensores rivales en sus zonas (ventaja {abs(de):.2f}).", "Jugadores")
        ra = r.get("refereeAvgCards")
    # alineaciones (prevista / confirmada / jugada)
    lu = match_lineups(match_id)
    duels = match_duels(lu)
    for side, nm, opp in (("H", H, "A"), ("A", A, "H")):
        miss = [p for p in lu.get(f"missing_{side}", []) if (p.get("impact") or 0) > 0.25]
        if miss:
            imp = sum(p["impact"] or 0 for p in miss)
            add(f"missing_{side}", f"Bajas de {nm}", opp, imp / 1.5,
                f"{nm} no tiene en el once a {', '.join(p['name'] for p in miss[:4])} (titulares habituales).", "Alineación")
        s_, u_ = lu.get(f"strength_{side}"), lu.get(f"usual_{side}")
        if s_ and u_ and abs(s_ - u_) > 0.4:
            add(f"lu_{side}", f"Once de {nm}", side if s_ > u_ else opp, abs(s_ - u_) / 2,
                f"El once de {nm} suma {s_:.1f} de impacto frente a {u_:.1f} del habitual.", "Alineación")
    for d in duels[:3]:
        if abs(d["advantage"]) > 0.5:
            winner = d["side"] if d["advantage"] > 0 else ("A" if d["side"] == "H" else "H")
            add(f"duel_{d['attacker']['name']}", "Duelo clave", winner, abs(d["advantage"]) / 2.5,
                f"{d['attacker']['name']} ({d['attacker']['pos']}) contra {d['defender']['name']} ({d['defender']['pos']}) en {d['zone']}.", "Jugadores")
        if d["card_risk"] >= 0.3:
            add(f"card_{d['defender']['name']}", "Riesgo de tarjeta", "N", d["card_risk"],
                f"{d['defender']['name']} ({d['card_risk']:.0%} de tarjeta estimada) defendiendo a {d['attacker']['name']}, que recibe {d['attacker'].get('fouled') or 0:.1f} faltas/90.", "Jugadores")
    ex = json.loads(m["extra"]) if m.get("extra") else {}
    if ex.get("refereeAvgCards"):
        rc = ex["refereeAvgCards"]
        if abs(rc - 4.3) > 0.7:
            add("ref", "Árbitro", "N", abs(rc - 4.3) / 2.5, f"{m.get('referee') or 'El árbitro'} saca {rc:.1f} tarjetas por partido ({ex.get('refereeGames') or '?'} partidos).", "Árbitro")
    F.sort(key=lambda f: -f["strength"])
    lean = {"H": 0.0, "A": 0.0, "D": 0.0, "O": 0.0, "U": 0.0}
    for f in F:
        if f["side"] in lean:
            lean[f["side"]] += f["strength"]
    return {"match": m, "factors": F, "lean": lean, "lineups": lu, "duels": duels[:8],
            "h2h": head_to_head(m["home_id"], m["away_id"], m["kickoff"]) if m["home_id"] else [],
            "form": {"H": team_form(m["home_id"], m["kickoff"]) if m["home_id"] else [],
                     "A": team_form(m["away_id"], m["kickoff"]) if m["away_id"] else []},
            "prediction": pay}


def narrative_rules(fx: dict) -> str:
    m = fx["match"]
    F = fx["factors"]
    p = fx.get("prediction") or {}
    H, A = m["home_name"], m["away_name"]
    parts = []
    if p.get("pH"):
        fav = H if p["pH"] >= p["pA"] else A
        parts.append(f"El mercado ve favorito a {fav} ({max(p['pH'], p['pA']):.0%}), con {p['lh'] + p['la']:.1f} goles esperados.")
    lean = fx["lean"]
    side = max(("H", "A"), key=lambda s: lean[s])
    if lean[side] - lean["A" if side == "H" else "H"] > 0.4:
        parts.append(f"La suma de factores se inclina hacia {H if side == 'H' else A}.")
    elif F:
        parts.append("Los factores están bastante repartidos entre los dos equipos.")
    top = [f for f in F if f["strength"] >= 0.25][:4]
    if top:
        parts.append("Lo que más pesa: " + " ".join(f["text"] for f in top))
    if lean["O"] > lean["U"] + 0.3:
        parts.append("Los indicios apuntan a más goles de lo normal.")
    elif lean["U"] > lean["O"] + 0.3:
        parts.append("Los indicios apuntan a pocos goles.")
    return " ".join(parts) or "Aún no hay datos suficientes para este partido."


def narrative_llm(fx: dict) -> str | None:
    if not LLM_PROVIDER:
        return None
    m = fx["match"]
    payload = {"partido": f"{m['home_name']} vs {m['away_name']}", "competicion": m["comp"],
               "prob_mercado": {k: fx["prediction"].get(k) for k in ("pH", "pD", "pA", "lh", "la")},
               "factores": [{k: f[k] for k in ("label", "side", "strength", "text")} for f in fx["factors"][:14]],
               "duelos": fx["duels"][:4]}
    prompt = ("Eres analista de apuestas de fútbol. Con SOLO estos datos calculados (no inventes lesiones, noticias ni cifras), "
              "escribe en castellano un análisis previo de 120-160 palabras: qué factores pesan más, si se contradicen, y qué mercados "
              "merecería mirar. No recomiendes apostar sin ventaja frente a la cuota. Datos:\n" + json.dumps(payload, ensure_ascii=False, default=str))
    try:
        if LLM_PROVIDER == "gemini" and GEMINI_KEY:
            r = httpx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                           params={"key": GEMINI_KEY}, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=40)
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        if LLM_PROVIDER == "anthropic" and ANTHROPIC_KEY:
            r = httpx.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
                           json={"model": "claude-haiku-5-5", "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]}, timeout=40)
            return r.json()["content"][0]["text"]
    except Exception:
        return None
    return None


def report(match_id: int, use_cache: bool = True) -> dict:
    if use_cache:
        c = db.q1("SELECT ts, payload FROM reports WHERE match_id=?", (match_id,))
        if c and time.time() - c["ts"] < 3 * 3600:
            return json.loads(c["payload"])
    fx = factors(match_id)
    if not fx:
        return {}
    ai = narrative_llm(fx)
    fx["narrative"] = ai or narrative_rules(fx)
    fx["narrative_source"] = "ia" if ai else "reglas"
    out = json.loads(json.dumps(fx, default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o)))
    db.execute("INSERT OR REPLACE INTO reports(match_id,ts,payload) VALUES(?,?,?)", (match_id, int(time.time()), json.dumps(out)))
    return out
