from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, TypeVar
from urllib.parse import quote

import pytest
from e2e_http import AuthHeaders, NoBody, Result, UnauthorizedError, post_form_external
from llm_translation.github_copilot.support import (
    CredentialView,
    FrozenModel,
    GithubHeaders,
    GithubUser,
    account_token_file,
    configured_host,
    github_api_base,
    read_secret,
    required,
    upstream,
)
from models import KeyDeleteBody, KeyGenerateBody, KeyGenerateResponse
from pydantic import Field, SecretStr
from transport import HttpTransport

Prefix = Literal["SHARED", "USER_A", "USER_B"]
PREFIXES: Final[tuple[Prefix, ...]] = ("SHARED", "USER_A", "USER_B")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class AuthAccount:
    prefix: Prefix
    login: str = field(repr=False)
    credential: str
    token_file: Path
    user_id: str | None = None


def configured_accounts(environ: Mapping[str, str]) -> tuple[AuthAccount, ...]:
    accounts: Final = tuple(
        AuthAccount(
            prefix=prefix,
            login=required(environ, f"{prefix}_LOGIN"),
            credential=required(environ, f"{prefix}_CREDENTIAL"),
            token_file=account_token_file(environ, prefix),
            user_id=required(environ, f"{prefix}_ID") if prefix != "SHARED" else None,
        )
        for prefix in PREFIXES
        if environ.get(f"E2E_GITHUB_COPILOT_{prefix}_LOGIN", "").strip()
    )
    if len(frozenset(account.token_file.resolve() for account in accounts)) != len(accounts):
        pytest.fail("Selected accounts must use separate TOKEN_FILE paths.", pytrace=False)
    user_ids: Final = tuple(account.user_id for account in accounts if account.user_id is not None)
    if len(frozenset(user_ids)) != len(user_ids):
        pytest.fail("Per-user accounts must use different LiteLLM user IDs.", pytrace=False)
    return accounts


class DeviceStart(FrozenModel):
    verification_uri: str
    user_code: SecretStr
    device_code: SecretStr | None = None
    flow_handle: SecretStr | None = None
    expires_in: int = Field(gt=0)
    interval: int = Field(default=5, gt=0)


class Poll(FrozenModel):
    status: str | None = None
    error: str | None = None
    access_token: SecretStr | None = None
    github_login: str | None = Field(default=None, repr=False)
    interval: int | None = Field(default=None, gt=0)


class DeviceRequest(FrozenModel):
    client_id: str
    scope: str = "read:user"


class TokenRequest(FrozenModel):
    client_id: str
    device_code: str = Field(repr=False)
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"


class ProxyPoll(FrozenModel):
    flow_handle: str = Field(repr=False)


@dataclass(frozen=True, slots=True)
class SavedConnection:
    token: SecretStr
    host: str
    login: str | None = field(default=None, repr=False)


async def read_saved_connection(account: AuthAccount, environ: Mapping[str, str]) -> SavedConnection | None:
    from prisma.engine.errors import EngineConnectionError
    from prisma.errors import PrismaError

    from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
    from litellm.proxy.credential_endpoints.user_provider_credentials import (
        get_user_provider_credential,
        list_user_provider_credentials,
    )
    from litellm.proxy.utils import PrismaClient, ProxyLogging

    if account.user_id is None:
        pytest.fail("Only per-user connections can be loaded from the test database.", pytrace=False)
    client: Final = PrismaClient(
        database_url=environ["DATABASE_URL"],
        proxy_logging_obj=ProxyLogging(user_api_key_cache=UserApiKeyCache()),
    )
    try:
        await client.connect()
        try:
            payload: Final = await get_user_provider_credential(client, account.user_id, account.credential)
            if payload is None:
                rows: Final = await list_user_provider_credentials(client, account.user_id)
                if any(row.credential_name == account.credential for row in rows):
                    pytest.fail(
                        f"[{account.prefix}] The saved connection could not be decrypted. "
                        "Check the test database and LITELLM_SALT_KEY. No authorization was started.",
                        pytrace=False,
                    )
                return None
            return SavedConnection(SecretStr(payload.access_token), payload.github_host, payload.github_login)
        finally:
            await client.disconnect()
    except (PrismaError, EngineConnectionError, OSError) as error:
        pytest.fail(
            f"[{account.prefix}] Could not load the saved connection from the test database "
            f"({type(error).__name__}). Check DATABASE_URL. Database error details are not printed.",
            pytrace=False,
        )


