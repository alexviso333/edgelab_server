"""Mensajes de Telegram con el argumento de cada apuesta, como lo escribiría un tipster: qué es, cuánto vale, por qué
tiene valor (factores a favor, riesgos, qué dice el mercado) y hasta qué cuota merece la pena. Todo sale de lo que la
web ya calcula (modelo, casas, análisis completo de la apuesta); no se inventa nada."""
from __future__ import annotations

import html
import re
import time

from .core import db

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _e(x) -> str:
    return html.escape(str(x or ""), quote=False)


def _n(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _when(ts: int) -> str:
    t = time.localtime(ts)
    hoy = time.localtime()
    dia = "hoy" if t.tm_yday == hoy.tm_yday and t.tm_year == hoy.tm_year else \
        "mañana" if (t.tm_yday - hoy.tm_yday) % 366 == 1 else f"{DIAS[t.tm_wday]} {time.strftime('%d/%m', t)}"
    return f"{dia} {time.strftime('%H:%M', t)}"


def _bar(p: float) -> str:
    k = max(0, min(5, round(p * 5)))
    return "▰" * k + "▱" * (5 - k)


SKIP = {"precio", "consenso", "simulacion", "racha10"}      # las mismas que oculta la página en «Por qué»


def _insight(v: dict) -> dict:
    """El MISMO análisis que enseña la página de la apuesta (endpoint /api/match/{id}/bet): el guardado si tiene menos
    de una hora; si no, se calcula ahora."""
    from .plans import vkey
    k = vkey(v["match"]["id"], v["market"], v.get("period"), v["selection"], v.get("line"))
    pre = db.kv_get("betins:" + k)
    if pre and time.time() - pre.get("ts", 0) < 3600:
        return pre.get("r") or {}
    try:
        from .insights import bet_insight
        r = bet_insight(v["match"]["id"], v["market"], v.get("period") or "FT", v["selection"], v.get("line"))
        if r:
            db.kv_set("betins:" + k, {"ts": int(time.time()), "r": r})
        return r or {}
    except Exception:
        return {}


def factors(v: dict, n_pro: int = 6, n_con: int = 3, f: dict | None = None) -> tuple[list[str], list[str]]:
    """Las señales «A favor» y «En contra» de la página (mismo filtro y mismo orden: las de más peso primero)."""
    f = f if f is not None else _insight(v)
    sig = f.get("signals") or []
    if sig:
        pro = [x["text"] for x in sorted([x for x in sig if x.get("dir", 0) > 0 and x.get("key") not in SKIP], key=lambda x: -x.get("w", 0))]
        con = [x["text"] for x in sorted([x for x in sig if x.get("dir", 0) < 0], key=lambda x: -x.get("w", 0))]
    else:
        d = v.get("dossier") or {}
        pro, con = list(d.get("pro") or []), list(d.get("con") or [])
    return pro[:n_pro], con[:n_con]


def market_line(v: dict) -> str | None:
    p, pm = v.get("p"), v.get("p_market")
    nb = v.get("n_books")
    if pm:
        dif = (p - pm) * 100
        s = f"Las casas la ven en un <b>{round(pm * 100)} %</b>" + (f" ({nb} casas)" if nb else "") + \
            f"; nosotros en un <b>{round(p * 100)} %</b> ({'+' if dif >= 0 else ''}{round(dif)} pts)."
    else:
        s = None
    best, bb = v.get("best"), v.get("best_book")
    if best and v.get("b365") and best > v["b365"] * 1.03 and bb:
        s = (s + " " if s else "") + f"(En {_e(bb)} está a {_n(best)}.)"
    return s


def ten_dots(p: float) -> str:
    """Los 10 puntos de la página: cuántas de cada 10 veces sale."""
    k = max(0, min(10, round(p * 10)))
    return "●" * k + "○" * (10 - k)


def risk_line(p: float) -> str:
    """El mismo riesgo que enseña la página (según cuántas veces sale)."""
    if p >= 0.62:
        return "🟢 <b>Riesgo bajo</b>: sale la mayoría de las veces"
    if p >= 0.45:
        return "🟡 <b>Riesgo medio</b>: sale más o menos una de cada dos"
    return "🔴 <b>Riesgo alto</b>: sale pocas veces, pero paga mucho"


def short(t: str, n: int = 70) -> str:
    """Una señal en una línea (el texto entero va en «Todo el análisis»): se queda con la parte que lleva el dato."""
    t = " ".join(str(t or "").split())
    if len(t) <= n:
        return _cap(t)
    head = re.split(r"; |: ", t)[0]
    if len(head) > n:
        head = re.sub(r" \([^)]*\)", "", head)            # fuera los paréntesis antes de recortar
    if len(head) <= n and (len(head) >= 25 or re.search(r"\d", head)):
        return _cap(head)
    cl = head.split(", ")
    dig = next((i for i, c in enumerate(cl) if re.search(r"\d", c)), None)
    out = head
    if dig:
        both = f"{cl[0]}, {cl[dig]}"
        out = f"{cl[0]}: {cl[dig]}" if len(cl[0]) <= 14 else both if len(both) <= n else cl[dig]
    if len(out) > n:
        out = out[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return _cap(out)


def _cap(t: str) -> str:
    return t if not t or t.startswith("bet365") else t[:1].upper() + t[1:]


def card(lines: list[str]) -> str:
    """Caja con lo importante (cita de Telegram)."""
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def fold(lines: list[str], limit: int = 2600) -> str:
    """Bloque plegado: se abre al tocarlo. Ahí va todo, para no perder nada."""
    body = "\n".join(lines)
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    return "<blockquote expandable>📝 <b>Todo el análisis</b> (toca para abrir)\n" + body + "</blockquote>"


def tip_message(v: dict, stake: float, title: str | None = None) -> str:
    """Arriba lo que hace falta para decidir (caja con cuota, apuesta, probabilidad, riesgo y valor, y los motivos en una
    línea); abajo, plegado, TODO lo que enseña la página: señales completas, análisis escrito y la comparación de casas."""
    from .insights import COMP_NAME
    mt = v["match"]
    f = _insight(v)
    p, o = float(v.get("p") or 0), float(v.get("b365") or 0)
    fair = v.get("fair") or (1 / p if p else None)
    ev = v.get("ev") if v.get("ev") is not None else (p * o - 1)
    pro, con = factors(v, n_pro=12, n_con=8, f=f)
    tone = f.get("tone") or (v.get("dossier") or {}).get("tone")
    verdict = f.get("verdict") or (v.get("dossier") or {}).get("verdict")
    head = {"warn": f"🟡 <b>{_e(v['label'])}</b>\n⚠️ {_e(verdict or 'Con cautela')}",
            "bad": f"⛔ <b>{_e(v['label'])}</b>\n⚠️ {_e(verdict or 'Mejor no')}"}.get(tone, f"✅ <b>{_e(v['label'])}</b>")
    rk = "🟢 Riesgo bajo" if p >= 0.62 else "🟡 Riesgo medio" if p >= 0.45 else "🔴 Riesgo alto"
    box = [head,
           f"💶 <b>{_n(o)}</b>  ·  apuesta <b>{f'{stake:g}'.replace('.', ',')} €</b>  ·  cobras <b>{_n(stake * o)} €</b>",
           f"🎲 {ten_dots(p)}  <b>{round(p * 10)}/10</b> ({round(p * 100)} %)",
           f"{rk}  ·  🔒 Confianza {v.get('conf') or 'media'}"]
    if o and p:
        box.append(f"📈 Valor <b>{'+' if ev >= 0 else ''}{round(ev * 100)} %</b>" + (f"  ·  vale hasta <b>{_n(fair * 1.02)}</b>" if fair else ""))
    L = ([title] if title else []) + [
        f"⚽ <b>{_e(mt.get('home_name'))} – {_e(mt.get('away_name'))}</b>",
        f"🏆 {_e(COMP_NAME.get(mt.get('comp'), mt.get('comp') or ''))} · 🕘 {_when(mt['kickoff'])}",
        "", card(box)]
    L += ["<b>👍 Por qué sí</b>"] + ([f"✔️ {_e(short(t))}" for t in pro[:4]] or ["✔️ Nada destacable aparte del precio"])
    if con:
        L += ["", "<b>👎 Ojo con</b>"] + [f"✖️ {_e(short(t))}" for t in con[:2]]
    # todo, plegado
    full = [f"<b>👍 A favor ({len(pro)})</b>"] + ([f"• {_e(t)}" for t in pro] or ["• Nada destacable"])
    full += ["", f"<b>👎 En contra ({len(con)})</b>"] + ([f"• {_e(t)}" for t in con] or ["• Nada en contra"])
    if v.get("explain"):
        full += ["", f"🧩 {_e(v['explain'])}"]
    txt = [t for t in (f.get("text") or []) if t][:6]
    if txt:
        full += ["", "<b>📝 Análisis</b>"] + [_e(t) for t in txt]
    edge = f"bet365 la paga como si saliera {round(100 / o)} de cada 100 veces; nosotros creemos que sale {round(p * 100)}" if o else ""
    if v.get("p_market") is not None:
        edge += f" (las demás casas, sin su margen, {round(v['p_market'] * 100)})"
    if edge:
        edge += "." + (f" A la larga, apostando siempre así, ganas {_n(ev * 10)} € por cada 10 €." if ev > 0 else "")
        full += ["", f"<b>📈 El valor</b>: {edge}"]
    ml = market_line(v)
    if ml:
        full += [f"🏦 {ml}"]
    if fair:
        full += [f"🎯 Merece la pena mientras pague <b>{_n(fair * 1.02)}</b> o más; por debajo, no la cojas."]
    top = "\n".join(L)
    return top + "\n\n" + fold(full, limit=max(800, 3850 - len(top)))


def short_reason(v: dict) -> str:
    """Una línea de motivo (para listas como la de mercados recién abiertos): la señal a favor de más peso."""
    pro, _ = factors(v, 1, 0)
    if pro:
        return pro[0]
    pm = v.get("p_market")
    if pm and v.get("p"):
        return f"las casas la ven en un {round(pm * 100)} % y nosotros en un {round(v['p'] * 100)} %"
    return ""
