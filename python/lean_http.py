"""
Robust lean-mode HTTP client for the LMS pub API.

Features:
  - Shared opener / keep-alive connection pool per host
  - Separate connect and read timeouts
  - Exponential backoff with jitter; honour Retry-After on 429/503
  - Per-request retry limit + total retry budget per run
  - Retry on bad/partial JSON
  - Circuit breaker after N consecutive host failures
  - Run-level stats summary (retries, failures, circuit trips)
"""

from __future__ import annotations

import http.client
import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LeanApiError(Exception):
    """HTTP / shape errors from the pub API (re-exported from lean_api)."""


@dataclass
class HttpSettings:
    """Configurable lean HTTP policy (config.yaml → http:)."""

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 20.0
    max_retries: int = 4
    max_retries_total: int = 80
    backoff_base_s: float = 0.4
    backoff_max_s: float = 10.0
    jitter_s: float = 0.3
    circuit_failure_threshold: int = 5
    circuit_cooldown_s: float = 20.0
    concurrency: int = 4
    user_agent: str = (
        "stagehand-volleyball/lean (+https://github.com/rbaas293/stagehand-volleyball)"
    )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> HttpSettings:
        if not raw:
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        # Accept camelCase aliases from YAML.
        aliases = {
            "connectTimeoutS": "connect_timeout_s",
            "readTimeoutS": "read_timeout_s",
            "maxRetries": "max_retries",
            "maxRetriesTotal": "max_retries_total",
            "backoffBaseS": "backoff_base_s",
            "backoffMaxS": "backoff_max_s",
            "jitterS": "jitter_s",
            "circuitFailureThreshold": "circuit_failure_threshold",
            "circuitCooldownS": "circuit_cooldown_s",
            "concurrency": "concurrency",
            "userAgent": "user_agent",
        }
        kwargs: dict[str, Any] = {}
        for k, v in raw.items():
            key = aliases.get(k, k)
            if key in known:
                kwargs[key] = v
        return cls(**kwargs)


@dataclass
class HttpRunStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    retry_after_honored: int = 0
    partial_json_retries: int = 0
    circuit_trips: int = 0
    by_error: dict[str, int] = field(default_factory=dict)

    def record_error(self, label: str) -> None:
        self.by_error[label] = self.by_error.get(label, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "retryAfterHonored": self.retry_after_honored,
            "partialJsonRetries": self.partial_json_retries,
            "circuitTrips": self.circuit_trips,
            "byError": dict(self.by_error) or None,
        }

    def log_lines(self) -> list[str]:
        lines = [
            (
                f"HTTP summary: attempts={self.attempts} successes={self.successes} "
                f"failures={self.failures} retries={self.retries} "
                f"retryAfter={self.retry_after_honored} partialJson={self.partial_json_retries} "
                f"circuitTrips={self.circuit_trips}"
            )
        ]
        if self.by_error:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.by_error.items()))
            lines.append(f"HTTP errors by type: {detail}")
        return lines


class CircuitOpenError(LeanApiError):
    """Host circuit breaker is open."""


def _host_key(parsed: urllib.parse.ParseResult) -> str:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def _parse_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except (AttributeError, KeyError, TypeError):
        raw = None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