def verified_connection(
    saved: SavedConnection | None,
    host: str,
    verify: Callable[[SecretStr], Result[GithubUser]],
) -> tuple[SecretStr, str] | None:
    if saved is None:
        return None
    if saved.host != host:
        pytest.fail("The saved connection belongs to another GitHub host. No token was sent.", pytrace=False)
    if not saved.token.get_secret_value().strip() or saved.login == "":
        pytest.fail("The saved connection is incomplete. No authorization was started.", pytrace=False)
    result: Final = verify(saved.token)
    if isinstance(result, UnauthorizedError):
        return None
    user: Final = upstream(result, "saved token verification")
    if saved.login is not None and user.login.casefold() != saved.login.casefold():
        pytest.fail(
            "The saved connection and GitHub identities disagree. The token file was not changed.", pytrace=False
        )
    return saved.token, user.login


def reuse_or_authorize(
    account: AuthAccount,
    host: str,
    *,
    read: Callable[[], SavedConnection | None],
    verify: Callable[[SecretStr], Result[GithubUser]],
    authorize: Callable[[], SavedConnection],
) -> tuple[SecretStr, str]:
    existing: Final = verified_connection(read(), host, verify)
    if existing is not None:
        sys.stdout.write(f"[{account.prefix}] Reusing the saved connection after direct GitHub verification.\n")
        return existing
    sys.stdout.write(f"[{account.prefix}] No valid saved connection. Starting device authorization.\n")
    sys.stdout.flush()
    connected: Final = verified_connection(authorize(), host, verify)
    if connected is None:
        pytest.fail("The newly authorized connection is missing or rejected by GitHub.", pytrace=False)
    return connected


class Waiter(Protocol):
    def wait(self, seconds: float) -> None: ...


def complete(
    flow: DeviceStart,
    poll: Callable[[], Result[Poll]],
    waiter: Waiter,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Poll:
    deadline: Final = clock() + flow.expires_in
    interval: float = flow.interval  # rebind-ok: OAuth slow-down responses change the required polling interval
    while clock() + interval < deadline:
        waiter.wait(interval)
        match upstream(poll(), "device authorization polling"):
            case Poll() as result if result.access_token is not None or result.status == "connected":
                return result
            case Poll(status="slow_down", interval=delay) | Poll(error="slow_down", interval=delay):
                interval = max(interval + 5, delay or 0)  # rebind-ok: honor cumulative OAuth slow-down replies
            case Poll(status="pending" | "authorization_pending") | Poll(error="pending" | "authorization_pending"):
                pass
            case _:
                pytest.fail("Device authorization was denied, expired or rejected. Run --auth again.", pytrace=False)
    pytest.fail("Device authorization expired. Run --auth again to obtain a fresh code.", pytrace=False)


def save_token(path: Path, token: SecretStr) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".copilot-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token.get_secret_value() + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def provision(
    accounts: tuple[AuthAccount, ...],
    authorize: Callable[[AuthAccount], tuple[SecretStr, str]],
    *,
    seen: frozenset[str] = frozenset(),
) -> None:
    if not accounts:
        return
    account: Final = accounts[0]
    token, identity = authorize(account)
    if account.user_id is not None and identity.casefold() in seen:
        pytest.fail(
            "Per-user A and B must use different GitHub accounts. The token file was not changed.", pytrace=False
        )
    save_token(account.token_file, token)
    sys.stdout.write(f"[{account.prefix}] Authentication saved. No tests were run.\n")
    provision(
        accounts[1:],
        authorize,
        seen=seen | frozenset((identity.casefold(),)) if account.user_id is not None else seen,
    )


