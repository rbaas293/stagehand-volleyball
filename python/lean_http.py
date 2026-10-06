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
    max_half_open_failures: int = 2
    max_circuit_trips: int = 3
    run_deadline_s: float = 120.0
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
            "maxHalfOpenFailures": "max_half_open_failures",
            "maxCircuitTrips": "max_circuit_trips",
            "runDeadlineS": "run_deadline_s",
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
    circuit_open_rejections: int = 0
    half_open_failures: int = 0
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
            "circuitOpenRejections": self.circuit_open_rejections,
            "halfOpenFailures": self.half_open_failures,
            "byError": dict(self.by_error) or None,
        }

    def log_lines(self) -> list[str]:
        lines = [
            (
                f"HTTP summary: attempts={self.attempts} successes={self.successes} "
                f"failures={self.failures} retries={self.retries} "
                f"retryAfter={self.retry_after_honored} partialJson={self.partial_json_retries} "
                f"circuitTrips={self.circuit_trips} "
                f"circuitRejections={self.circuit_open_rejections}"
            )
        ]
        if self.by_error:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.by_error.items()))
            lines.append(f"HTTP errors by type: {detail}")
        return lines


class CircuitOpenError(LeanApiError):
    """Host circuit breaker is open / probe wait timed out."""


class CircuitTrippedError(LeanApiError):
    """Circuit breaker tripped and caused division failures; do not overwrite games.json."""


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
    """Thread-safe keep-alive HTTP client with retries, budget, and circuit breaker.

    The circuit breaker is **shared across the whole client** (not per-request
    isolation), so a host outage fails the run quickly instead of serializing
    cooldown+probe once per division.
    """

    def __init__(self, settings: HttpSettings | None = None) -> None:
        self.settings = settings or HttpSettings()
        self.stats = HttpRunStats()
        self._lock = threading.RLock()
        # One keep-alive pool per thread — http.client connections are not
        # safe to share across concurrent requests on the same socket.
        self._local = threading.local()
        self._fail_streak = 0
        self._circuit_until = 0.0
        self._circuit_state = "closed"  # closed | open | half_open | tripped
        self._half_open_holder: int | None = None
        self._half_open_failures = 0
        self._retries_used = 0
        self._run_deadline: float | None = None
        self._fatal: CircuitTrippedError | None = None
        self._ssl_ctx = ssl.create_default_context()

    def reset_stats(self) -> None:
        with self._lock:
            self.stats = HttpRunStats()
            self._retries_used = 0
            self._fail_streak = 0
            self._circuit_until = 0.0
            self._circuit_state = "closed"
            self._half_open_holder = None
            self._half_open_failures = 0
            self._run_deadline = None
            self._fatal = None

    def begin_run(self) -> None:
        """Mark the start of a scrape run (deadline + fresh breaker budget)."""
        with self._lock:
            self.reset_stats()
            self._run_deadline = time.monotonic() + self.settings.run_deadline_s

    def is_run_aborted(self) -> bool:
        with self._lock:
            return self._fatal is not None

    def run_abort_error(self) -> CircuitTrippedError | None:
        with self._lock:
            return self._fatal

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

    def _trip_run(self, message: str) -> None:
        """Abort the whole run; subsequent get_json calls raise immediately."""
        self._circuit_state = "tripped"
        self._half_open_holder = None
        if self._fatal is None:
            self._fatal = CircuitTrippedError(message)
            self.stats.circuit_trips += 1

    def _open_circuit(self) -> None:
        """Transition to open; count a trip once per opening."""
        self._circuit_state = "open"
        self._circuit_until = time.monotonic() + self.settings.circuit_cooldown_s
        self._half_open_holder = None
        self._fail_streak = 0
        self.stats.circuit_trips += 1
        if self.stats.circuit_trips >= self.settings.max_circuit_trips:
            self._trip_run(
                f"HTTP circuit breaker: trip budget exhausted "
                f"({self.stats.circuit_trips} trips)"
            )
            return
        # Drop this thread's pooled conns; other threads drop on next use/invalidate.
        self._close_all_unlocked()

    def _check_deadline(self) -> None:
        if self._fatal is not None:
            raise self._fatal
        if self._run_deadline is not None and time.monotonic() >= self._run_deadline:
            self._trip_run(
                f"Lean HTTP run deadline exceeded ({self.settings.run_deadline_s:.0f}s)"
            )
            raise self._fatal

    def _enter_circuit(self) -> bool:
        """
        Gate requests through the shared breaker.

        Returns True if this caller is the half-open probe holder (must release
        in finally via _release_probe).
        """
        tid = threading.get_ident()
        # Bound total wait so a timed-out probe cannot stall every worker.
        wait_deadline = time.monotonic() + max(self.settings.circuit_cooldown_s * 2, 1.0)
        while True:
            self._check_deadline()
            wait_for = 0.0
            with self._lock:
                if self._fatal is not None:
                    raise self._fatal
                state = self._circuit_state
                if state == "closed":
                    return False
                if state == "tripped":
                    self.stats.circuit_open_rejections += 1
                    raise self._fatal or CircuitTrippedError("HTTP circuit breaker tripped")
                if state == "open":
                    remaining = self._circuit_until - time.monotonic()
                    if remaining > 0:
                        wait_for = min(remaining, self.settings.circuit_cooldown_s)
                    elif self._half_open_holder is None:
                        self._circuit_state = "half_open"
                        self._half_open_holder = tid
                        return True
                    else:
                        wait_for = 0.05
                elif state == "half_open":
                    if self._half_open_holder == tid:
                        return True
                    wait_for = 0.05
            if time.monotonic() >= wait_deadline:
                with self._lock:
                    self.stats.circuit_open_rejections += 1
                    self._trip_run(
                        "HTTP circuit breaker: timed out waiting for cooldown/probe; "
                        "aborting run"
                    )
                raise self._fatal
            if wait_for > 0:
                time.sleep(wait_for)

    def _release_probe(self, *, outcome: str) -> None:
        """
        Release the half-open probe holder.

        outcome: "success" | "breaker_failure" | "other"
        Must be called from a finally so 404 / non-UTF-8 / API errors cannot stick
        the holder.
        """
        tid = threading.get_ident()
        with self._lock:
            if self._half_open_holder != tid:
                return
            self._half_open_holder = None
            if self._circuit_state != "half_open":
                return
            if outcome == "success":
                self._circuit_state = "closed"
                self._circuit_until = 0.0
                self._half_open_failures = 0
                self._fail_streak = 0
            elif outcome == "breaker_failure":
                self.stats.half_open_failures += 1
                self._half_open_failures += 1
                if self._half_open_failures >= self.settings.max_half_open_failures:
                    self._trip_run(
                        f"HTTP circuit breaker: {self._half_open_failures} consecutive "
                        f"failed half-open probes; aborting run"
                    )
                else:
                    self._open_circuit()
            else:
                # Non-breaker failure during probe (404, bad body, API envelope):
                # release holder and return to open so another request can probe.
                self._circuit_state = "open"
                self._circuit_until = time.monotonic() + self.settings.circuit_cooldown_s

    def _record_success(self) -> None:
        with self._lock:
            self._fail_streak = 0
            self.stats.successes += 1

    def _record_failure(self, label: str, *, toward_breaker: bool = True) -> str:
        """
        Record a failure. Returns probe outcome hint for _release_probe:
        "breaker_failure" | "other".
        """
        with self._lock:
            self.stats.failures += 1
            self.stats.record_error(label)
            if not toward_breaker:
                return "other"
            if self._circuit_state == "half_open":
                return "breaker_failure"
            self._fail_streak += 1
            if self._fail_streak >= self.settings.circuit_failure_threshold:
                self._open_circuit()
                if self._fatal is not None:
                    raise self._fatal
            return "breaker_failure"

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
        is_probe = False
        probe_outcome = "other"

        try:
            is_probe = self._enter_circuit()
            for attempt in range(1, attempts + 1):
                with self._lock:
                    self.stats.attempts += 1
                # Re-check abort between attempts (another worker may have tripped).
                if self.is_run_aborted():
                    raise self.run_abort_error()  # type: ignore[misc]
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
                        probe_outcome = self._record_failure("partial_json")
                        raise LeanApiError(
                            f"Bad/partial JSON from {url} after {attempt} attempt(s): {err}"
                        ) from err

                    if isinstance(payload, dict) and "Data" in payload and "StatusCode" in payload:
                        code = payload.get("StatusCode")
                        if code not in (200, None) and payload.get("Data") is None:
                            # API envelope error — not a breaker failure.
                            probe_outcome = "other"
                            raise LeanApiError(
                                f"API error for {url}: {payload.get('ErrorMessage') or code}"
                            )
                        self._record_success()
                        probe_outcome = "success"
                        return payload["Data"]
                    self._record_success()
                    probe_outcome = "success"
                    return payload

                except CircuitTrippedError:
                    probe_outcome = "breaker_failure"
                    raise
                except LeanApiError:
                    # probe_outcome already set when we raised above; keep "other" for API errs.
                    raise
                except urllib.error.HTTPError as err:
                    last_err = err
                    retry_after = _parse_retry_after(getattr(err, "headers", None))
                    retryable = err.code in (429, 500, 502, 503, 504)
                    counts_for_breaker = err.code == 429 or err.code >= 500
                    if retryable and attempt < attempts and self._budget_allow_retry():
                        self._invalidate_conn(host)
                        self._sleep_backoff(
                            attempt,
                            retry_after=retry_after if err.code in (429, 503) else None,
                        )
                        continue
                    probe_outcome = self._record_failure(
                        f"http_{err.code}", toward_breaker=counts_for_breaker
                    )
                    raise LeanApiError(
                        f"HTTP {err.code} for {url}: {err.reason or err}"
                    ) from err
                except (http.client.HTTPException, OSError, TimeoutError) as err:
                    last_err = err
                    label = type(err).__name__
                    if attempt < attempts and self._budget_allow_retry():
                        self._invalidate_conn(host)
                        self._sleep_backoff(attempt)
                        continue
                    probe_outcome = self._record_failure(label)
                    raise LeanApiError(
                        f"Request failed for {url} after {attempt} attempt(s): "
                        f"{type(err).__name__}: {err}"
                    ) from err

            raise LeanApiError(f"Request failed for {url}: {last_err}")
        finally:
            if is_probe:
                self._release_probe(outcome=probe_outcome)

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
    """Apply settings and start a fresh run budget/deadline (call at scrape start)."""
    client = _CLIENT
    if settings is not None:
        client.configure(settings)
    client.begin_run()
    return client


def http_get_json(url: str) -> Any:
    return _CLIENT.get_json(url)
