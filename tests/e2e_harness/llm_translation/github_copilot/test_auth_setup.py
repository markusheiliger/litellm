from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from queue import SimpleQueue
from typing import Final

import pytest
from e2e_http import NetworkError, Result, Success, UnauthorizedError
from llm_translation.github_copilot.auth_browser import browser_step
from llm_translation.github_copilot.auth_setup import (
    AuthAccount,
    DeviceStart,
    Poll,
    SavedConnection,
    complete,
    configured_accounts,
    provision,
    reuse_or_authorize,
    run,
    save_token,
)
from llm_translation.github_copilot.support import GithubUser, read_secret
from playwright.sync_api import Error, TimeoutError
from pydantic import SecretStr


def test_missing_browser_reports_the_failed_stage_without_exposing_private_paths(tmp_path: Path) -> None:
    e2e: Final = Path(__file__).resolve().parents[4] / "tests/e2e"
    private_path: Final = tmp_path / "private-browser-path"
    result: Final = subprocess.run(
        [sys.executable, str(e2e / "llm_translation/github_copilot/runner.py"), "--auth"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(e2e),
            "PYTHON_DOTENV_DISABLED": "1",
            "PLAYWRIGHT_BROWSERS_PATH": str(private_path),
            "E2E_GITHUB_COPILOT_HOST": "github.com",
            "E2E_GITHUB_COPILOT_SHARED_LOGIN": "dedicated-test-login",
            "E2E_GITHUB_COPILOT_SHARED_CREDENTIAL": "shared",
            "E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE": str(tmp_path / "token"),
            "E2E_GITHUB_COPILOT_USER_A_LOGIN": "",
            "E2E_GITHUB_COPILOT_USER_B_LOGIN": "",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "[browser setup]" in result.stderr
    assert "BrowserType.launch" in result.stderr
    assert "playwright install chromium" in result.stderr
    assert str(private_path) not in result.stderr
    assert not (tmp_path / "token").exists()


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (TimeoutError("Locator.click: Timeout 30000ms exceeded."), "timed out"),
        (Error("Page.goto: net::ERR_CONNECTION_RESET at"), "net::ERR_CONNECTION_RESET"),
        (Error("Locator.fill: Execution context was destroyed"), "navigation interrupted"),
        (Error("Page.goto: Target page, context or browser has been closed"), "was closed"),
        (Error("Locator.fill: Unexpected browser response"), "Raw error details are withheld"),
    ),
)
def test_browser_errors_identify_the_approval_and_operation_without_leaking_auth_data(
    error: Error, reason: str
) -> None:
    secret: Final = "synthetic-secret-never-log"
    url: Final = f"https://private-user:private-password@login.example.test/{secret}?code={secret}#{secret}"
    with pytest.raises(pytest.fail.Exception) as failure:
        with browser_step("USER_B proxy connection", "Prefilling sign-in", location=lambda: url):
            raise type(error)(f"{error.message} {secret}\nCall log: filling device code {secret}")
    message: Final = str(failure.value)
    assert "[USER_B proxy connection]" in message
    assert "Prefilling sign-in" in message
    assert type(error).__name__ in message
    assert reason in message
    assert "https://login.example.test" in message
    assert secret not in message
    assert "private-user" not in message
    assert "private-password" not in message
    assert "playwright install" not in message


def test_only_accounts_with_nonempty_login_are_selected(tmp_path: Path) -> None:
    accounts: Final = configured_accounts(
        {
            "E2E_GITHUB_COPILOT_SHARED_LOGIN": " ",
            "E2E_GITHUB_COPILOT_SHARED_CREDENTIAL": "shared",
            "E2E_GITHUB_COPILOT_USER_A_ID": "id-without-login",
            "E2E_GITHUB_COPILOT_USER_B_LOGIN": "dedicated-login",
            "E2E_GITHUB_COPILOT_USER_B_ID": "user-b-id",
            "E2E_GITHUB_COPILOT_USER_B_CREDENTIAL": "users",
            "E2E_GITHUB_COPILOT_USER_B_TOKEN_FILE": str(tmp_path / "token"),
        }
    )
    assert accounts == (AuthAccount("USER_B", "dedicated-login", "users", tmp_path / "token", "user-b-id"),)
    assert configured_accounts({}) == ()


