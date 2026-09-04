"""Reliable media transfer primitives used by the extractors."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        base = min(self.max_delay, self.initial_delay * (2 ** max(0, attempt - 1)))
        return base * (1 + random.uniform(-self.jitter, self.jitter))


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES or 500 <= status <= 599


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, body: bytes) -> None:
    """Write bytes to `<file>.part` and publish atomically."""
    partial = Path(f"{path}.part")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_partial(partial, body, append=False)
        _finish_partial(partial, path)
    except Exception:
        _clear_partial(partial)
        raise


def _parse_content_range_start(header_value: str | None) -> int | None:
    if not header_value or not header_value.startswith("bytes "):
        return None
    try:
        return int(header_value[6:].split("-", 1)[0])
    except (TypeError, ValueError):
        return None


def _parse_content_range_total(header_value: str | None) -> int | None:
    if not header_value or "/" not in header_value:
        return None
    total = header_value.rsplit("/", 1)[1]
    try:
        return None if total == "*" else int(total)
    except ValueError:
        return None


def _write_partial(path: Path, body: bytes, *, append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    with path.open(mode) as handle:
        handle.write(body)
        handle.flush()


def _finish_partial(partial: Path, destination: Path) -> None:
    partial.replace(destination)


def _clear_partial(partial: Path) -> None:
    try:
        partial.unlink()
    except FileNotFoundError:
        pass


def _headers(referer: str | None, partial_size: int) -> dict[str, str]:
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if partial_size:
        headers["Range"] = f"bytes={partial_size}-"
    return headers


def _download_response_sync(context, url: str, headers: dict[str, str], timeout_ms: int, policy: RetryPolicy, logger=None):
    for attempt in range(1, policy.max_attempts + 1):
        try:
            response = context.request.get(url, headers=headers or None, timeout=timeout_ms)
            status = response.status
            if is_retryable_status(status):
                response.dispose()
                raise IOError(f"retryable HTTP status {status}")
            return response
        except Exception as exc:
            if attempt >= policy.max_attempts:
                raise
            delay = policy.delay(attempt)
            if logger:
                logger.debug(f"    retry {attempt}/{policy.max_attempts - 1} in {delay:.1f}s: {exc}")
            time.sleep(delay)
    raise RuntimeError("download failed")


async def _download_response_async(context, url: str, headers: dict[str, str], timeout_ms: int, policy: RetryPolicy, logger=None):
    for attempt in range(1, policy.max_attempts + 1):
        try:
            response = await context.request.get(url, headers=headers or None, timeout=timeout_ms)
            status = response.status
            if is_retryable_status(status):
                await response.dispose()
                raise IOError(f"retryable HTTP status {status}")
            return response
        except Exception as exc:
            if attempt >= policy.max_attempts:
                raise
            delay = policy.delay(attempt)
            if logger:
                logger.debug(f"    retry {attempt}/{policy.max_attempts - 1} in {delay:.1f}s: {exc}")
            await asyncio.sleep(delay)
    raise RuntimeError("download failed")


def download_sync(
    context,
    url: str,
    destination: Path,
    *,
    referer: str | None = None,
    timeout_ms: int = 60_000,
    policy: RetryPolicy | None = None,
    logger=None,
) -> dict:
    policy = policy or RetryPolicy()
    partial = Path(f"{destination}.part")
    partial_size = partial.stat().st_size if partial.exists() else 0
    response = _download_response_sync(context, url, _headers(referer, partial_size), timeout_ms, policy, logger)
    try:
        if response.status == 416 and partial_size:
            _clear_partial(partial)
            raise IOError("partial download range is no longer valid")
        if not response.ok:
            raise IOError(f"HTTP {response.status}")

        body = response.body()
        if not body:
            raise IOError("empty response body")
        response_headers = dict(response.headers)
        append = response.status == 206 and partial_size > 0 and _parse_content_range_start(response_headers.get("content-range")) == partial_size
        if response.status == 206 and partial_size > 0 and not append:
            _clear_partial(partial)
            append = False

        if not append:
            partial_size = 0
        _write_partial(partial, body, append=append)

        expected_total = _parse_content_range_total(response_headers.get("content-range")) if append else None
        final_size = partial.stat().st_size
        if expected_total is not None and final_size != expected_total:
            raise IOError(f"partial response incomplete: expected {expected_total}, got {final_size} bytes")
        if expected_total is None and response.status == 200:
            expected_length = response_headers.get("content-length")
            if expected_length:
                try:
                    if int(expected_length) != len(body):
                        raise IOError(f"response size mismatch: expected {expected_length}, got {len(body)} bytes")
                except ValueError:
                    pass

        _finish_partial(partial, destination)
        return {
            "path": destination,
            "url": url,
            "content_type": response_headers.get("content-type", "").split(";", 1)[0].strip().lower(),
            "bytes": final_size,
            "sha256": sha256_file(destination),
        }
    except Exception:
        raise
    finally:
        response.dispose()


async def download_async(
    context,
    url: str,
    destination: Path,
    *,
    referer: str | None = None,
    timeout_ms: int = 60_000,
    policy: RetryPolicy | None = None,
    logger=None,
) -> dict:
    policy = policy or RetryPolicy()
    partial = Path(f"{destination}.part")
    partial_size = partial.stat().st_size if partial.exists() else 0
    response = await _download_response_async(context, url, _headers(referer, partial_size), timeout_ms, policy, logger)
    try:
        if response.status == 416 and partial_size:
            _clear_partial(partial)
            raise IOError("partial download range is no longer valid")
        if not response.ok:
            raise IOError(f"HTTP {response.status}")

        body = await response.body()
        if not body:
            raise IOError("empty response body")
        response_headers = dict(response.headers)
        append = response.status == 206 and partial_size > 0 and _parse_content_range_start(response_headers.get("content-range")) == partial_size
        if response.status == 206 and partial_size > 0 and not append:
            _clear_partial(partial)
            append = False

        if not append:
            partial_size = 0
        _write_partial(partial, body, append=append)

        expected_total = _parse_content_range_total(response_headers.get("content-range")) if append else None
        final_size = partial.stat().st_size
        if expected_total is not None and final_size != expected_total:
            raise IOError(f"partial response incomplete: expected {expected_total}, got {final_size} bytes")
        if expected_total is None and response.status == 200:
            expected_length = response_headers.get("content-length")
            if expected_length:
                try:
                    if int(expected_length) != len(body):
                        raise IOError(f"response size mismatch: expected {expected_length}, got {len(body)} bytes")
                except ValueError:
                    pass

        _finish_partial(partial, destination)
        return {
            "path": destination,
            "url": url,
            "content_type": response_headers.get("content-type", "").split(";", 1)[0].strip().lower(),
            "bytes": final_size,
            "sha256": sha256_file(destination),
        }
    finally:
        await response.dispose()
