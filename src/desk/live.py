"""Network adapters for paper mode. No credential is read or sent.

Reads retry a brief SSL or URL disconnect. An HTTP response is not a
disconnect and is not retried. Three attempts, 0.2s then 0.5s apart.
The failure that remains is LiveRequestError naming the error class.
"""

from __future__ import annotations

import errno
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal

from desk.clob import CLOB_HOST, PyClobBooks, build_readonly_client
from desk.geoblock import GEOBLOCK_URL, GeoblockError

GAMMA_HOST = "https://gamma-api.polymarket.com"
USER_AGENT = "desk-paper/0.1"

# One try plus two retries. The gaps stay short so a blip does not stall the loop.
_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.2, 0.5)

_TRANSIENT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "EAI_AGAIN", None),
    )
    if code is not None
)


class LiveRequestError(Exception):
    pass


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _disconnect_name(exc: BaseException) -> str:
    """Exception class to report. The message text can contain a host, so it is not used."""

    current = exc
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            break
        if isinstance(current, urllib.error.URLError) and isinstance(current.reason, BaseException):
            current = current.reason
            continue
        break
    return type(current).__name__


def _is_httpx_request_error(exc: BaseException) -> bool:
    for cls in type(exc).__mro__:
        if cls.__name__ == "RequestError" and (cls.__module__ or "").split(".")[0] == "httpx":
            return True
    return False


def _is_clob_transport_error(exc: BaseException) -> bool:
    """py-clob-client wraps httpx transport errors with status_code None.

    An HTTP status is a response, including a refusal, and is not retried.
    """

    if type(exc).__name__ != "PolyApiException":
        return False
    return getattr(exc, "status_code", "missing") is None


def is_transient_disconnect(exc: BaseException) -> bool:
    """True for a dropped connection, not for an HTTP answer or a bad certificate."""

    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return is_transient_disconnect(reason)
        return False
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (ssl.SSLError, TimeoutError, ConnectionError, EOFError)):
        return True
    if isinstance(exc, (http.client.IncompleteRead, http.client.BadStatusLine)):
        return True
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    if _is_httpx_request_error(exc) or _is_clob_transport_error(exc):
        return True
    return False


def call_with_retry(operation: Callable[[], object], sleeper: Callable[[float], None] | None = None) -> object:
    """Run `operation` up to three times when the failure is a transient disconnect."""

    pause = _sleep if sleeper is None else sleeper
    for attempt in range(_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            if not is_transient_disconnect(exc):
                raise
            if attempt + 1 == _ATTEMPTS:
                raise LiveRequestError(_disconnect_name(exc)) from exc
            pause(_BACKOFF_SECONDS[attempt])
    raise RuntimeError("retry loop ended without a result")


def http_get_json(
    url: str,
    accept_json: bool = True,
    sleeper: Callable[[float], None] | None = None,
) -> object:
    def once() -> object:
        headers = {"User-Agent": USER_AGENT}
        if accept_json:
            headers["Accept"] = "application/json"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise LiveRequestError(type(exc).__name__) from exc
        except urllib.error.URLError as exc:
            if is_transient_disconnect(exc):
                raise
            raise LiveRequestError(type(exc).__name__) from exc
        return json.loads(body, parse_float=Decimal)

    return call_with_retry(once, sleeper=sleeper)


class LivePorts:
    """Gamma, CLOB market info, geoblock, and a read-only order book."""

    def __init__(self, books: PyClobBooks | None = None) -> None:
        self.books = books if books is not None else PyClobBooks(build_readonly_client())

    def geoblock(self) -> object:
        try:
            return http_get_json(GEOBLOCK_URL)
        except LiveRequestError as exc:
            raise GeoblockError(
                "Refusing to run: Polymarket geoblock check did not succeed."
            ) from exc

    def event(self, slug: str) -> dict | None:
        query = urllib.parse.urlencode({"slug": slug})
        payload = http_get_json(f"{GAMMA_HOST}/events?{query}")
        if isinstance(payload, list):
            if not payload:
                return None
            event = payload[0]
            return event if isinstance(event, dict) else None
        return payload if isinstance(payload, dict) else None

    def clob_market(self, condition_id: str) -> dict:
        payload = http_get_json(f"{CLOB_HOST}/clob-markets/{condition_id}")
        if not isinstance(payload, dict):
            raise LiveRequestError("clob market payload was not an object")
        return payload

    def order_book(self, token_id: str):
        return self.books.order_book(token_id)

    def settled_events(self) -> list:
        rows: list = []
        for offset in (0, 50):
            query = urllib.parse.urlencode(
                {
                    "series_slug": "eth-up-or-down-15m",
                    "closed": "true",
                    "limit": "50",
                    "offset": str(offset),
                    "order": "endDate",
                    "ascending": "false",
                }
            )
            # The history query is sent without an Accept header. A large
            # Gamma response has answered 406 when that header is set.
            payload = http_get_json(f"{GAMMA_HOST}/events?{query}", accept_json=False)
            if not isinstance(payload, list):
                raise LiveRequestError("settled-window payload was not a list")
            rows.extend(payload)
        return rows