def test_defined_account_with_missing_configuration_fails_instead_of_skipping() -> None:
    with pytest.raises(pytest.fail.Exception, match="SHARED_CREDENTIAL"):
        configured_accounts({"E2E_GITHUB_COPILOT_SHARED_LOGIN": "dedicated-login"})


def test_setup_and_tests_share_derived_private_paths_without_per_user_token_settings(tmp_path: Path) -> None:
    environ: Final = {
        "XDG_DATA_HOME": str(tmp_path),
        "E2E_GITHUB_COPILOT_HOST": "test.ghe.com",
        "E2E_GITHUB_COPILOT_USER_A_LOGIN": "a",
        "E2E_GITHUB_COPILOT_USER_A_CREDENTIAL": "users",
        "E2E_GITHUB_COPILOT_USER_A_ID": "id-a",
        "E2E_GITHUB_COPILOT_USER_B_LOGIN": "b",
        "E2E_GITHUB_COPILOT_USER_B_CREDENTIAL": "users",
        "E2E_GITHUB_COPILOT_USER_B_ID": "id-b",
    }
    accounts: Final = configured_accounts(environ)
    assert len(accounts) == 2 and accounts[0].token_file != accounts[1].token_file
    for account in accounts:
        assert account.token_file.is_relative_to(tmp_path)
        save_token(account.token_file, SecretStr(f"saved-{account.prefix}"))
        assert read_secret(environ, f"{account.prefix}_TOKEN_FILE") == SecretStr(f"saved-{account.prefix}")
    other_host: Final = configured_accounts({**environ, "E2E_GITHUB_COPILOT_HOST": "other.ghe.com"})
    assert frozenset(account.token_file for account in accounts).isdisjoint(
        account.token_file for account in other_host
    )


def test_relative_data_home_cannot_put_default_tokens_in_the_working_directory() -> None:
    with pytest.raises(pytest.fail.Exception, match="XDG_DATA_HOME must be absolute"):
        read_secret({"XDG_DATA_HOME": "relative-data"}, "USER_A_TOKEN_FILE")


