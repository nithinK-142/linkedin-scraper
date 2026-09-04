"""Fail-closed detection for LinkedIn access and restriction signals."""

from __future__ import annotations

from dataclasses import dataclass

from linkedin_archiver.settings import LOGIN_MARKERS


@dataclass(frozen=True)
class SafetySignal:
    kind: str
    message: str
    url: str = ""


class SafetyStop(RuntimeError):
    """Stop the run instead of repeatedly requesting a restricted session."""

    def __init__(self, signal: SafetySignal):
        super().__init__(signal.message)
        self.signal = signal


_RESTRICTION_PHRASES = (
    "unusually large number of page views",
    "temporarily restricted",
    "we've restricted your account",
    "we have restricted your account",
    "your account has been restricted",
    "restricted action",
)

_SECURITY_TITLE_PHRASES = (
    "security verification",
    "verify your identity",
    "captcha",
)


def inspect_url_and_text(url: str, text: str = "", title: str = "") -> SafetySignal | None:
    lowered_url = (url or "").lower()
    for marker in LOGIN_MARKERS:
        if marker in lowered_url:
            return SafetySignal("login_or_challenge", "LinkedIn session requires login or security verification.", url)

    lowered_text = (text or "").lower()
    for phrase in _RESTRICTION_PHRASES:
        if phrase in lowered_text:
            return SafetySignal("restriction", "LinkedIn restriction signal detected. Stopping instead of retrying.", url)

    lowered_title = (title or "").lower()
    for phrase in _SECURITY_TITLE_PHRASES:
        if phrase in lowered_title:
            return SafetySignal("challenge", "LinkedIn security challenge detected. Stopping instead of retrying.", url)
    return None


def check_response_status(url: str, status: int) -> SafetySignal | None:
    if "linkedin.com" in (url or "").lower() and status == 429:
        return SafetySignal("rate_limit", "LinkedIn returned HTTP 429. Stopping page processing.", url)
    return None


class SafetyMonitor:
    def __init__(self) -> None:
        self.signal: SafetySignal | None = None

    def observe_response(self, url: str, status: int) -> None:
        if self.signal is None:
            self.signal = check_response_status(url, status)

    def raise_if_triggered(self) -> None:
        if self.signal is not None:
            raise SafetyStop(self.signal)
