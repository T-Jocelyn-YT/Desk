"""Retries for brief SSL and URL disconnects. Permanent answers are not retried."""

import http.client
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage

from desk.app import DeskApp
from desk.clob import PyClobBooks
from desk.geoblock import GEOBLOCK_URL
from desk.journal import Journal
from desk.__main__ import main
from desk.live import GAMMA_HOST, LiveRequestError, http_get_json
from desk.twap import TwapTick
from tests.support import Book, FakePorts, clob_info, eth_event, settled_series

WINDOW = 1_700_002_800
INSIDE = datetime.fromtimestamp(WINDOW + 900 - 120, tz=timezone.utc)
DOC_IP = "203.0.113.42"


class _Response:
    def __init__(self, body: bytes, read_error: BaseException | None = None) -> None:
        self._body = body
        self._read_error = read_error

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "no", EmailMessage(), None)


def _ssl_url_error() -> urllib.error.URLError:
    return urllib.error.URLError(ssl.SSLEOFError("EOF occurred in violation of protocol"))


def _paper_ports() -> FakePorts:
    ports = FakePorts()
    event = eth_event(WINDOW, price_to_beat="1000", condition="condition-current")
    ports.events[event["slug"]] = event
    ports.clob["condition-current"] = clob_info()
    # 5 shares at 20¢ is $1, so the per-bet cap still allows the fill.
    ports.books["token-up"] = Book([{"price": "0.20", "size": "20"}])
    ports.books["token-down"] = Book([{"price": "0.90", "size": "20"}])
    ports.settled = settled_series(WINDOW)
    return ports


def _bind_gamma(monkeypatch, ports: FakePorts, fail_on: set[int]) -> dict:
    """Serve Gamma through http_get_json. `fail_on` are 1-based slug attempts."""

    state = {"slug_calls": 0, "sleeps": []}

    def urlopen(request, timeout=15):
        state["slug_calls"] += 1
        if state["slug_calls"] in fail_on:
            raise _ssl_url_error()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        slug = query.get("slug", [""])[0]
        if slug in ports.events:
            body = json.dumps([ports.events[slug]]).encode()
        else:
            body = b"[]"
        return _Response(body)

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("desk.live._sleep", lambda seconds: state["sleeps"].append(seconds))

    def event(slug: str):
        query = urllib.parse.urlencode({"slug": slug})
        payload = http_get_json(f"{GAMMA_HOST}/events?{query}")
        if isinstance(payload, list):
            if not payload:
                return None
            found = payload[0]
            return found if isinstance(found, dict) else None
        return payload if isinstance(payload, dict) else None

    ports.event = event
    return state


def _app(tmp_path, ports: FakePorts) -> DeskApp:
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "FREEZE")
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=int(INSIDE.timestamp()) * 1000))
    return app


class _Script:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.slug_calls = 0
        self.fail_slug_on: set[int] = set()
        self.gamma_status: int | None = None
        self.geoblock_steps: list[str] = ["ok"]
        self.sleeps: list[float] = []
        self.rtds_started = False
        self.book_errors = 0

    def urlopen(self, request, timeout=15):
        url = request.full_url
        self.calls.append(url)
        if url.startswith(GEOBLOCK_URL):
            return self._geoblock(url)
        if "series_slug=" in url:
            return _Response(b"[]")
        if url.startswith(GAMMA_HOST):
            return self._gamma(url)
        if "/clob-markets/" in url:
            return _Response(json.dumps(clob_info()).encode())
        raise AssertionError(url)

    def _geoblock(self, url: str):
        step = self.geoblock_steps.pop(0)
        if step == "ssl":
            raise _ssl_url_error()
        if step == "http":
            raise _http_error(url, 403)
        blocked = step == "blocked"
        payload = {
            "blocked": blocked,
            "ip": DOC_IP,
            "country": "XX",
            "region": "YY",
        }
        return _Response(json.dumps(payload).encode())

    def _gamma(self, url: str):
        self.slug_calls += 1
        if self.gamma_status is not None:
            raise _http_error(url, self.gamma_status)
        if self.slug_calls in self.fail_slug_on:
            raise _ssl_url_error()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        slug = query["slug"][0]
        start = int(slug.rsplit("-", 1)[-1])
        body = json.dumps(
            [eth_event(start, price_to_beat="1000", condition="condition-current")]
        ).encode()
        return _Response(body)

    def client(self):
        script = self

        class _Client:
            def get_order_book(self, token_id: str):
                if script.book_errors > 0:
                    script.book_errors -= 1
                    raise ssl.SSLEOFError("EOF occurred in violation of protocol")
                return Book([{"price": "0.40", "size": "20"}])

        return _Client()


