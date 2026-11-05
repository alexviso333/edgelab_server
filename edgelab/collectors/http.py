"""Cliente HTTP educado: límite de ritmo por dominio, reintentos con espera,
detección de bloqueos de Cloudflare y caché en SQLite.

Usa curl_cffi (imita la huella TLS de Chrome) si está instalado; si no, httpx.
"""
from __future__ import annotations

import gzip
import json
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ..core import db

try:  # curl_cffi pasa mucho mejor los filtros de Cloudflare
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover
    cffi_requests = None
import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/154.0.0.0 Safari/537.36")


class Blocked(Exception):
    """La fuente nos ha bloqueado (403/429 persistente o reto de Cloudflare)."""


class PoliteClient:
    def __init__(self, min_interval: float = 2.0, jitter: float = 0.7, max_retries: int = 4,
                 headers: dict | None = None, impersonate: str = "chrome", timeout: float = 30.0):
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_retries = max_retries
        self.headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
        if headers:
            self.headers.update(headers)
        self.impersonate = impersonate
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._session = None
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "blocked": 0}

    def _sess(self):
        if self._session is None:
            if cffi_requests is not None:
                self._session = cffi_requests.Session(impersonate=self.impersonate)
            else:
                self._session = httpx.Client(follow_redirects=True, timeout=self.timeout)
        return self._session

    def _throttle(self, host: str) -> None:
        with self._lock:
            now = time.time()
            wait = self._last.get(host, 0) + self.min_interval + random.uniform(0, self.jitter) - now
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()

    @staticmethod
    def _cache_get(url: str, ttl: float | None):
        if not ttl:
            return None
        r = db.q1("SELECT ts, status, body FROM http_cache WHERE url=?", (url,))
        if r and time.time() - r["ts"] < ttl and r["status"] == 200:
            return gzip.decompress(r["body"]).decode("utf-8")
        return None

    @staticmethod
    def _cache_put(url: str, status: int, text: str) -> None:
        db.execute("INSERT INTO http_cache(url,ts,status,body) VALUES(?,?,?,?) "
                   "ON CONFLICT(url) DO UPDATE SET ts=excluded.ts,status=excluded.status,body=excluded.body",
                   (url, int(time.time()), status, gzip.compress(text.encode("utf-8"))))

    def get_text(self, url: str, params: dict | None = None, ttl: float | None = None,
                 headers: dict | None = None) -> str:
        full = url
        if params:
            from urllib.parse import urlencode
            full = url + ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        cached = self._cache_get(full, ttl)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        host = urlparse(full).netloc
        h = dict(self.headers)
        if headers:
            h.update(headers)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                self.stats["requests"] += 1
                if cffi_requests is not None:
                    r = self._sess().get(full, headers=h, timeout=self.timeout)
                else:
                    r = self._sess().get(full, headers=h)
                status = r.status_code
                text = r.text
            except Exception as e:  # red
                last_err = e
                self.stats["errors"] += 1
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            if status == 200:
                if "Just a moment" in text[:3000] and "<html" in text[:200].lower():
                    self.stats["blocked"] += 1
                    last_err = Blocked(f"reto de Cloudflare en {host}")
                    time.sleep(min(120, 10 * (attempt + 1)))
                    continue
                if ttl:
                    self._cache_put(full, status, text)
                return text
            if status in (429, 503, 403, 520, 522, 524):
                self.stats["blocked"] += 1
                retry_after = 0.0
                try:
                    retry_after = float(r.headers.get("Retry-After", 0))
                except Exception:
                    pass
                time.sleep(max(retry_after, min(180, 5 * 2 ** attempt)) + random.random())
                last_err = Blocked(f"HTTP {status} en {host}")
                continue
            if status == 404:
                raise FileNotFoundError(full)
            last_err = RuntimeError(f"HTTP {status} {full}")
            time.sleep(2 ** attempt)
        raise last_err or RuntimeError(full)

    def get_json(self, url: str, params: dict | None = None, ttl: float | None = None,
                 headers: dict | None = None) -> Any:
        return json.loads(self.get_text(url, params=params, ttl=ttl, headers=headers))