class LeanHttpClient:
    """Thread-safe keep-alive HTTP client with retries, budget, and circuit breaker."""

    def __init__(self, settings: HttpSettings | None = None) -> None:
        self.settings = settings or HttpSettings()
        self.stats = HttpRunStats()
        self._lock = threading.RLock()
        # One keep-alive pool per thread — http.client connections are not
        # safe to share across concurrent requests on the same socket.
        self._local = threading.local()
        self._fail_streak: dict[str, int] = {}
        self._circuit_until: dict[str, float] = {}
        self._retries_used = 0
        self._ssl_ctx = ssl.create_default_context()

    def reset_stats(self) -> None:
        with self._lock:
            self.stats = HttpRunStats()
            self._retries_used = 0
            self._fail_streak.clear()
            self._circuit_until.clear()

    def configure(self, settings: HttpSettings) -> None:
        with self._lock:
            self.settings = settings
            self._close_all_unlocked()

    def _thread_conns(self) -> dict[str, http.client.HTTPConnection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        return conns

    def _close_all_unlocked(self) -> None:
        conns = getattr(self._local, "conns", None)
        if not conns:
            return
        for conn in conns.values():
            try:
                conn.close()
            except OSError:
                pass
        conns.clear()

    def close(self) -> None:
        with self._lock:
            self._close_all_unlocked()

    def _check_circuit(self, host: str) -> None:
        until = self._circuit_until.get(host, 0.0)
        if until and time.monotonic() < until:
            self.stats.circuit_trips += 1
            raise CircuitOpenError(
                f"Circuit open for {host} for {until - time.monotonic():.1f}s more "
                f"(after {self.settings.circuit_failure_threshold} consecutive failures)"
            )
        if until and time.monotonic() >= until:
            self._circuit_until.pop(host, None)
            self._fail_streak[host] = 0

    def _record_success(self, host: str) -> None:
        with self._lock:
            self._fail_streak[host] = 0
            self.stats.successes += 1

    def _record_failure(self, host: str, label: str) -> None:
        with self._lock:
            self.stats.failures += 1
            self.stats.record_error(label)
            streak = self._fail_streak.get(host, 0) + 1
            self._fail_streak[host] = streak
            if streak >= self.settings.circuit_failure_threshold:
                self._circuit_until[host] = (
                    time.monotonic() + self.settings.circuit_cooldown_s
                )
                self.stats.circuit_trips += 1
                # Drop pooled connection; it may be bad.
                conn = self._thread_conns().pop(host, None)
                if conn:
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _get_conn(self, parsed: urllib.parse.ParseResult, host: str) -> http.client.HTTPConnection:
        conns = self._thread_conns()
        conn = conns.get(host)
        if conn is not None:
            return conn
        # Connect timeout only at construction; read timeout applied on the socket after connect.
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or 443,
                timeout=self.settings.connect_timeout_s,
                context=self._ssl_ctx,
            )
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port or 80,
                timeout=self.settings.connect_timeout_s,
            )
        conns[host] = conn
        return conn

    def _invalidate_conn(self, host: str) -> None:
        conn = self._thread_conns().pop(host, None)
        if conn:
            try:
                conn.close()
            except OSError:
                pass

    def _budget_allow_retry(self) -> bool:
        with self._lock:
            if self._retries_used >= self.settings.max_retries_total:
                return False
            self._retries_used += 1
            self.stats.retries += 1
            return True

    def _sleep_backoff(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None:
            with self._lock:
                self.stats.retry_after_honored += 1
            time.sleep(min(retry_after, self.settings.backoff_max_s))
            return
        base = self.settings.backoff_base_s * (2 ** (attempt - 1))
        delay = min(base, self.settings.backoff_max_s)
        jitter = random.uniform(0, self.settings.jitter_s) if self.settings.jitter_s else 0.0
        time.sleep(delay + jitter)

    def get_json(self, url: str) -> Any:
        """GET URL and return parsed JSON (unwraps LMS {Data, StatusCode} envelopes)."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LeanApiError(f"Invalid URL: {url}")
        host = _host_key(parsed)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        settings = self.settings
        attempts = max(1, settings.max_retries)
        last_err: BaseException | None = None

        for attempt in range(1, attempts + 1):
            with self._lock:
                self.stats.attempts += 1
            self._check_circuit(host)

            try:
                body = self._raw_get(parsed, host, path)
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as err:
                    last_err = err
                    with self._lock:
                        self.stats.partial_json_retries += 1
                        self.stats.record_error("partial_json")
                    if attempt < attempts and self._budget_allow_retry():
                        self._invalidate_conn(host)
                        self._sleep_backoff(attempt)
                        continue
                    self._record_failure(host, "partial_json")
                    raise LeanApiError(
                        f"Bad/partial JSON from {url} after {attempt} attempt(s): {err}"
                    ) from err

                if isinstance(payload, dict) and "Data" in payload and "StatusCode" in payload:
                    code = payload.get("StatusCode")
                    if code not in (200, None) and payload.get("Data") is None:
                        raise LeanApiError(
                            f"API error for {url}: {payload.get('ErrorMessage') or code}"
                        )
                    self._record_success(host)
                    return payload["Data"]
                self._record_success(host)
                return payload

            except LeanApiError:
                raise
            except urllib.error.HTTPError as err:
                last_err = err
                retry_after = _parse_retry_after(getattr(err, "headers", None))
                retryable = err.code in (429, 500, 502, 503, 504)
                if retryable and attempt < attempts and self._budget_allow_retry():
                    self._invalidate_conn(host)
                    self._sleep_backoff(
                        attempt,
                        retry_after=retry_after if err.code in (429, 503) else None,
                    )
                    continue
                self._record_failure(host, f"http_{err.code}")
                raise LeanApiError(f"HTTP {err.code} for {url}: {err.reason or err}") from err
            except (http.client.HTTPException, OSError, TimeoutError) as err:
                last_err = err
                label = type(err).__name__
                if attempt < attempts and self._budget_allow_retry():
                    self._invalidate_conn(host)
                    self._sleep_backoff(attempt)
                    continue
                self._record_failure(host, label)
                raise LeanApiError(
                    f"Request failed for {url} after {attempt} attempt(s): "
                    f"{type(err).__name__}: {err}"
                ) from err

        raise LeanApiError(f"Request failed for {url}: {last_err}")

    def _raw_get(
        self, parsed: urllib.parse.ParseResult, host: str, path: str
    ) -> str:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.settings.user_agent,
            "Connection": "keep-alive",
        }
        conn = self._get_conn(parsed, host)
        try:
            conn.timeout = self.settings.connect_timeout_s
            conn.request("GET", path, headers=headers)
            # After connect, use read timeout for response body.
            if conn.sock is not None:
                conn.sock.settimeout(self.settings.read_timeout_s)
            resp = conn.getresponse()
            status = resp.status
            raw_headers = resp.headers
            data = resp.read()
        except Exception:
            self._invalidate_conn(host)
            raise

        if status >= 400:
            # Mimic urllib HTTPError for shared handling.
            raise urllib.error.HTTPError(
                f"{parsed.scheme}://{parsed.netloc}{path}",
                status,
                getattr(resp, "reason", f"HTTP {status}"),
                raw_headers,
                None,
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as err:
            raise LeanApiError(f"Non-UTF8 body from {host}{path}") from err


# Process-wide client (one pool / one retry budget per scrape run).
_CLIENT = LeanHttpClient()


def get_client() -> LeanHttpClient:
    return _CLIENT


def configure_http(settings: HttpSettings | None = None) -> LeanHttpClient:
    """Apply settings and reset run stats (call at the start of each scrape)."""
    client = _CLIENT
    if settings is not None:
        client.configure(settings)
    client.reset_stats()
    return client


def http_get_json(url: str) -> Any:
    return _CLIENT.get_json(url)
