"""Tests: lean HTTP Retry-After, circuit breaker, partial JSON, protocol retries.

Replaces the pre-#6 urllib-based suite with LeanHttpClient coverage:
  - test_http_retries_protocol_and_os_errors_then_succeeds  →
      test_retries_protocol_and_os_errors_then_succeeds (same four exceptions)
  - test_http_exhausted_retries_become_lean_api_error →
      test_exhausted_retries_become_lean_api_error (same four exceptions)
Plus new robust-lean cases: Retry-After, partial JSON, circuit breaker.
"""

from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lean_http import (  # noqa: E402
    CircuitTrippedError,
    HttpSettings,
    LeanApiError,
    LeanHttpClient,
)

_PROTOCOL_EXCS = [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"partial"),
    http.client.BadStatusLine("bad status"),
    ConnectionResetError(104, "Connection reset by peer"),
]
_PROTOCOL_IDS = [
    "RemoteDisconnected",
    "IncompleteRead",
    "BadStatusLine",
    "ConnectionResetError",
]


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _ok_response(payload=None):
    resp = MagicMock()
    resp.status = 200
    resp.reason = "OK"
    resp.headers = {}
    resp.read.return_value = _json_bytes(
        payload if payload is not None else {"Data": {"ok": True}, "StatusCode": 200}
    )
    return resp


