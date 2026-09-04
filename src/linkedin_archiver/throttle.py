"""Conservative pacing helpers for browser-driven LinkedIn operations."""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass

_SLEEP_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?$")


@dataclass(frozen=True)
class SleepInterval:
    minimum: float = 0.0
    maximum: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.maximum > 0

    def sample(self) -> float:
        if not self.enabled:
            return 0.0
        if self.minimum == self.maximum:
            return self.minimum
        return random.uniform(self.minimum, self.maximum)

    def wait(self, logger=None, reason: str = "") -> float:
        delay = self.sample()
        if delay <= 0:
            return 0.0
        if logger:
            suffix = f" {reason}" if reason else ""
            logger.info(f"Sleeping {delay:.2f}s{suffix}")
        time.sleep(delay)
        return delay

    async def wait_async(self, logger=None, reason: str = "") -> float:
        delay = self.sample()
        if delay <= 0:
            return 0.0
        if logger:
            suffix = f" {reason}" if reason else ""
            logger.info(f"Sleeping {delay:.2f}s{suffix}")
        await asyncio.sleep(delay)
        return delay


def parse_sleep(value: str | float | int | SleepInterval | None) -> SleepInterval:
    if isinstance(value, SleepInterval):
        return value
    if value is None:
        return SleepInterval()
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("sleep must be non-negative")
        return SleepInterval(float(value), float(value))

    match = _SLEEP_RANGE.match(value)
    if not match:
        raise ValueError("sleep must be a number or range such as '2' or '2-5'")

    minimum = float(match.group(1))
    maximum = float(match.group(2) or minimum)
    if maximum < minimum:
        raise ValueError("sleep range maximum must be greater than or equal to minimum")
    return SleepInterval(minimum, maximum)
