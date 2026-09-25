from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .math import D


class PipKalshiError(RuntimeError):
    pass


@dataclass(frozen=True)
class KalshiEnvironment:
    name: str
    host: str
    ws_url: str


ENVIRONMENTS = {
    "demo": KalshiEnvironment(
        "demo",
        "https://external-api.demo.kalshi.co",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
    "production": KalshiEnvironment(
        "production",
        "https://external-api.kalshi.com",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
}


class PipKalshiClient:
    """Small current Kalshi V2 client dedicated to Pip.

    Public market reads work without credentials. Authenticated portfolio/order calls
    require a matching API key + private key. Write requests are never automatically
    retried, avoiding accidental duplicate orders after ambiguous failures.
    """

    def __init__(self, env: str | None = None, timeout: float = 20.0):
        env_name = (env or os.getenv("KALSHI_ENV", "demo")).lower()
        if env_name == "live":
            env_name = "production"
        if env_name not in ENVIRONMENTS:
            raise PipKalshiError(f"Unsupported KALSHI_ENV: {env_name}")
        self.environment = ENVIRONMENTS[env_name]
        self.api_key = os.getenv("KALSHI_API_KEY_ID") or os.getenv("KALSHI_API_KEY") or ""
        self.private_key = self._load_private_key_optional()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key and self.private_key)

    @property
    def ws_url(self) -> str:
        return self.environment.ws_url

    def _load_private_key_optional(self):
        raw = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").strip()
        path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if raw:
            raw = raw.replace("\\n", "\n")
            data = raw.encode()
        elif path:
            p = Path(path)
            if not p.exists():
                raise PipKalshiError(f"KALSHI_PRIVATE_KEY_PATH not found: {path}")
            data = p.read_bytes()
        else:
            return None
        try:
            return serialization.load_pem_private_key(data, password=None)
        except Exception as exc:
            raise PipKalshiError(f"Could not load Kalshi private key: {exc}") from exc

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _signature(self, timestamp_ms: str, method: str, path: str) -> str:
        if not self.private_key:
            raise PipKalshiError("Authenticated request requires Kalshi credentials")
        payload = f"{timestamp_ms}{method.upper()}{path}".encode()
        key = self.private_key
        if isinstance(key, rsa.RSAPrivateKey):
            sig = key.sign(
                payload,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        elif isinstance(key, ed25519.Ed25519PrivateKey):
            sig = key.sign(payload)
        else:
            raise PipKalshiError(f"Unsupported private key type: {type(key).__name__}")
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            raise PipKalshiError("Kalshi API key/private key not configured")
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._signature(ts, method, path),
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()
        if not path.startswith("/trade-api/v2/"):
            raise PipKalshiError(f"Unexpected API path: {path}")
        url = self.environment.host + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers(method, path))
        sess = await self.session()

        attempts = 3 if method == "GET" else 1
        last_error = None
        for attempt in range(attempts):
            try:
                async with sess.request(method, url, headers=headers, json=body) as resp:
                    text = await resp.text()
                    if 200 <= resp.status < 300:
                        return json.loads(text) if text else {}
                    err = PipKalshiError(f"Kalshi HTTP {resp.status}: {text[:500]}")
                    if method == "GET" and (resp.status == 429 or resp.status >= 500) and attempt + 1 < attempts:
                        last_error = err
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    raise err
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if method != "GET" or attempt + 1 >= attempts:
                    raise PipKalshiError(f"Kalshi request failed: {exc}") from exc
                await asyncio.sleep(0.4 * (2 ** attempt))
        raise PipKalshiError(f"Kalshi request failed: {last_error}")

    async def get_markets(self, *, limit: int = 1000, cursor: str | None = None, status: str = "open"):
        return await self.request(
            "GET", "/trade-api/v2/markets", params={"limit": limit, "cursor": cursor, "status": status}
        )

    async def iter_open_markets(self, max_pages: int = 20):
        cursor = None
        for _ in range(max_pages):
            data = await self.get_markets(cursor=cursor)
            for market in data.get("markets", []):
                yield market
            cursor = data.get("cursor")
            if not cursor:
                return

    async def get_market(self, ticker: str):
        return await self.request("GET", f"/trade-api/v2/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 20):
        return await self.request(
            "GET", f"/trade-api/v2/markets/{ticker}/orderbook", params={"depth": depth}
        )

    async def get_balance(self):
        return await self.request("GET", "/trade-api/v2/portfolio/balance", auth=True)

    async def get_positions(self):
        return await self.request("GET", "/trade-api/v2/portfolio/positions", auth=True)

    async def get_orders(self, status: str | None = None):
        return await self.request(
            "GET", "/trade-api/v2/portfolio/orders", params={"status": status}, auth=True
        )

    async def get_order(self, order_id: str):
        return await self.request(
            "GET", f"/trade-api/v2/portfolio/events/orders/{order_id}", auth=True
        )

    async def create_order_v2(
        self,
        *,
        ticker: str,
        client_order_id: str,
        book_side: str,
        count: int,
        yes_price: Decimal,
        post_only: bool = False,
        time_in_force: str = "good_till_canceled",
    ):
        if book_side not in {"bid", "ask"}:
            raise PipKalshiError("book_side must be bid or ask")
        if not (Decimal("0.01") <= yes_price <= Decimal("0.99")):
            raise PipKalshiError(f"Invalid YES-side price: {yes_price}")
        if count <= 0:
            raise PipKalshiError("count must be positive")
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": str(count),
            "price": format(yes_price.quantize(Decimal("0.01")), "f"),
            "time_in_force": time_in_force,
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
            "self_trade_prevention_type": "taker_at_cross",
        }
        return await self.request(
            "POST", "/trade-api/v2/portfolio/events/orders", body=body, auth=True
        )

    async def cancel_order_v2(self, order_id: str, market_ticker: str):
        return await self.request(
            "DELETE",
            f"/trade-api/v2/portfolio/events/orders/{order_id}",
            params={"market_ticker": market_ticker},
            auth=True,
        )


def fp(value: Any, default: str = "0") -> Decimal:
    """Read Kalshi fixed-point fields safely."""
    if value is None or value == "":
        return D(default)
    return D(value)
