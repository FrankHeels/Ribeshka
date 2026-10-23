"""A synchronous, bounded interface to the fixed MoySklad JSON API."""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json as json_module
import logging
import math
import re

import httpx
from django.conf import settings

from .config import get_config
from .contracts import MoySkladError
from .request_guard import ensure_request_task
from .throttling import ApiThrottle


API_BASE = "https://api.moysklad.ru/api/remap/1.2/"
TOTAL_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RESOURCE = re.compile(
    rf"(?:entity/(?:product|uom|pricetype|currency|organization|counterparty|webhook)(?:/{_UUID})?"
    rf"|entity/customerorder(?:/{_UUID}(?:/positions(?:/{_UUID})?)?|/syncid/{_UUID}"
    rf"|/metadata(?:/states(?:/{_UUID})?)?)?|context/companysettings(?:/pricetype(?:/{_UUID})?)?)\Z"
)
_WRITABLE = re.compile(rf"entity/(?:customerorder|webhook)(?:/{_UUID})?\Z")
_DELETABLE = re.compile(rf"entity/customerorder/{_UUID}\Z")
_PARAMETERS = frozenset(("limit", "offset", "filter", "expand", "order"))


def _suppress_http_logging():
    # HTTPX logs URLs and httpcore debug traces can contain request headers.
    # This boundary emits only safe MoySkladError codes to its caller.
    names = {"httpx", "httpcore"} | {
        name for name in logging.Logger.manager.loggerDict
        if name.startswith(("httpx.", "httpcore."))
    }
    for name in names:
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.setLevel(logging.CRITICAL + 1)


def _json_number(value):
    """Encode Decimal as a JSON number without an intermediate float."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return json_module.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError
        return "{" + ",".join(
            f"{json_module.dumps(key, ensure_ascii=False)}:{_json_number(item)}"
            for key, item in value.items()
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_number(item) for item in value) + "]"
    raise ValueError


def _parameters(params):
    if params is None:
        return None
    if not isinstance(params, Mapping) or any(key not in _PARAMETERS for key in params):
        raise MoySkladError("invalid_parameters")
    result = {}
    for key, value in params.items():
        if key in ("limit", "offset"):
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise MoySkladError("invalid_parameters")
            if not re.fullmatch(r"[0-9]+", str(value)):
                raise MoySkladError("invalid_parameters")
            number = int(value)
            if number < (1 if key == "limit" else 0) or (key == "limit" and number > 1000):
                raise MoySkladError("invalid_parameters")
            result[key] = number
        elif not isinstance(value, str) or not value or len(value) > 4096 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise MoySkladError("invalid_parameters")
        else:
            result[key] = value
    return result


def _retry_after(headers):
    delays = []
    for key in ("X-Lognex-Retry-After", "X-Lognex-Reset"):
        try:
            delay = float(headers[key]) / 1000
            if math.isfinite(delay) and 0 <= delay <= 31536000:
                delays.append(delay)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    if not delays and "Retry-After" in headers:
        try:
            delay = float(headers["Retry-After"])
        except (ValueError, OverflowError):
            try:
                reset = parsedate_to_datetime(headers["Retry-After"])
                delay = (reset - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 0
        if math.isfinite(delay) and 0 <= delay <= 31536000:
            delays.append(delay)
    return max(delays, default=1.0)


class MoySkladClient:
    def __init__(self, config=None, transport=None, readonly_probe=False, webhook_management=False):
        if not isinstance(readonly_probe, bool) or not isinstance(webhook_management, bool):
            raise MoySkladError("invalid_configuration")
        self.readonly_probe = readonly_probe
        self.webhook_management = webhook_management
        self.config = get_config(config)
        if getattr(settings, "MOYSKLAD_TEST_MODE", False) and not isinstance(
            transport, httpx.AsyncBaseTransport,
        ):
            raise MoySkladError("test_transport_required")
        if not self.config.api_token:
            raise MoySkladError("missing_api_token")
        _suppress_http_logging()
        self._throttle = None if transport is not None else ApiThrottle(self.config)
        self._runner = asyncio.Runner()
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self.config.api_token}",
                "Accept": "application/json;charset=utf-8",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json;charset=utf-8",
            },
            timeout=httpx.Timeout(connect=5.0, pool=5.0, read=20.0, write=20.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            verify=True, follow_redirects=False, trust_env=False, transport=transport,
        )
        self._closed = False

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, *, json):
        return self._request("POST", path, json=json)

    def put(self, path, *, json):
        return self._request("PUT", path, json=json)

    def delete(self, path):
        return self._request("DELETE", path)

    def _request(self, method, path, *, params=None, json=None):
        if self._closed:
            raise MoySkladError("client_closed")
        if self.readonly_probe and method != "GET":
            raise MoySkladError("readonly_probe")
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise MoySkladError("invalid_resource")
        if not isinstance(path, str) or not _RESOURCE.fullmatch(path):
            raise MoySkladError("invalid_resource")
        if method != "GET" and not _WRITABLE.fullmatch(path):
            raise MoySkladError("invalid_resource")
        if method == "DELETE" and (not _DELETABLE.fullmatch(path) or params is not None or json is not None):
            raise MoySkladError("invalid_resource")
        if method != "GET" and path.startswith("entity/webhook") and not self.webhook_management:
            raise MoySkladError("webhook_management_required")
        params = _parameters(params)
        try:
            content = None if method in ("GET", "DELETE") else _json_number(json).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise MoySkladError("invalid_payload") from None
        token = self._throttle.acquire(access_probe=self.readonly_probe) if self._throttle else None
        try:
            if self._throttle:
                self._throttle.heartbeat(token)
            ensure_request_task()
            try:
                status, headers, body = self._runner.run(self._send(method, path, params, content))
            except (httpx.TimeoutException, TimeoutError):
                raise MoySkladError("timeout", retryable=True) from None
            except httpx.HTTPError:
                raise MoySkladError("network_error", retryable=True) from None
            if self._throttle:
                self._throttle.heartbeat(token)
            if status in (401, 403):
                if self._throttle:
                    self._throttle.pause_access()
                raise MoySkladError("access_denied", status_code=status)
            if status == 429:
                delay = _retry_after(headers)
                if self._throttle:
                    self._throttle.defer(delay)
                raise MoySkladError("rate_limited", retryable=True, retry_after=delay, status_code=status)
            if status >= 500:
                raise MoySkladError("server_error", retryable=True, status_code=status)
            if status == 404:
                raise MoySkladError("not_found", status_code=status)
            if status < 200 or status >= 300:
                raise MoySkladError("validation_error" if status == 400 else "http_error", status_code=status)
            if not body and (status == 204 or (method == "DELETE" and status == 200)):
                return None
            try:
                return json_module.loads(body, parse_float=Decimal, parse_constant=self._invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise MoySkladError("invalid_json") from None
        finally:
            if self._throttle:
                self._throttle.release(token)

    @staticmethod
    def _invalid_constant(value):
        raise ValueError

    async def _send(self, method, path, params, content):
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with self._client.stream(method, path, params=params, content=content) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise MoySkladError("response_too_large")
                    body.extend(chunk)
                return response.status_code, response.headers, bytes(body)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._runner.run(self._client.aclose())
        except httpx.HTTPError:
            raise MoySkladError("close_failed") from None
        finally:
            self._runner.close()

    def __enter__(self):
        if self._closed:
            raise MoySkladError("client_closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