def _install_main(monkeypatch, tmp_path, script: _Script) -> _Script:
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("DESK_MODE", "paper")
    monkeypatch.setenv("DESK_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DESK_FREEZE_FILE", str(tmp_path / "FREEZE"))
    monkeypatch.setattr("desk.live.urllib.request.urlopen", script.urlopen)
    monkeypatch.setattr("desk.live._sleep", lambda seconds: script.sleeps.append(seconds))
    monkeypatch.setattr("desk.live.build_readonly_client", lambda *args, **kwargs: script.client())

    def start_rtds(book):
        script.rtds_started = True
        return None, threading.Event()

    monkeypatch.setattr("desk.__main__.start_rtds", start_rtds)
    return script


def _combined(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _assert_no_address(text: str) -> None:
    assert DOC_IP not in text
    assert "Traceback" not in text
    assert "https://" not in text
    assert "EOF occurred" not in text


def test_gamma_urlerror_succeeds_on_the_second_attempt(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def urlopen(request, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ssl_url_error()
        assert "eth-updown-15m-1" in request.full_url
        return _Response(b'[{"slug": "eth-updown-15m-1", "ok": true}]')

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    payload = http_get_json(
        f"{GAMMA_HOST}/events?slug=eth-updown-15m-1",
        sleeper=lambda seconds: sleeps.append(seconds),
    )
    assert payload == [{"slug": "eth-updown-15m-1", "ok": True}]
    assert calls["n"] == 2
    assert sleeps == [0.2]


def test_raw_ssl_eof_during_the_body_read_is_retried(monkeypatch):
    calls = {"n": 0}

    def urlopen(request, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response(b"", ssl.SSLEOFError("EOF occurred in violation of protocol"))
        return _Response(b'{"blocked": false}')

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    assert http_get_json("https://gamma-api.polymarket.com/events", sleeper=lambda _seconds: None) == {
        "blocked": False
    }
    assert calls["n"] == 2


def test_incomplete_read_is_retried(monkeypatch):
    calls = {"n": 0}

    def urlopen(request, timeout=15):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.IncompleteRead(b"partial", 20)
        return _Response(b"[1]")

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    assert http_get_json("https://gamma-api.polymarket.com/events", sleeper=lambda _seconds: None) == [1]
    assert calls["n"] == 3


def test_exhausted_ssl_disconnect_raises_live_request_error(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def urlopen(request, timeout=15):
        calls["n"] += 1
        raise _ssl_url_error()

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    url = f"{GAMMA_HOST}/events?slug=eth-updown-15m-9"
    try:
        http_get_json(url, sleeper=lambda seconds: sleeps.append(seconds))
    except LiveRequestError as exc:
        assert str(exc) == "SSLEOFError"
        assert url not in str(exc)
        assert isinstance(exc.__cause__, urllib.error.URLError)
        assert isinstance(exc.__cause__.reason, ssl.SSLEOFError)
    else:
        raise AssertionError("exhausted retries returned a payload")
    assert calls["n"] == 3
    assert sleeps == [0.2, 0.5]


def test_http_status_is_not_retried(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def urlopen(request, timeout=15):
        calls["n"] += 1
        raise _http_error(request.full_url, 404)

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    try:
        http_get_json(f"{GAMMA_HOST}/events?slug=missing", sleeper=lambda seconds: sleeps.append(seconds))
    except LiveRequestError as exc:
        assert str(exc) == "HTTPError"
    else:
        raise AssertionError("HTTP 404 was treated as success")
    assert calls["n"] == 1
    assert sleeps == []


def test_permanent_url_error_and_certificate_failure_are_not_retried(monkeypatch):
    calls = {"n": 0}

    def unknown(request, timeout=15):
        calls["n"] += 1
        raise urllib.error.URLError("unknown url type: ftp")

    monkeypatch.setattr("desk.live.urllib.request.urlopen", unknown)
    try:
        http_get_json("ftp://example.invalid/events", sleeper=lambda _seconds: None)
    except LiveRequestError as exc:
        assert str(exc) == "URLError"
    else:
        raise AssertionError("permanent URL error was retried into success")
    assert calls["n"] == 1

    def certificate(request, timeout=15):
        calls["n"] += 1
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    monkeypatch.setattr("desk.live.urllib.request.urlopen", certificate)
    try:
        http_get_json("https://gamma-api.polymarket.com/events", sleeper=lambda _seconds: None)
    except LiveRequestError as exc:
        assert str(exc) == "URLError"
    else:
        raise AssertionError("certificate failure was retried into success")
    assert calls["n"] == 2


def test_malformed_json_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def urlopen(request, timeout=15):
        calls["n"] += 1
        return _Response(b"not-json")

    monkeypatch.setattr("desk.live.urllib.request.urlopen", urlopen)
    try:
        http_get_json("https://gamma-api.polymarket.com/events", sleeper=lambda _seconds: None)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed JSON was accepted")
    assert calls["n"] == 1


def test_tick_survives_a_gamma_disconnect_and_still_fills(tmp_path, monkeypatch):
    ports = _paper_ports()
    state = _bind_gamma(monkeypatch, ports, fail_on={2})
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    app.tick(INSIDE)
    fill = json.loads((tmp_path / "fills.jsonl").read_text(encoding="utf-8"))
    assert fill["mode"] == "paper"
    assert fill["side"] == "Up"
    assert fill["shares"] == "5"
    assert fill["price"] == "0.20"
    assert state["slug_calls"] >= 3
    assert state["sleeps"] == [0.2]
    assert DOC_IP not in (tmp_path / "fills.jsonl").read_text(encoding="utf-8")


def test_exhausted_gamma_disconnect_on_tick_is_a_live_request_error(tmp_path, monkeypatch):
    ports = _paper_ports()
    state = _bind_gamma(monkeypatch, ports, fail_on={2, 3, 4})
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    try:
        app.tick(INSIDE)
    except LiveRequestError as exc:
        assert str(exc) == "SSLEOFError"
        assert not isinstance(exc, ssl.SSLError)
    else:
        raise AssertionError("tick swallowed an exhausted Gamma disconnect")
    assert state["slug_calls"] == 4
    assert state["sleeps"] == [0.2, 0.5]
    assert not (tmp_path / "fills.jsonl").exists()


def test_order_book_disconnect_retries_then_returns_the_book(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    class Client:
        def get_order_book(self, token_id: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ssl.SSLEOFError("EOF occurred in violation of protocol")
            return Book([{"price": "0.40", "size": "20"}])

    monkeypatch.setattr("desk.live._sleep", lambda seconds: sleeps.append(seconds))
    book = PyClobBooks(Client()).order_book("token-up")
    assert book.min_order_size == "5"
    assert calls["n"] == 2
    assert sleeps == [0.2]


def test_exhausted_order_book_disconnect_raises_live_request_error(monkeypatch):
    calls = {"n": 0}

    class Client:
        def get_order_book(self, token_id: str):
            calls["n"] += 1
            raise ssl.SSLEOFError("EOF occurred in violation of protocol")

    monkeypatch.setattr("desk.live._sleep", lambda _seconds: None)
    try:
        PyClobBooks(Client()).order_book("token-up")
    except LiveRequestError as exc:
        assert str(exc) == "SSLEOFError"
        assert isinstance(exc.__cause__, ssl.SSLEOFError)
    else:
        raise AssertionError("order book disconnect was not reported")
    assert calls["n"] == 3


def test_clob_http_status_is_not_retried(monkeypatch):
    calls = {"n": 0}

    class PolyApiException(Exception):
        def __init__(self) -> None:
            super().__init__("status")
            self.status_code = 400
            self.error_msg = "status"

    class Client:
        def get_order_book(self, token_id: str):
            calls["n"] += 1
            raise PolyApiException()

    def fail_if_slept(seconds: float) -> None:
        raise AssertionError(f"slept {seconds}")

    monkeypatch.setattr("desk.live._sleep", fail_if_slept)
    try:
        PyClobBooks(Client()).order_book("token-up")
    except PolyApiException:
        pass
    else:
        raise AssertionError("CLOB HTTP status was retried into success")
    assert calls["n"] == 1


def test_clob_transport_error_and_httpx_request_error_are_retried(monkeypatch):
    calls = {"n": 0}

    class PolyApiException(Exception):
        def __init__(self) -> None:
            super().__init__("Request exception!")
            self.status_code = None
            self.error_msg = "Request exception!"

    class RequestError(Exception):
        pass

    class ConnectError(RequestError):
        pass

    RequestError.__module__ = "httpx"
    ConnectError.__module__ = "httpx"

    class Client:
        def get_order_book(self, token_id: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PolyApiException()
            if calls["n"] == 2:
                raise ConnectError("eof")
            return {"asks": []}

    monkeypatch.setattr("desk.live._sleep", lambda _seconds: None)
    assert PyClobBooks(Client()).order_book("token-up") == {"asks": []}
    assert calls["n"] == 3


def test_startup_ssl_blip_does_not_exit_the_process(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.fail_slug_on = {1}
    assert main(["--once"]) == 0
    text = _combined(capsys)
    _assert_no_address(text)
    startup = (tmp_path / "logs" / "startup.jsonl").read_text(encoding="utf-8")
    assert '"mode":"paper"' in startup
    assert DOC_IP not in startup
    assert script.sleeps == [0.2]
    assert script.rtds_started is True
    assert script.slug_calls >= 2


def test_tick_ssl_blip_does_not_exit_the_process(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.fail_slug_on = {2}
    assert main(["--once"]) == 0
    _assert_no_address(_combined(capsys))
    assert script.sleeps == [0.2]
    assert script.slug_calls >= 3


def test_exhausted_tick_disconnect_exits_with_code_6(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.fail_slug_on = {2, 3, 4}
    assert main(["--once"]) == 6
    text = _combined(capsys)
    assert "network request failed: SSLEOFError" in text
    _assert_no_address(text)
    assert script.sleeps == [0.2, 0.5]
    assert script.slug_calls == 4


def test_exhausted_startup_disconnect_exits_with_code_6(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.fail_slug_on = {1, 2, 3}
    assert main(["--once"]) == 6
    text = _combined(capsys)
    assert "network request failed: SSLEOFError" in text
    _assert_no_address(text)
    assert script.rtds_started is False
    assert script.slug_calls == 3
    assert script.sleeps == [0.2, 0.5]


def test_exhausted_order_book_disconnect_exits_with_code_6(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.book_errors = 3
    assert main(["--once"]) == 6
    text = _combined(capsys)
    assert "network request failed: SSLEOFError" in text
    _assert_no_address(text)
    assert script.sleeps == [0.2, 0.5]
    assert script.rtds_started is False


def test_gamma_http_status_exits_without_a_retry(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.gamma_status = 404
    assert main(["--once"]) == 6
    text = _combined(capsys)
    assert "network request failed: HTTPError" in text
    _assert_no_address(text)
    assert script.slug_calls == 1
    assert script.sleeps == []


def test_geoblock_refusal_after_a_disconnect_still_refuses(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.geoblock_steps = ["ssl", "blocked"]
    assert main(["--once"]) == 2
    text = _combined(capsys)
    assert "blocked" in text
    assert "network request failed" not in text
    _assert_no_address(text)
    assert script.sleeps == [0.2]
    assert script.slug_calls == 0
    assert script.rtds_started is False


def test_geoblock_http_error_is_a_refusal_without_retry(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.geoblock_steps = ["http"]
    assert main(["--once"]) == 2
    text = _combined(capsys)
    assert "did not succeed" in text
    assert "network request failed" not in text
    _assert_no_address(text)
    assert script.sleeps == []
    assert sum(1 for url in script.calls if url.startswith(GEOBLOCK_URL)) == 1


def test_exhausted_geoblock_disconnect_is_still_a_refusal(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    script.geoblock_steps = ["ssl", "ssl", "ssl"]
    assert main(["--once"]) == 2
    text = _combined(capsys)
    assert "did not succeed" in text
    assert "network request failed" not in text
    _assert_no_address(text)
    assert script.sleeps == [0.2, 0.5]
    assert script.rtds_started is False


def test_freeze_stops_before_any_retry(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    freeze = tmp_path / "FREEZE"
    freeze.write_text("stop\n", encoding="utf-8")
    assert main([]) == 3
    text = _combined(capsys)
    assert "FREEZE" in text
    assert script.calls == []
    assert script.sleeps == []


def test_live_request_still_exits_without_a_network_call(tmp_path, monkeypatch, capsys):
    script = _install_main(monkeypatch, tmp_path, _Script())
    monkeypatch.setenv("MODE", "live")
    assert main(["--live"]) == 4
    text = _combined(capsys)
    assert "disabled" in text.lower()
    assert script.calls == []
    assert script.sleeps == []