@contextmanager
def proxy_caller(
    account: AuthAccount, environ: Mapping[str, str], host: str
) -> Generator[tuple[HttpTransport, AuthHeaders]]:
    base: Final = environ.get("LITELLM_CONTROL_PLANE_URL") or environ.get("LITELLM_PROXY_URL", "")
    master: Final = environ.get("LITELLM_MASTER_KEY", "")
    if not base or not master:
        pytest.fail("Per-user setup requires LITELLM_PROXY_URL and LITELLM_MASTER_KEY.", pytrace=False)
    transport: Final = HttpTransport(base_url=base, master_key=master)
    credential: Final = upstream(
        transport.get(
            f"/credentials/by_name/{quote(account.credential, safe='')}",
            headers=transport.master,
            params=NoBody(),
            response_type=CredentialView,
        ),
        "per-user credential validation",
    ).credential_values
    if credential.github_copilot_host != host or credential.github_copilot_auth_type != "per_user_oauth":
        pytest.fail(
            "The per-user proxy credential must match the configured host and authentication mode.", pytrace=False
        )
    key: Final = upstream(
        transport.post(
            "/key/generate",
            headers=transport.master,
            json=KeyGenerateBody(user_id=account.user_id, duration="1h"),
            response_type=KeyGenerateResponse,
        ),
        "temporary user key creation",
    ).key
    try:
        yield transport, transport.bearer(key)
    finally:
        upstream(
            transport.post(
                "/key/delete", headers=transport.master, json=KeyDeleteBody(keys=[key]), response_type=NoBody
            ),
            "temporary user key cleanup",
        )


