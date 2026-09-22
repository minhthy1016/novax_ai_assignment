"""Retry backoff and per-model circuit breaking.

Breakers are keyed by model id, not provider. Hosted APIs such as NVIDIA NIM run each
model as a separate deployment, so one failing model must not block healthy models on the
same provider. A provider-wide outage still opens every affected model's breaker, just
independently.

Both are in-process. With several API replicas each keeps its own breaker state, which
is acceptable: a breaker only needs to stop *this* instance from queueing requests
behind a dead provider. (Shared breaker state in Redis is noted in the scale proposal.)
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from opsassist.providers.base import ProviderError, ProviderRateLimited


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_s: float = 0.5
    cap_s: float = 8.0

    def delay(self, attempt: int, error: ProviderError, rng: random.Random | None = None) -> float:
        """Delay before retry number ``attempt`` (1-based) using full jitter.

        A provider-supplied Retry-After is honoured when it fits under the cap; otherwise the
        retry would outlive any reasonable interactive deadline and we move on instead.
        """
        if isinstance(error, ProviderRateLimited) and error.retry_after_s is not None:
            return min(error.retry_after_s, self.cap_s)
        ceiling = min(self.cap_s, self.base_s * (2 ** (attempt - 1)))
        return (rng or random).uniform(0, ceiling)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures; after ``cooldown_s`` lets a
    single probe through (half-open). A success closes it, a failure re-opens it."""

    failure_threshold: int = 3
    cooldown_s: float = 30.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _failures: int = 0
    _opened_at: float | None = None
    _probe_in_flight: bool = False

    def _now(self) -> float:
        return self.clock()

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._now() - self._opened_at >= self.cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = self._now()


class BreakerRegistry:
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, model_id: str) -> CircuitBreaker:
        if model_id not in self._breakers:
            self._breakers[model_id] = CircuitBreaker(self._threshold, self._cooldown)
        return self._breakers[model_id]

    def states(self) -> dict[str, BreakerState]:
        return {name: b.state for name, b in self._breakers.items()}
