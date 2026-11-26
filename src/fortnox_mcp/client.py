"""Fortnox API client for the Fortnox MCP server.

Everything learned the hard way about the Fortnox API lives here, so the tools stay simple:

* Auth: service account (client credentials + TenantId header). No refresh token, nothing
  rotates, concurrent callers can't break each other. Token cached until shortly before expiry.
* Pacing: ~4 requests/s (Fortnox allows 25 per 5 s and exposes no rate headers); 429 backs off.
* Network errors: reads retry; writes retry only if the connection was never made (no duplicate documents).
* Two API families: classic `/3/...` (bodies wrapped in the resource key, Swedish error messages
  with a code) and Warehouse `/api/warehouse/...` (bare JSON, `{"error": ...}` errors, a 404 there
  usually means the PATH is wrong).
* PDFs: send NO Accept header (`Accept: application/pdf` -> HTTP 400 code 1000030) and check the
  bytes start with %PDF.
* Article numbers are URL-encoded with safe='' (numbers contain '/').
* Dry run: inside `dry_run()` writes are recorded instead of sent; reads still happen so the
  preview is built from live data.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import os
import time
import urllib.parse
from typing import Any

import httpx

API = "https://api.fortnox.se"
TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"
MIN_INTERVAL = 0.25
TIMEOUT = 60

_dry: contextvars.ContextVar[list | None] = contextvars.ContextVar("fortnox_dry", default=None)


class FortnoxError(Exception):
    def __init__(self, status: int, message: str, code: Any = None, body: Any = None):
        self.status, self.message, self.code, self.body = status, message, code, body
        super().__init__(f"Fortnox HTTP {status}" + (f" code {code}" if code else "") + f": {message}")


def enc(value: str) -> str:
    """URL-encode one path segment (article numbers contain '/')."""
    return urllib.parse.quote(str(value), safe="")


class Fortnox:
    def __init__(self) -> None:
        self._client_id = os.environ.get("FORTNOX_CLIENT_ID", "").strip()
        self._client_secret = os.environ.get("FORTNOX_CLIENT_SECRET", "").strip()
        self._tenant = os.environ.get("FORTNOX_TENANT_ID", "").strip()
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._pace_lock = asyncio.Lock()
        self._last = 0.0
        self._http = httpx.AsyncClient(timeout=TIMEOUT)

    # ------------------------------------------------------------------ auth + pacing
    async def _access_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            if not self._client_id or not self._client_secret or not self._tenant:
                raise FortnoxError(0, "FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET and FORTNOX_TENANT_ID must be set "
                                      "(see the README: Fortnox service account / client credentials)")
            basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
            r = await self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}", "TenantId": self._tenant},
            )
            if r.status_code != 200:
                raise FortnoxError(r.status_code, f"token endpoint refused: {r.text[:300]}")
            tok = r.json()
            self._token = tok["access_token"]
            self._token_exp = time.time() + int(tok.get("expires_in", 3600)) - 120
            return self._token

    async def _pace(self) -> None:
        async with self._pace_lock:
            wait = self._last + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    # ------------------------------------------------------------------ core request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        raw: bool = False,
        accept_json: bool = True,
        retry_404: int = 0,
        is_write: bool | None = None,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/"):
            path = "/" + path
        if not (path.startswith("/3/") or path.startswith("/api/")):
            raise FortnoxError(0, "path must start with /3/ or /api/ (Fortnox API only)")
        params = {k: v for k, v in (params or {}).items() if v is not None}

        rec = _dry.get()
        writes = (method != "GET") if is_write is None else is_write
        if rec is not None and writes:
            rec.append({"method": method, "path": path, "params": params or None, "body": json})
            return {"dry_run": True}

        url = API + path
        attempts_401 = attempts_429 = attempts_net = attempts_404 = 0
        while True:
            await self._pace()
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            if accept_json:
                headers["Accept"] = "application/json"
            if json is not None:
                headers["Content-Type"] = "application/json"
            try:
                r = await self._http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                # Retry only when the request certainly did not reach Fortnox (connection never made), or for reads.
                # A write that timed out may already have been created: retrying could make a duplicate document.
                never_sent = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                attempts_net += 1
                if attempts_net >= 3 or not (never_sent or method == "GET"):
                    raise FortnoxError(0, f"network error talking to Fortnox ({type(e).__name__}): {e}. "
                                          + ("" if method == "GET" or never_sent else
                                             "The request may have reached Fortnox: check before sending it again."))
                await asyncio.sleep(2 * attempts_net)
                continue
            if r.status_code == 401 and attempts_401 == 0:
                attempts_401 += 1
                await self._access_token(force=True)
                continue
            if r.status_code == 429 and attempts_429 < 5:
                attempts_429 += 1
                await asyncio.sleep(5)
                continue
            if r.status_code == 404 and attempts_404 < retry_404:
                attempts_404 += 1
                await asyncio.sleep(1)
                continue
            if r.status_code >= 400:
                raise self._error(r)
            if raw:
                return r.content
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {"text": r.text[:2000]}

    @staticmethod
    def _error(r: httpx.Response) -> FortnoxError:
        try:
            body = r.json()
        except ValueError:
            return FortnoxError(r.status_code, r.text[:500])
        info = body.get("ErrorInformation") or body.get("errorInformation") if isinstance(body, dict) else None
        if info:
            return FortnoxError(r.status_code, info.get("message") or info.get("Message") or str(info),
                                info.get("code") or info.get("Code"), body)
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error") or str(body)[:500]
            return FortnoxError(r.status_code, msg, body.get("error"), body)
        return FortnoxError(r.status_code, str(body)[:500], None, body)

    # ------------------------------------------------------------------ helpers
    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def pdf(self, path: str) -> bytes:
        data = await self.request("GET", path, raw=True, accept_json=False)
        if not data.startswith(b"%PDF"):
            raise FortnoxError(400, f"Fortnox did not return a PDF: {data[:200]!r}")
        return data

    async def list_all(self, path: str, key: str, max_pages: int = 50, **params: Any) -> list:
        """Page a classic /3/ list endpoint (MetaInformation paging)."""
        out: list = []
        page = 1
        params.setdefault("limit", 500)
        while page <= max_pages:
            res = await self.get(path, page=page, **params)
            out.extend(res.get(key, []) or [])
            meta = res.get("MetaInformation", {})
            if page >= int(meta.get("@TotalPages", 1) or 1):
                break
            page += 1
        return out


@contextlib.contextmanager
def dry_run(enabled: bool):
    """Record writes instead of sending them. Yields the list of recorded requests."""
    if not enabled:
        yield None
        return
    rec: list = []
    token = _dry.set(rec)
    try:
        yield rec
    finally:
        _dry.reset(token)


def gs1_valid(code: str) -> bool:
    """GS1 mod-10 check digit for EAN-8/UPC-12/EAN-13/GTIN-14."""
    if not code or not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check
