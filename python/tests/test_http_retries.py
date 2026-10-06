"""Tests: lean _http_get_json retries transient protocol/connection errors."""

from __future__ import annotations

import http.client
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lean_api import LeanApiError, _http_get_json


def _raise_then_ok(exc: BaseException):
    """urlopen side_effect: fail once with exc, then return a valid JSON response."""
    ok = MagicMock()
    ok.status = 200
    ok.getcode.return_value = 200
    ok.read.return_value = b'{"ok": true}'
    ok.__enter__.return_value = ok
    ok.__exit__.return_value = None

    def side_effect(*_a, **_k):
        if not getattr(side_effect, "failed", False):
            side_effect.failed = True
            raise exc
        return ok

    return side_effect


@pytest.mark.parametrize(
    "exc",
    [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.IncompleteRead(b"partial"),
        http.client.BadStatusLine("bad status"),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
    ids=["RemoteDisconnected", "IncompleteRead", "BadStatusLine", "ConnectionResetError"],
)
def test_http_retries_protocol_and_os_errors_then_succeeds(exc):
    with (
        patch("lean_api.urllib.request.urlopen", side_effect=_raise_then_ok(exc)),
        patch("lean_api.time.sleep") as sleep,
    ):
        data = _http_get_json("https://example.test/api", retries=3, backoff_s=0.01)
    assert data == {"ok": True}
    assert sleep.call_count == 1


@pytest.mark.parametrize(
    "exc",
    [
        http.client.RemoteDisconnected("gone"),
        http.client.IncompleteRead(b"x"),
        http.client.BadStatusLine("no"),
        ConnectionResetError("reset"),
    ],
    ids=["RemoteDisconnected", "IncompleteRead", "BadStatusLine", "ConnectionResetError"],
)
def test_http_exhausted_retries_become_lean_api_error(exc):
    with (
        patch("lean_api.urllib.request.urlopen", side_effect=exc),
        patch("lean_api.time.sleep"),
        pytest.raises(LeanApiError, match="after 2 attempt"),
    ):
        _http_get_json("https://example.test/api", retries=2, backoff_s=0.01)
