"""Small, dependency-free reliability primitives shared by runtime components.

The module deliberately contains no request payloads or provider responses.  It
is safe to use in public error paths, worker checkpoints, and eval artifacts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import httpx


class OperationDeadlineExceeded(TimeoutError):
    """Raised before an operation starts when its shared deadline is exhausted."""

    def __init__(self, stage: str):
        super().__init__(f"operation deadline exhausted at stage={stage}")
        self.stage = stage


class DependencyCircuitOpen(RuntimeError):
    """Raised when a dependency has recently failed often enough to fail fast."""

    def __init__(self, dependency: str, retry_after_seconds: float):
        super().__init__(f"dependency circuit is open: {dependency}")
        self.dependency = dependency
        self.retry_after_seconds = max(0.0, retry_after_seconds)


@dataclass(frozen=True)
class OperationDeadline:
    """A monotonic deadline that can safely be passed to nested calls."""

    expires_at: float

    @classmethod
    def after(cls, timeout_seconds: float, *, now: Callable[[], float] = time.monotonic) -> OperationDeadline:
        return cls(now() + max(0.0, float(timeout_seconds)))

    def remaining_seconds(self, *, now: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, self.expires_at - now())

    def remaining_ms(self, *, now: Callable[[], float] = time.monotonic) -> int:
        return int(self.remaining_seconds(now=now) * 1000)

    def timeout_seconds(self, configured_timeout_seconds: float, *, stage: str) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise OperationDeadlineExceeded(stage)
        return min(max(0.001, float(configured_timeout_seconds)), remaining)

    def ensure_remaining(self, *, stage: str) -> None:
        self.timeout_seconds(float("inf"), stage=stage)


@dataclass(frozen=True)
class SafeFailure:
    """Public-safe failure metadata; never include exception text in this value."""

    error_code: str
    stage: str
    retryable: bool
    attempt: int = 1
    request_id: str = ""
    operation_id: str = ""
    deadline_remaining_ms: int | None = None

    def model_dump(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy for operations that have not produced a response."""

    max_attempts: int = 2
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 2.0

    def delay_seconds(self, attempt: int) -> float:
        return min(self.max_delay_seconds, self.base_delay_seconds * max(1, attempt))


class DependencyCircuit:
    """In-process circuit breaker with a single half-open probe.

    The local Compose target uses one process per service, so an in-process
    breaker is sufficient and intentionally avoids making Redis a hard runtime
    dependency.
    """

    def __init__(
        self,
        dependency: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 15.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dependency = dependency
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.001, float(cooldown_seconds))
        self._now = now
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe = False

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        elapsed = self._now() - self._opened_at
        if elapsed < self.cooldown_seconds:
            raise DependencyCircuitOpen(self.dependency, self.cooldown_seconds - elapsed)
        if self._half_open_probe:
            raise DependencyCircuitOpen(self.dependency, self.cooldown_seconds)
        self._half_open_probe = True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open_probe = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._half_open_probe = False
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = self._now()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and self._now() - self._opened_at < self.cooldown_seconds


def is_retryable_exception(exc: BaseException) -> bool:
    """Return true only for transient transport/dependency failures."""

    if isinstance(exc, (OperationDeadlineExceeded, asyncio.CancelledError, ValueError, PermissionError)):
        return False
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError, httpx.PoolTimeout),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code in {429, 502, 503, 504}


def safe_failure_from_exception(
    exc: BaseException,
    *,
    stage: str,
    attempt: int = 1,
    request_id: str = "",
    operation_id: str = "",
    deadline: OperationDeadline | None = None,
) -> SafeFailure:
    explicit_code = getattr(exc, "safe_code", None)
    domain_code = getattr(exc, "code", None)
    metadata = getattr(exc, "metadata", None)
    metadata_code = metadata.get("safe_error_code") if isinstance(metadata, dict) else None
    if isinstance(explicit_code, str) and explicit_code:
        code = explicit_code
    elif isinstance(domain_code, str) and domain_code:
        code = domain_code
    elif isinstance(metadata_code, str) and metadata_code:
        code = metadata_code
    elif isinstance(exc, DependencyCircuitOpen):
        code = "DEPENDENCY_CIRCUIT_OPEN"
    elif isinstance(exc, OperationDeadlineExceeded):
        code = "DEPENDENCY_TIMEOUT"
    elif isinstance(exc, asyncio.CancelledError):
        code = "CANCELLED"
    elif isinstance(exc, httpx.TimeoutException):
        code = "DEPENDENCY_TIMEOUT"
    elif isinstance(exc, TimeoutError):
        code = "DEPENDENCY_TIMEOUT"
    elif isinstance(exc, httpx.NetworkError):
        code = "DEPENDENCY_UNAVAILABLE"
    elif isinstance(exc, httpx.HTTPStatusError):
        code = "DEPENDENCY_UNAVAILABLE" if exc.response.status_code in {429, 502, 503, 504} else "INTERNAL_ERROR"
    else:
        code = "INTERNAL_ERROR"
    explicit_retryable = getattr(exc, "retryable", None)
    if isinstance(explicit_retryable, bool):
        retryable = explicit_retryable
    elif hasattr(exc, "__cause__") and isinstance(exc.__cause__, BaseException):
        retryable = is_retryable_exception(exc.__cause__)
    else:
        retryable = is_retryable_exception(exc)
    return SafeFailure(
        error_code=code,
        stage=stage,
        retryable=retryable,
        attempt=max(1, attempt),
        request_id=request_id,
        operation_id=operation_id,
        deadline_remaining_ms=deadline.remaining_ms() if deadline is not None else None,
    )