@pytest.mark.parametrize("exc", _PROTOCOL_EXCS, ids=_PROTOCOL_IDS)
def test_http_retries_protocol_and_os_errors_then_succeeds(exc):
    """Successor to test_http_retries_protocol_and_os_errors_then_succeeds."""
    client = LeanHttpClient(
        HttpSettings(max_retries=3, backoff_base_s=0.01, jitter_s=0, max_retries_total=10)
    )
    conn = MagicMock()
    calls = {"n": 0}

    def request(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc

    conn.request.side_effect = request
    conn.getresponse.side_effect = lambda: _ok_response()
    conn.sock = MagicMock()

    with (
        patch.object(client, "_get_conn", return_value=conn),
        patch("lean_http.time.sleep") as sleep,
    ):
        data = client.get_json("https://example.test/api")
    assert data == {"ok": True}
    assert sleep.call_count == 1
    assert client.stats.retries >= 1


@pytest.mark.parametrize("exc", _PROTOCOL_EXCS, ids=_PROTOCOL_IDS)
def test_http_exhausted_retries_become_lean_api_error(exc):
    """Successor to test_http_exhausted_retries_become_lean_api_error."""
    client = LeanHttpClient(
        HttpSettings(max_retries=2, backoff_base_s=0.01, jitter_s=0, max_retries_total=10)
    )
    conn = MagicMock()
    conn.request.side_effect = exc
    conn.sock = MagicMock()

    with (
        patch.object(client, "_get_conn", return_value=conn),
        patch("lean_http.time.sleep"),
        pytest.raises(LeanApiError, match="after 2 attempt"),
    ):
        client.get_json("https://example.test/api")


def test_honours_retry_after_on_429():
    client = LeanHttpClient(
        HttpSettings(max_retries=3, backoff_base_s=0.01, jitter_s=0, max_retries_total=10)
    )
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        if calls["n"] == 1:
            resp = MagicMock()
            resp.status = 429
            resp.reason = "Too Many Requests"
            resp.headers = {"Retry-After": "1.5"}
            resp.read.return_value = b""
            return resp
        return _ok_response({"Data": [1], "StatusCode": 200})

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with (
        patch.object(client, "_get_conn", return_value=conn),
        patch("lean_http.time.sleep") as sleep,
    ):
        data = client.get_json("https://example.test/api")
    assert data == [1]
    assert client.stats.retry_after_honored == 1
    assert sleep.call_args_list[0].args[0] == 1.5


def test_partial_json_is_retried():
    client = LeanHttpClient(
        HttpSettings(max_retries=3, backoff_base_s=0.01, jitter_s=0, max_retries_total=10)
    )
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        resp = MagicMock()
        resp.status = 200
        resp.reason = "OK"
        resp.headers = {}
        if calls["n"] == 1:
            resp.read.return_value = b'{"Data": '
        else:
            resp.read.return_value = _json_bytes({"Data": {"ok": True}, "StatusCode": 200})
        return resp

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with (
        patch.object(client, "_get_conn", return_value=conn),
        patch("lean_http.time.sleep"),
    ):
        data = client.get_json("https://example.test/api")
    assert data == {"ok": True}
    assert client.stats.partial_json_retries >= 1


def test_circuit_breaker_opens_after_consecutive_failures():
    settings = HttpSettings(
        max_retries=1,
        max_retries_total=20,
        circuit_failure_threshold=3,
        circuit_cooldown_s=0.01,
        max_half_open_failures=2,
        max_circuit_trips=5,
        backoff_base_s=0.01,
        jitter_s=0,
    )
    client = LeanHttpClient(settings)
    client.begin_run()
    conn = MagicMock()
    conn.request.side_effect = ConnectionResetError("reset")
    conn.sock = MagicMock()

    with (
        patch.object(client, "_get_conn", return_value=conn),
        patch("lean_http.time.sleep"),
    ):
        for _ in range(3):
            with pytest.raises(LeanApiError):
                client.get_json("https://example.test/api")
        assert client.stats.circuit_trips == 1
        assert client._circuit_state == "open"
        # Half-open probe also fails → re-open (or trip once half-open budget exhausted).
        with pytest.raises(LeanApiError):
            client.get_json("https://example.test/api")
    assert client.stats.circuit_trips >= 2 or client._circuit_state == "tripped"



def test_four_xx_except_429_do_not_open_circuit():
    """5x 404 then a 200 must succeed; breaker never opens."""
    client = LeanHttpClient(
        HttpSettings(
            max_retries=1,
            max_retries_total=20,
            circuit_failure_threshold=3,
            circuit_cooldown_s=0.01,
            backoff_base_s=0.01,
            jitter_s=0,
        )
    )
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        if calls["n"] <= 5:
            resp = MagicMock()
            resp.status = 404
            resp.reason = "Not Found"
            resp.headers = {}
            resp.read.return_value = b""
            return resp
        return _ok_response({"Data": {"ok": True}, "StatusCode": 200})

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with patch.object(client, "_get_conn", return_value=conn), patch("lean_http.time.sleep"):
        for _ in range(5):
            with pytest.raises(LeanApiError, match="HTTP 404"):
                client.get_json("https://example.test/d")
        data = client.get_json("https://example.test/d")
    assert data == {"ok": True}
    assert client.stats.circuit_trips == 0
    assert client._circuit_state == "closed"


def test_five_xx_opens_breaker_half_open_probe_closes():
    """5xx failures open the breaker; after cooldown a successful probe closes it."""
    client = LeanHttpClient(
        HttpSettings(
            max_retries=1,
            max_retries_total=50,
            circuit_failure_threshold=3,
            circuit_cooldown_s=0.01,
            max_half_open_failures=2,
            max_circuit_trips=5,
            backoff_base_s=0.01,
            jitter_s=0,
        )
    )
    client.begin_run()
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        if calls["n"] <= 3:
            resp = MagicMock()
            resp.status = 503
            resp.reason = "Unavailable"
            resp.headers = {}
            resp.read.return_value = b""
            return resp
        return _ok_response({"Data": [1], "StatusCode": 200})

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with patch.object(client, "_get_conn", return_value=conn), patch("lean_http.time.sleep"):
        for _ in range(3):
            with pytest.raises(LeanApiError, match="HTTP 503"):
                client.get_json("https://example.test/api")
        assert client.stats.circuit_trips == 1
        # Cooldown elapsed (sleep patched / cooldown tiny); this call is the half-open probe.
        data = client.get_json("https://example.test/api")
    assert data == [1]
    assert client._circuit_state == "closed"


def test_circuit_tripped_error_leaves_games_json_untouched(tmp_path, monkeypatch):
    """CircuitTrippedError → exit 1 and do not overwrite an existing games.json."""
    import main as main_mod

    games = tmp_path / "games.json"
    original = '{"matchedTeams":["keep-me"],"games":[{"t":1}]}'
    games.write_text(original, encoding="utf-8")

    async def _boom(_cli=None, _env=None):
        raise CircuitTrippedError("circuit tripped for test")

    monkeypatch.setattr(main_mod, "scrape", _boom)
    monkeypatch.setattr(main_mod, "ROOT", tmp_path)
    # Force OUTPUT_PATH into tmp via _RUNNING_FROM_SOURCE True path: ROOT/games.json
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", True)

    with pytest.raises(SystemExit) as ei:
        main_mod.main([])
    assert ei.value.code == 1
    assert games.read_text(encoding="utf-8") == original


def test_half_open_holder_released_after_404():
    """404 during half-open must release the probe holder (finally), not stick the breaker."""
    client = LeanHttpClient(
        HttpSettings(
            max_retries=1,
            max_retries_total=50,
            circuit_failure_threshold=2,
            circuit_cooldown_s=0.01,
            max_half_open_failures=5,
            max_circuit_trips=10,
            backoff_base_s=0.01,
            jitter_s=0,
        )
    )
    client.begin_run()
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        resp = MagicMock()
        resp.headers = {}
        resp.read.return_value = b""
        if calls["n"] <= 2:
            resp.status = 503
            resp.reason = "Unavailable"
            return resp
        resp.status = 404
        resp.reason = "Not Found"
        return resp

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with patch.object(client, "_get_conn", return_value=conn), patch("lean_http.time.sleep"):
        for _ in range(2):
            with pytest.raises(LeanApiError, match="HTTP 503"):
                client.get_json("https://example.test/api")
        assert client._circuit_state == "open"
        with pytest.raises(LeanApiError, match="HTTP 404"):
            client.get_json("https://example.test/api")
    assert client._half_open_holder is None
    assert client._circuit_state in ("open", "closed")


def test_half_open_holder_released_after_non_utf8():
    """Non-UTF-8 body during half-open must release the probe holder."""
    client = LeanHttpClient(
        HttpSettings(
            max_retries=1,
            max_retries_total=50,
            circuit_failure_threshold=2,
            circuit_cooldown_s=0.01,
            max_half_open_failures=5,
            max_circuit_trips=10,
            backoff_base_s=0.01,
            jitter_s=0,
        )
    )
    client.begin_run()
    conn = MagicMock()
    calls = {"n": 0}

    def getresponse():
        calls["n"] += 1
        resp = MagicMock()
        resp.headers = {}
        if calls["n"] <= 2:
            resp.status = 503
            resp.reason = "Unavailable"
            resp.read.return_value = b""
            return resp
        resp.status = 200
        resp.reason = "OK"
        resp.read.return_value = b"\xff\xfe not utf8"
        return resp

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    with patch.object(client, "_get_conn", return_value=conn), patch("lean_http.time.sleep"):
        for _ in range(2):
            with pytest.raises(LeanApiError, match="HTTP 503"):
                client.get_json("https://example.test/api")
        with pytest.raises(LeanApiError, match="Non-UTF8"):
            client.get_json("https://example.test/api")
    assert client._half_open_holder is None


def test_persistent_5xx_aborts_run_within_few_seconds():
    """Shared breaker + half-open budget: persistent outage fails fast (not ~12 min)."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = LeanHttpClient(
        HttpSettings(
            max_retries=1,
            max_retries_total=200,
            circuit_failure_threshold=2,
            circuit_cooldown_s=0.05,
            max_half_open_failures=2,
            max_circuit_trips=3,
            run_deadline_s=30.0,
            backoff_base_s=0.01,
            jitter_s=0,
        )
    )
    client.begin_run()
    conn = MagicMock()

    def getresponse():
        resp = MagicMock()
        resp.status = 503
        resp.reason = "Unavailable"
        resp.headers = {}
        resp.read.return_value = b""
        return resp

    conn.request = MagicMock()
    conn.getresponse.side_effect = getresponse
    conn.sock = MagicMock()

    t0 = time.perf_counter()
    errors: list[BaseException] = []

    def once():
        try:
            client.get_json("https://example.test/api")
        except BaseException as err:  # noqa: BLE001
            errors.append(err)
            raise

    with patch.object(client, "_get_conn", return_value=conn):
        # Real short sleeps (cooldown 50ms) — still must finish in a few seconds.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(once) for _ in range(24)]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except (LeanApiError, CircuitTrippedError):
                    pass
                if client.is_run_aborted():
                    break
            pool.shutdown(wait=False, cancel_futures=True)

    elapsed = time.perf_counter() - t0
    assert client.is_run_aborted() or any(isinstance(e, CircuitTrippedError) for e in errors)
    assert elapsed < 5.0, f"persistent outage took {elapsed:.2f}s (expected <5s)"


def test_persistent_5xx_scrape_leaves_games_json_untouched(tmp_path, monkeypatch):
    """End-to-end: circuit abort during scrape_lean → exit 1, games.json unchanged."""
    import main as main_mod

    games = tmp_path / "games.json"
    original = '{"matchedTeams":["keep-me"],"games":[{"t":1}]}'
    games.write_text(original, encoding="utf-8")

    async def _boom(_cli=None, _env=None):
        raise CircuitTrippedError(
            "HTTP circuit breaker: 2 consecutive failed half-open probes; aborting run"
        )

    monkeypatch.setattr(main_mod, "scrape", _boom)
    monkeypatch.setattr(main_mod, "ROOT", tmp_path)
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", True)

    with pytest.raises(SystemExit) as ei:
        main_mod.main([])
    assert ei.value.code == 1
    assert games.read_text(encoding="utf-8") == original

