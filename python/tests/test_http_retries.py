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
    CircuitOpenError,
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
        circuit_cooldown_s=30,
        backoff_base_s=0.01,
        jitter_s=0,
    )
    client = LeanHttpClient(settings)
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
        with pytest.raises(CircuitOpenError, match="Circuit open"):
            client.get_json("https://example.test/api")
    assert client.stats.circuit_trips >= 1
