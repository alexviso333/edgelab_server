"""Alertas por Telegram (gratis). Crea un bot con @BotFather, pon TELEGRAM_TOKEN
y TELEGRAM_CHAT_ID en .env. Si no hay token, las alertas quedan solo en la web."""
from __future__ import annotations

import json
import time

import httpx

from .config import settings
from .core import db


def tg_conf() -> tuple[str, str]:
    """Token y chat: los de .env o los que el usuario guardó desde Ajustes."""
    kv = db.kv_get("telegram") or {}
    return (settings.telegram_token or kv.get("token") or "", settings.telegram_chat_id or kv.get("chat_id") or "")


def send_telegram(text: str, _retry: bool = True) -> bool:
    token, chat = tg_conf()
    if not token or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                             "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        _tg_error(f"sin conexión con Telegram: {e}")
        return False
    if r.status_code == 200:
        return True
    try:
        d = r.json()
    except Exception:
        d = {}
    # al quitar permisos (o pasar de 200 miembros) Telegram convierte el grupo en supergrupo y le cambia el número:
    # se coge el nuevo y se reintenta
    new = (d.get("parameters") or {}).get("migrate_to_chat_id")
    if new and _retry:
        kv = db.kv_get("telegram") or {}
        db.kv_set("telegram", {**kv, "chat_id": str(new)})
        _tg_error(f"el grupo pasó a supergrupo: nuevo chat {new}", force=True)
        return send_telegram(text, _retry=False)
    if _retry and "parse" in str(d.get("description", "")).lower():
        import re as _re
        return send_telegram(_re.sub(r"<[^>]+>", "", text), _retry=False)     # HTML que Telegram no acepta: sin formato
    _tg_error(f"{r.status_code}: {d.get('description') or r.text[:200]}")
    return False


def _tg_error(msg: str, force: bool = False) -> None:
    """Deja constancia (como mucho una vez cada 10 min) de por qué no llegan los mensajes."""
    last = db.kv_get("tg_err_ts") or 0
    if force or time.time() - float(last) > 600:
        db.kv_set("tg_err_ts", time.time())
        db.kv_set("tg_last_error", {"ts": int(time.time()), "msg": msg})
        db.log_job("telegram", "error", msg)


def alert(kind: str, title: str, message: str, match_id: int | None = None, strategy_id: str | None = None,
          payload: dict | None = None, dedupe: str | None = None, push: bool = True) -> int | None:
    """Guarda la alerta (deduplicada) y la envía por Telegram."""
    try:
        aid = db.execute("INSERT INTO alerts(ts,match_id,strategy_id,kind,title,message,payload,dedupe) VALUES(?,?,?,?,?,?,?,?)",
                         (int(time.time()), match_id, strategy_id, kind, title, message,
                          json.dumps(payload or {}, default=str), dedupe))
    except Exception:
        return None  # ya enviada (dedupe)
    sent = send_telegram(f"<b>{title}</b>\n{message}") if push else False
    if sent:
        db.execute("UPDATE alerts SET sent=1 WHERE id=?", (aid,))
    return aid
