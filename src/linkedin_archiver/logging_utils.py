"""Shared logging setup.

Every script gets its own log file (named after the script) plus console
output, per the project's logging requirements: timestamps, level, and
enough context to debug after the terminal is closed. Never log cookies,
auth headers, session tokens, or credentials.
"""

from __future__ import annotations

import logging
from pathlib import Path

from linkedin_archiver.paths import logs_dir, ensure_dir

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    script_name: str,
    *,
    log_dir: Path | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """Configure and return a logger for ``script_name``.

    Log file: ``<log_dir>/<script_name>.log`` (default: project ``logs/``).
    Safe to call more than once per process; handlers are not duplicated.
    """
    logger = logging.getLogger(script_name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        # Already configured (e.g. re-entrant call). Leave it alone.
        return logger

    target_dir = ensure_dir(log_dir if log_dir is not None else logs_dir())
    log_file = target_dir / f"{script_name}.log"

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    logger.debug(f"Logging initialized. Log file: {log_file}")

    return logger