def test_per_user_auth_requires_explicit_database_configuration_before_starting_auth(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="DATABASE_URL and LITELLM_SALT_KEY"):
        run(
            {
                "XDG_DATA_HOME": str(tmp_path),
                "E2E_GITHUB_COPILOT_USER_A_LOGIN": "a",
                "E2E_GITHUB_COPILOT_USER_A_CREDENTIAL": "users",
                "E2E_GITHUB_COPILOT_USER_A_ID": "id-a",
            }
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_app_only_setup_reaches_app_validation_without_requiring_device_accounts(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="APP_ORGANIZATION must be"):
        run(
            {
                "XDG_DATA_HOME": str(tmp_path),
                "E2E_GITHUB_COPILOT_APP_ORGANIZATION": "https://not-an-organization.test/",
            }
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_valid_proxy_token_is_copied_exactly_without_device_authorization(tmp_path: Path) -> None:
    account: Final = AuthAccount("USER_A", "a", "users", tmp_path / "token", "id-a")
    saved: Final = SavedConnection(SecretStr("proxy-saved-token"), "github.com", "test-user")
    verified: Final = SimpleQueue[SecretStr]()

    def verify(token: SecretStr) -> Result[GithubUser]:
        verified.put(token)
        return Success(status_code=200, data=GithubUser(login="test-user"))

    def authorize() -> SavedConnection:
        raise AssertionError("A valid saved connection must not start another device authorization")

    provision(
        (account,),
        lambda item: reuse_or_authorize(item, "github.com", read=lambda: saved, verify=verify, authorize=authorize),
    )
    assert verified.get_nowait() == saved.token
    assert verified.empty()
    assert account.token_file.read_text().strip() == saved.token.get_secret_value()


@pytest.mark.parametrize("expired", (False, True))
def test_missing_or_expired_connection_uses_one_approval_and_the_new_proxy_token(tmp_path: Path, expired: bool) -> None:
    account: Final = AuthAccount("USER_A", "a", "users", tmp_path / "token", "id-a")
    saved: Final = SavedConnection(SecretStr("expired"), "github.com", "test-user") if expired else None
    fresh: Final = SavedConnection(SecretStr("persisted-after-approval"), "github.com", "test-user")
    events: Final = SimpleQueue[str]()

    def authorize() -> SavedConnection:
        events.put("authorize")
        return fresh

    def verify(token: SecretStr) -> Result[GithubUser]:
        events.put(f"verify-{token.get_secret_value()}")
        return (
            UnauthorizedError()
            if token == SecretStr("expired")
            else Success(status_code=200, data=GithubUser(login="test-user"))
        )

    token, login = reuse_or_authorize(account, "github.com", read=lambda: saved, verify=verify, authorize=authorize)
    assert (token, login) == (fresh.token, fresh.login)
    assert tuple(events.get_nowait() for _ in range(events.qsize())) == (
        *(("verify-expired",) if expired else ()),
        "authorize",
        "verify-persisted-after-approval",
    )


@pytest.mark.parametrize(
    ("failure", "message"),
    (("host", "another GitHub host"), ("identity", "identities disagree"), ("network", "saved token verification")),
)
def test_connection_errors_do_not_start_auth_or_overwrite_a_saved_token(
    tmp_path: Path, failure: str, message: str
) -> None:
    account: Final = AuthAccount("USER_A", "a", "users", tmp_path / "token", "id-a")
    save_token(account.token_file, SecretStr("unchanged"))
    saved: Final = SavedConnection(
        SecretStr("saved-token"), "other.ghe.com" if failure == "host" else "github.com", "test-user"
    )

    def verify(token: SecretStr) -> Result[GithubUser]:
        assert failure != "host", "A token must not be sent to a different host"
        return (
            NetworkError(message="synthetic network failure")
            if failure == "network"
            else Success(status_code=200, data=GithubUser(login="different-user"))
        )

    def authorize() -> SavedConnection:
        raise AssertionError("Configuration and network errors must not start another device authorization")

    with pytest.raises(pytest.fail.Exception, match=message):
        provision(
            (account,),
            lambda item: reuse_or_authorize(item, "github.com", read=lambda: saved, verify=verify, authorize=authorize),
        )
    assert account.token_file.read_text().strip() == "unchanged"


@pytest.mark.parametrize(("duplicate", "message"), (("ID", "user IDs"), ("TOKEN_FILE", "TOKEN_FILE")))
def test_accounts_cannot_overwrite_another_accounts_connection_or_token(
    tmp_path: Path, duplicate: str, message: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match=message):
        configured_accounts(
            {
                "E2E_GITHUB_COPILOT_USER_A_LOGIN": "a",
                "E2E_GITHUB_COPILOT_USER_A_CREDENTIAL": "users",
                "E2E_GITHUB_COPILOT_USER_A_ID": "id-a",
                "E2E_GITHUB_COPILOT_USER_A_TOKEN_FILE": str(tmp_path / "a"),
                "E2E_GITHUB_COPILOT_USER_B_LOGIN": "b",
                "E2E_GITHUB_COPILOT_USER_B_CREDENTIAL": "users",
                "E2E_GITHUB_COPILOT_USER_B_ID": "id-a" if duplicate == "ID" else "id-b",
                "E2E_GITHUB_COPILOT_USER_B_TOKEN_FILE": str(tmp_path / ("a" if duplicate == "TOKEN_FILE" else "b")),
            }
        )


class Clock:
    def __init__(self) -> None:
        self.now: float = 0
        self.delays: SimpleQueue[float] = SimpleQueue()

    def wait(self, seconds: float) -> None:
        self.delays.put(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _flow(expires_in: int = 30) -> DeviceStart:
    return DeviceStart(
        verification_uri="https://github.com/login/device",
        user_code=SecretStr("not-a-real-code"),
        device_code=SecretStr("not-a-real-device-secret"),
        expires_in=expires_in,
        interval=2,
    )


def test_pending_and_slow_down_are_polled_at_the_required_intervals() -> None:
    responses: Final = SimpleQueue[Poll]()
    for response in (
        Poll(error="authorization_pending"),
        Poll(error="slow_down"),
        Poll(error="slow_down", interval=15),
        Poll(access_token=SecretStr("synthetic-token")),
    ):
        responses.put(response)
    clock: Final = Clock()

    def poll() -> Result[Poll]:
        return Success(status_code=200, data=responses.get_nowait())

    result: Final = complete(_flow(expires_in=60), poll, clock, clock=clock)
    assert result.access_token == SecretStr("synthetic-token")
    assert tuple(clock.delays.get_nowait() for _ in range(clock.delays.qsize())) == (2, 2, 7, 15)
    assert responses.empty()


@pytest.mark.parametrize("status", ("denied", "expired", "no_copilot_seat", "unexpected"))
def test_rejected_device_flow_stops_without_an_access_token(status: str) -> None:
    clock: Final = Clock()
    with pytest.raises(pytest.fail.Exception, match="denied, expired or rejected"):
        complete(_flow(), lambda: Success(status_code=200, data=Poll(status=status)), clock, clock=clock)
    assert clock.delays.qsize() == 1


def test_unapproved_device_flow_expires_without_continuing_to_poll() -> None:
    clock: Final = Clock()
    with pytest.raises(pytest.fail.Exception, match="expired"):
        complete(
            _flow(expires_in=4),
            lambda: Success(status_code=200, data=Poll(error="authorization_pending")),
            clock,
            clock=clock,
        )
    assert clock.now == 2


def test_tokens_are_replaced_privately_without_leaving_temporary_files(tmp_path: Path) -> None:
    path: Final = tmp_path / "account/token"
    save_token(path, SecretStr("old"))
    path.chmod(0o644)
    save_token(path, SecretStr("new"))
    assert path.read_text().strip() == "new"
    assert path.stat().st_mode & 0o077 == 0
    assert tuple(path.parent.iterdir()) == (path,)


def test_duplicate_per_user_identity_does_not_replace_the_second_token(tmp_path: Path) -> None:
    a: Final = AuthAccount("USER_A", "a", "users", tmp_path / "a", "id-a")
    b: Final = AuthAccount("USER_B", "b", "users", tmp_path / "b", "id-b")
    save_token(b.token_file, SecretStr("existing-b-token"))
    with pytest.raises(pytest.fail.Exception, match="different GitHub accounts"):
        provision(
            (a, b),
            lambda account: (
                SecretStr(f"new-{account.prefix}"),
                "Same-identity" if account.prefix == "USER_A" else "same-identity",
            ),
        )
    assert a.token_file.read_text().strip() == "new-USER_A"
    assert b.token_file.read_text().strip() == "existing-b-token"


def test_configured_accounts_are_authorized_in_order_with_their_own_tokens(tmp_path: Path) -> None:
    accounts: Final = (
        AuthAccount("SHARED", "shared", "shared", tmp_path / "shared"),
        AuthAccount("USER_A", "a", "users", tmp_path / "a", "id-a"),
        AuthAccount("USER_B", "b", "users", tmp_path / "b", "id-b"),
    )
    observed: Final = SimpleQueue[str]()

    def authorize(account: AuthAccount) -> tuple[SecretStr, str]:
        observed.put(account.prefix)
        return SecretStr(f"token-{account.prefix}"), f"identity-{account.prefix}"

    provision(accounts, authorize)
    assert tuple(observed.get_nowait() for _ in range(observed.qsize())) == ("SHARED", "USER_A", "USER_B")
    assert tuple(account.token_file.read_text().strip() for account in accounts) == (
        "token-SHARED",
        "token-USER_A",
        "token-USER_B",
    )
