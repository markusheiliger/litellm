from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Final
from urllib.parse import urlsplit

import pytest
from e2e_metadata import step
from playwright.sync_api import BrowserContext, Error, Page, Request, Response, TimeoutError


def _browser_reason(error: Error) -> str:
    if error.message.startswith("BrowserType.launch:"):
        if "Executable doesn't exist" in error.message:
            return "Chromium is missing. Run 'uv run --no-sync playwright install chromium'."
        if "error while loading shared libraries" in error.message:
            return (
                "Chromium system libraries are missing. Run "
                "'sudo -A .venv/bin/python -m playwright install-deps chromium'."
            )
        if "Missing X server or $DISPLAY" in error.message or "cannot open display" in error.message:
            return "The graphical display is unavailable. Use a visible graphical session."
    if isinstance(error, TimeoutError):
        return "The browser operation timed out."
    if "Target page, context or browser has been closed" in error.message:
        return "The approval page, browser context or browser was closed."
    if "Execution context was destroyed" in error.message or "interrupted by another navigation" in error.message:
        return "Page navigation interrupted the browser operation."
    network: Final = re.match(r"[A-Za-z]+\.[a-z_]+: (net::ERR_[A-Z0-9_]+)\b", error.message)
    if network is not None:
        return f"Browser network failure {network.group(1)}."
    return "Playwright rejected the operation. Raw error details are withheld to protect authentication data."


@contextmanager
def browser_step(label: str, operation: str, *, location: Callable[[], str] | None = None) -> Generator[None]:
    try:
        yield
    except Error as error:
        parsed: Final = urlsplit(location() if location is not None else "")
        origin: Final = (
            f"{parsed.scheme}://{parsed.hostname}"
            if parsed.scheme in ("http", "https") and parsed.hostname
            else "unavailable"
        )
        api: Final = re.match(r"(?:BrowserType|Browser|BrowserContext|Page|Locator)\.[a-z_]+(?=:)", error.message)
        action: Final = f" in {api.group()}" if api is not None else ""
        pytest.fail(
            f"[{label}] {operation} failed with {type(error).__name__}{action}. "
            f"{_browser_reason(error)} Last browser origin {origin}. "
            "URL paths, queries and browser call logs are not printed.",
            pytrace=False,
        )


def prefill(page: Page, *, host: str, login: str, code: str, label: str = "device authorization") -> None:
    with browser_step(label, "Prefilling sign-in or device-code fields", location=lambda: page.url):
        if page.is_closed():
            pytest.fail(f"[{label}] The approval page was closed before authorization completed.", pytrace=False)
        parsed: Final = urlsplit(page.url)
        if parsed.scheme != "https":
            return
        username: Final = page.locator(
            'input[autocomplete="username"]:visible, input[type="email"]:visible, '
            'input[name="loginfmt"]:visible, input[name="login"]:visible'
        ).first
        if username.is_visible() and not username.input_value():
            username.fill(login)
        if parsed.hostname == host and "/sso" in parsed.path:
            proceed: Final = page.get_by_role("button", name="Continue", exact=True)
            if proceed.is_visible():
                proceed.click(no_wait_after=True)
            return
        if parsed.hostname != host or not parsed.path.startswith("/login/device"):
            return
        field: Final = page.locator('input[name="user_code"]:visible, input#user_code:visible').first
        if field.is_visible():
            if field.input_value() != code:
                field.fill(code)
            return
        digits: Final = page.locator('input[id^="user-code-"]:visible, input[name="user_code[]"]:visible')
        characters: Final = code.replace("-", "")
        if digits.count() == len(characters):
            for index, character in enumerate(characters):
                if digits.nth(index).input_value() != character:
                    digits.nth(index).fill(character)


@dataclass(frozen=True, slots=True)
class Approval:
    page: Page
    host: str
    login: str = field(repr=False)
    code: str = field(repr=False)
    label: str = "device authorization"
    navigation_errors: SimpleQueue[str] = field(default_factory=SimpleQueue, repr=False, compare=False)

    @step("Watch account-navigation failures")
    def monitor_navigation(self) -> None:
        self.page.on("response", lambda response: self._observe_response(response))
        self.page.on("requestfailed", lambda request: self._observe_failure(request))

    def _observe_response(self, response: Response) -> None:
        if (
            response.status >= 400
            and response.request.is_navigation_request()
            and response.request.frame == self.page.main_frame
        ):
            self.navigation_errors.put(f"HTTP {response.status}")

    def _observe_failure(self, request: Request) -> None:
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            reason: Final = request.failure or ""
            if reason == "net::ERR_ABORTED":
                return
            self.navigation_errors.put(reason if re.fullmatch(r"net::ERR_[A-Z0-9_]+", reason) else "network failure")

    def wait(self, seconds: float) -> None:
        deadline: Final = time.monotonic() + seconds
        with browser_step(self.label, "Waiting for browser interaction", location=lambda: self.page.url):
            while time.monotonic() < deadline:
                if not self.navigation_errors.empty():
                    pytest.fail(
                        f"[{self.label}] Account navigation failed ({self.navigation_errors.get_nowait()}). "
                        "Check the sign-in service, organization policy and the visible browser error. "
                        "Response bodies and redirect parameters are not printed.",
                        pytrace=False,
                    )
                prefill(self.page, host=self.host, login=self.login, code=self.code, label=self.label)
                self.page.wait_for_timeout(min(200, max(0, deadline - time.monotonic()) * 1000))

    def close(self) -> None:
        with browser_step(self.label, "Closing the approval tab", location=lambda: self.page.url):
            self.page.close()


@step("Open the labeled account approval page")
def open_account_page(
    context: BrowserContext, *, host: str, login: str, label: str, uri: str, code: str = ""
) -> Approval:
    parsed: Final = urlsplit(uri)
    if parsed.scheme != "https" or parsed.netloc != host or parsed.fragment:
        pytest.fail("Unsupported account approval URL. No credentials were sent to it.", pytrace=False)
    with browser_step(label, "Opening the approval tab", location=lambda: uri):
        page: Final = context.new_page()
    title: Final = json.dumps(f"Copilot {label} | {login} | ")
    with browser_step(label, "Initializing the approval tab", location=lambda: page.url):
        page.add_init_script(
            f"document.addEventListener('DOMContentLoaded', () => {{ document.title = {title} + document.title; }});"
        )
    approval: Final = Approval(page=page, host=host, login=login, code=code, label=label)
    approval.monitor_navigation()
    with browser_step(
        label, "Navigating to account approval", location=lambda: uri if page.url == "about:blank" else page.url
    ):
        page.goto(uri, wait_until="domcontentloaded")
    return approval


def open_approval(context: BrowserContext, *, host: str, login: str, label: str, uri: str, code: str) -> Approval:
    parsed: Final = urlsplit(uri)
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or not parsed.path.startswith("/login/device")
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail("Unsupported device verification URL. No approval code was sent to it.", pytrace=False)
    return open_account_page(context, host=host, login=login, label=label, uri=uri, code=code)
