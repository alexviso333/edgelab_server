"""Bajas, noticias y rotaciones → cuánto pierde cada equipo.

Para cada equipo se compara lo que aporta el once que va a salir (confirmado o previsto) con su once habitual de los
últimos partidos, jugador a jugador (goles esperados, asistencias esperadas, defensa y nota). Si aún no hay once, se
usan las bajas publicadas (lesión, sanción, duda) y las noticias que dan a alguien como baja.

    r = 1    → sale con lo de siempre
    r = 0.8  → le falta el 20 % de lo que suele aportar (estrella lesionada, rotación…)

Con eso se ajustan sus goles esperados (ataque) y un poco los del rival (defensa), SOLO por la parte que el modelo aún
no sabía: si la predicción ya se hizo con esas bajas, no se cuenta dos veces.
"""
from __future__ import annotations

import json
import time

from .core import db


def _impact(x) -> float:
    try:
        return max(0.0, float(x or 0))
    except (TypeError, ValueError):
        return 0.0


def team_ratios(mid: int, fresh: bool = False) -> dict:
    """{'H': {'r', 'src', 'missing': [nombres]}, 'A': {...}} (se guarda en kv absr:<id>)."""
    key = f"absr:{mid}"
    c = db.kv_get(key)
    if c and not fresh and time.time() - c.get("ts", 0) < 1800:
        return c.get("v") or {}
    from .models.players import latest_player_form, match_lineups
    try:
        lu = match_lineups(mid)
    except Exception:
        lu = {}
    m = db.q1("SELECT kickoff FROM matches WHERE id=?", (mid,)) or {}
    news = (db.kv_get(f"news:{mid}") or {}).get("flags") or []
    out = {}
    for side in ("H", "A"):
        usual = lu.get(f"usual_{side}") or 0.0
        if usual <= 0.3:
            continue
        starters = [p for p in (lu.get(side) or []) if p.get("starter")]
        missing = [p for p in (lu.get(f"missing_{side}") or [])]
        if len(starters) >= 9 and lu.get("source") in ("confirmed", "predicted"):
            strength = sum(_impact((p.get("form") or {}).get("impact")) for p in starters)
            r = strength / usual
            src = "once confirmado" if lu.get("source") == "confirmed" else "once previsto"
            names = [p["name"] for p in sorted(missing, key=lambda p: -_impact(p.get("impact"))) if _impact(p.get("impact")) > 0.15][:3]
        else:
            out_ids = {p["id"] for p in missing if p.get("status") == "baja"}
            # noticias que lo dan como baja (y no está ya en la lista oficial)
            extra = [f for f in news if f.get("kind") == "baja" and f.get("level") == "fuera" and f.get("side") == side
                     and f.get("player_id") and f["player_id"] not in out_ids]
            form = latest_player_form([f["player_id"] for f in extra], m.get("kickoff")) if extra else {}
            lost = sum(_impact(p.get("impact")) for p in missing if p.get("status") == "baja")
            lost += 0.5 * sum(_impact(p.get("impact")) for p in missing if p.get("status") == "duda")
            lost += 0.7 * sum(_impact((form.get(f["player_id"]) or {}).get("impact")) for f in extra)
            r = 1 - lost / usual
            src = "bajas publicadas" + (" y noticias" if extra else "")
            names = [p["name"] for p in sorted([p for p in missing if p.get("status") == "baja"], key=lambda p: -_impact(p.get("impact")))
                     if _impact(p.get("impact")) > 0.15][:3] + [f["player"] for f in extra][:2]
        out[side] = {"r": round(min(1.1, max(0.5, r)), 3), "src": src, "missing": names}
    db.kv_set(key, {"ts": int(time.time()), "v": out})
    return out


def factors(mid: int, source: str) -> dict:
    """Multiplicadores de goles esperados {'H': fh, 'A': fa} según bajas/rotaciones.
    El modelo propio (ensemble) NO usa alineaciones (sus variables son Elo, forma, xG, descanso…), así que el ajuste
    se aplica entero; algo suavizado porque el mercado, que sí sabe de bajas, es uno de sus miembros. Si el precio sale
    casi todo del mercado ('mix': 65 % casas) solo se ajusta la parte del modelo; si sale solo del mercado, nada."""
    if source == "market":
        return {"H": 1.0, "A": 1.0}
    now = team_ratios(mid)
    if not now:
        return {"H": 1.0, "A": 1.0}
    expo = {"ensemble": 0.75, "mix": 0.35}.get(source, 1.0)
    rel = {s: ((now.get(s) or {}).get("r", 1.0)) ** expo for s in ("H", "A")}
    att = {s: min(1.06, max(0.78, rel[s] ** 0.6)) for s in rel}
    dfn = {s: min(1.08, max(0.95, (1 / rel[s]) ** 0.2)) if rel[s] else 1.0 for s in rel}     # defensa más floja → el rival marca algo más
    return {"H": round(att["H"] * dfn["A"], 4), "A": round(att["A"] * dfn["H"], 4)}


def signature(mid: int) -> str:
    """Huella de todo lo que puede cambiar quién juega: bajas publicadas, noticias de baja y el once (fuente + titulares)."""
    miss = sorted((p.get("id"), p.get("status")) for p in ((db.kv_get(f"missing:{mid}") or {}).get("players") or []))
    news = sorted((f.get("player_id"), f.get("level")) for f in ((db.kv_get(f"news:{mid}") or {}).get("flags") or [])
                  if f.get("kind") == "baja")
    lu = db.q("SELECT source, player_id FROM lineups WHERE match_id=? AND starter=1 AND source IN ('confirmed','predicted') "
              "ORDER BY source, player_id", (mid,))
    return json.dumps([miss, news, [(r["source"], r["player_id"]) for r in lu]], default=str)


def snapshot_for_predictions(mids: list[int]) -> None:
    """Al rehacer las predicciones: se apunta con qué once/bajas se hicieron (para no contarlo dos veces luego)."""
    for mid in mids:
        try:
            v = team_ratios(mid, fresh=True)
            db.kv_set(f"absr_pred:{mid}", {s: (v.get(s) or {}).get("r", 1.0) for s in ("H", "A")})
        except Exception:
            pass