def run(environ: Mapping[str, str]) -> int:
    from llm_translation.github_copilot.auth_browser import browser_step, open_approval
    from playwright.sync_api import BrowserContext, sync_playwright

    from litellm.llms.github_copilot.authenticator import DEFAULT_GITHUB_CLIENT_ID

    accounts: Final = configured_accounts(environ)
    app_selected: Final = bool(environ.get("E2E_GITHUB_COPILOT_APP_ORGANIZATION", "").strip())
    host: Final = configured_host(environ)
    for prefix in PREFIXES:
        if not any(account.prefix == prefix for account in accounts):
            sys.stdout.write(f"[{prefix}] Skipped, no LOGIN configured.\n")
    if not accounts and not app_selected:
        pytest.fail("No accounts configured. Set an E2E_GITHUB_COPILOT_*_LOGIN or APP_ORGANIZATION.", pytrace=False)
    if any(account.user_id is not None for account in accounts):
        missing: Final = tuple(
            name for name in ("DATABASE_URL", "LITELLM_SALT_KEY") if not environ.get(name, "").strip()
        )
        if missing:
            pytest.fail(
                f"Per-user auth setup requires {' and '.join(missing)} from the dedicated test proxy. "
                "Routine API tests do not need database access.",
                pytrace=False,
            )
    if host != "github.com" and any(account.prefix == "SHARED" for account in accounts):
        sys.stdout.write(
            "[SHARED] Enterprise login can be provisioned, but this revision still has a shared inference-routing gap.\n"
        )
    sys.stdout.flush()
    client_id: Final = environ.get("E2E_GITHUB_COPILOT_OAUTH_CLIENT_ID") or DEFAULT_GITHUB_CLIENT_ID

    def connect_proxy(account: AuthAccount, context: BrowserContext) -> str:
        with proxy_caller(account, environ, host) as (transport, headers):
            path: Final = f"/credentials/{quote(account.credential, safe='')}/user_connection"
            flow: Final = upstream(
                transport.post(f"{path}/start", headers=headers, json=NoBody(), response_type=DeviceStart),
                "proxy device authorization start",
            )
            if flow.flow_handle is None:
                pytest.fail("The proxy did not return a device-flow handle.", pytrace=False)
            handle: Final = flow.flow_handle.get_secret_value()
            sys.stdout.write(
                f"[{account.prefix} proxy connection] Sign in as {account.login}. Complete password, MFA and approval.\n"
            )
            sys.stdout.flush()
            approval: Final = open_approval(
                context,
                host=host,
                login=account.login,
                label=f"{account.prefix} proxy connection",
                uri=flow.verification_uri,
                code=flow.user_code.get_secret_value(),
            )
            try:
                connected: Final = complete(
                    flow,
                    lambda: transport.post(
                        f"{path}/poll",
                        headers=headers,
                        json=ProxyPoll(flow_handle=handle),
                        response_type=Poll,
                    ),
                    approval,
                )
                if not connected.github_login:
                    pytest.fail("The saved proxy connection has no GitHub identity.", pytrace=False)
                return connected.github_login
            finally:
                approval.close()

    def authorize_shared(account: AuthAccount, context: BrowserContext) -> SavedConnection:
        direct: Final = upstream(
            post_form_external(
                f"https://{host}/login/device/code",
                form=DeviceRequest(client_id=client_id),
                headers=GithubHeaders(accept="application/json"),
                response_type=DeviceStart,
            ),
            "shared device authorization start",
        )
        if direct.device_code is None:
            pytest.fail("GitHub did not return a device code.", pytrace=False)
        device_code: Final = direct.device_code.get_secret_value()
        sys.stdout.write(
            f"[{account.prefix} shared connection] Sign in as {account.login}. Approve the shared login.\n"
        )
        sys.stdout.flush()
        runner_approval: Final = open_approval(
            context,
            host=host,
            login=account.login,
            label=f"{account.prefix} shared connection",
            uri=direct.verification_uri,
            code=direct.user_code.get_secret_value(),
        )
        try:
            result: Final = complete(
                direct,
                lambda: post_form_external(
                    f"https://{host}/login/oauth/access_token",
                    form=TokenRequest(client_id=client_id, device_code=device_code),
                    headers=GithubHeaders(accept="application/json"),
                    response_type=Poll,
                ),
                runner_approval,
            )
        finally:
            runner_approval.close()
        if result.access_token is None:
            pytest.fail("GitHub did not return a shared token.", pytrace=False)
        return SavedConnection(result.access_token, host)

    def in_browser(account: AuthAccount, action: Callable[[BrowserContext], _T]) -> _T:
        with (
            browser_step("browser setup", "Launching or closing Chromium"),
            sync_playwright() as playwright,
            playwright.chromium.launch(headless=False) as browser,
            browser_step(account.prefix, "Creating or closing the account browser context"),
            browser.new_context() as context,
        ):
            return action(context)

    def read_connection(account: AuthAccount) -> SavedConnection | None:
        if account.user_id is not None:
            return asyncio.run(read_saved_connection(account, environ))
        if account.token_file.exists():
            return SavedConnection(read_secret(environ, "SHARED_TOKEN_FILE"), host)
        return None

    def authorize_new(account: AuthAccount) -> SavedConnection:
        if account.user_id is None:
            return in_browser(account, lambda context: authorize_shared(account, context))
        expected: Final = in_browser(account, lambda context: connect_proxy(account, context))
        saved: Final = read_connection(account)
        if saved is None or saved.login is None or saved.login.casefold() != expected.casefold():
            pytest.fail(
                f"[{account.prefix}] The saved proxy connection does not match the approved GitHub identity. "
                "The token file was not changed.",
                pytrace=False,
            )
        return saved

    def verify_token(token: SecretStr) -> Result[GithubUser]:
        return HttpTransport(base_url=github_api_base(host), master_key="").get(
            "/user",
            headers=GithubHeaders(authorization=f"Bearer {token.get_secret_value()}"),
            params=NoBody(),
            response_type=GithubUser,
        )

    provision(
        accounts,
        lambda account: reuse_or_authorize(
            account,
            host,
            read=lambda: read_connection(account),
            verify=verify_token,
            authorize=lambda: authorize_new(account),
        ),
    )
    if app_selected:
        from llm_translation.github_copilot.auth_app import run as run_app

        run_app(environ)
    else:
        sys.stdout.write("[APP] Skipped, no APP_ORGANIZATION configured.\n")
    return 0
