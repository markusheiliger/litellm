from __future__ import annotations

import hmac
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from functools import cache
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Final, Literal, Protocol, TypeVar
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest
from dotenv import set_key
from e2e_http import NoBody, Result, UnknownApiError
from e2e_metadata import step
from llm_translation.github_copilot.auth_browser import Approval, browser_step, open_account_page
from llm_translation.github_copilot.auth_setup import save_token
from llm_translation.github_copilot.support import (
    Account,
    CredentialView,
    FrozenModel,
    GithubHeaders,
    InstallationPermissions,
    UpstreamTransport,
    app_headers,
    configured_host,
    github_api_base,
    load_account,
    read_secret,
    required,
    upstream,
)
from playwright.sync_api import BrowserContext, sync_playwright
from pydantic import BaseModel, Field, SecretStr
from transport import HttpTransport

_R = TypeVar("_R", bound=BaseModel)
_ORG: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


class Owner(FrozenModel):
    login: str = Field(repr=False)
    type: Literal["Organization"]


class App(FrozenModel):
    id: int = Field(gt=0)
    client_id: str | None = None
    slug: str = Field(min_length=1)
    html_url: str = Field(repr=False)
    owner: Owner


class RegisteredApp(App):
    pem: SecretStr


class GrantedPermissions(FrozenModel):
    organization_copilot_requests: Literal["read"]


class Installation(FrozenModel):
    id: int = Field(gt=0)
    app_id: int = Field(gt=0)
    account: Owner
    permissions: GrantedPermissions
    suspended_at: str | None = None


class Manifest(FrozenModel):
    name: str
    url: str = "https://docs.litellm.ai/"
    redirect_url: str
    public: bool = False
    default_permissions: InstallationPermissions = InstallationPermissions()


class AppValues(FrozenModel):
    github_copilot_auth_type: Literal["github_app"] = "github_app"
    github_copilot_host: str
    github_copilot_app_id: str
    github_copilot_app_installation_id: str
    github_copilot_app_private_key: str = Field(repr=False)


class CredentialInfo(FrozenModel):
    custom_llm_provider: Literal["github_copilot"] = "github_copilot"


class AppCredential(FrozenModel):
    credential_name: str
    credential_values: AppValues
    credential_info: CredentialInfo = CredentialInfo()
    credential_values_to_delete: tuple[str, ...] = ()


class ProxyTransport(UpstreamTransport, Protocol):
    def patch(self, path: str, *, headers: BaseModel, json: BaseModel, response_type: type[_R]) -> Result[_R]: ...


def _page(context: BrowserContext, host: str, login: str, uri: str) -> Approval:
    return open_account_page(context, host=host, login=login, label="APP organization authorization", uri=uri)


@step("Listen for the App registration callback on loopback")
@contextmanager
def callback_receiver() -> Generator[str]:
    path: Final = f"/copilot-e2e/{secrets.token_urlsafe(24)}"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"<p>Registration received. Return to the terminal.</p>")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread: Final = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}{path}"
        finally:
            server.shutdown()
            thread.join()


def registration_code(url: str, callback: str, state: str) -> str | None:
    parsed: Final = urlsplit(url)
    target: Final = urlsplit(callback)
    if (parsed.scheme, parsed.netloc, parsed.path) != (target.scheme, target.netloc, target.path):
        return None
    query: Final = parse_qs(parsed.query)
    if len(query.get("state", ())) != 1 or not hmac.compare_digest(query["state"][0], state):
        pytest.fail("[APP] Registration state did not match. No registration code was exchanged.", pytrace=False)
    codes: Final = query.get("code", ())
    if len(codes) != 1 or not codes[0] or parsed.fragment:
        pytest.fail("[APP] Registration returned no unique authorization code.", pytrace=False)
    return codes[0]


def _registration_ready(approval: Approval, registration: str) -> bool:
    parsed: Final = urlsplit(approval.page.url)
    target: Final = urlsplit(registration)
    return (
        parsed.netloc == target.netloc
        and parsed.path == target.path
        and approval.page.locator('input#integration_name, input[name="integration[name]"]').first.is_visible()
    )


def _organization_approval(context: BrowserContext, host: str, organization: str, login: str) -> tuple[str, Approval]:
    registration: Final = f"https://{host}/organizations/{quote(organization, safe='')}/settings/apps/new"
    approval: Final = _page(context, host, login, registration)
    deadline: Final = time.monotonic() + 900
    while time.monotonic() < deadline:
        if _registration_ready(approval, registration):
            return registration, approval
        approval.wait(0.2)
    pytest.fail("[APP] Organization settings were unavailable. Check App-administrator access.", pytrace=False)


@contextmanager
def _app_browser() -> Generator[BrowserContext]:
    with (
        browser_step("APP", "Opening or closing the administrator browser"),
        sync_playwright() as playwright,
        playwright.chromium.launch(headless=False) as browser,
        browser.new_context() as context,
    ):
        yield context


@step("Register the dedicated organization App with browser approval")
def register_app(
    host: str, organization: str, login: str, github: UpstreamTransport, *, context: BrowserContext
) -> RegisteredApp:
    state: Final = secrets.token_urlsafe(32)
    sys.stdout.write("[APP] Sign in with the configured administrator. Review and approve the private App.\n")
    sys.stdout.flush()
    with (
        browser_step("APP", "Registering the organization App"),
        callback_receiver() as callback,
    ):
        manifest: Final = Manifest(
            name=f"litellm-copilot-e2e-{secrets.token_hex(4)}",
            redirect_url=callback,
        )
        registration, approval = _organization_approval(context, host, organization, login)
        deadline: Final = time.monotonic() + 900
        approval.page.set_content(
            f'<form method="post" action="{escape(registration + "?" + urlencode({"state": state}), quote=True)}">'
            f'<input type="hidden" name="manifest" value="{escape(manifest.model_dump_json(), quote=True)}">'
            '<button type="submit">Configure App</button></form>'
        )
        approval.page.get_by_role("button", name="Configure App", exact=True).click()
        while time.monotonic() < deadline:
            if (code := registration_code(approval.page.url, callback, state)) is not None:
                return upstream(
                    github.post(
                        f"/app-manifests/{quote(code, safe='')}/conversions",
                        headers=GithubHeaders(),
                        json=NoBody(),
                        response_type=RegisteredApp,
                    ),
                    "App manifest conversion",
                )
            approval.wait(0.2)
    pytest.fail("[APP] App registration approval timed out. No credentials were printed.", pytrace=False)


def _check_owner(app: App, organization: str) -> None:
    if app.owner.login.casefold() != organization.casefold():
        pytest.fail("[APP] The App belongs to another organization. No proxy credential was changed.", pytrace=False)


@step("Verify the organization App installation")
def read_installation(github: UpstreamTransport, environ: Mapping[str, str], app: App) -> Installation | None:
    organization: Final = required(environ, "APP_ORGANIZATION")
    result: Final = github.get(
        f"/orgs/{quote(organization, safe='')}/installation",
        headers=app_headers(environ),
        params=NoBody(),
        response_type=Installation,
    )
    if isinstance(result, UnknownApiError) and result.status_code == 404:
        return None
    installation: Final = upstream(result, "organization App installation verification")
    if (
        installation.app_id != app.id
        or installation.account.login.casefold() != organization.casefold()
        or installation.suspended_at is not None
    ):
        pytest.fail("[APP] Installation does not match the active organization App.", pytrace=False)
    configured: Final = environ.get("E2E_GITHUB_COPILOT_APP_INSTALLATION_ID", "").strip()
    if configured and configured != str(installation.id):
        pytest.fail("[APP] Saved installation ID does not match the organization installation.", pytrace=False)
    return installation


@step("Install the dedicated organization App with browser approval")
def install_app(
    host: str, login: str, app: App, lookup: Callable[[], Installation | None], *, context: BrowserContext
) -> Installation:
    sys.stdout.write(
        "[APP] Approve installation on the configured organization and review Copilot Requests permission.\n"
    )
    sys.stdout.flush()
    with browser_step("APP", "Approving the organization installation"):
        _, approval = _organization_approval(context, host, app.owner.login, login)
        approval.page.goto(installation_url(app, host), wait_until="domcontentloaded")
        deadline: Final = time.monotonic() + 900
        while time.monotonic() < deadline:
            approval.wait(5)
            if (installation := lookup()) is not None:
                return installation
    pytest.fail(
        "[APP] Installation approval timed out. Check organization policy and administrator approval.", pytrace=False
    )


def installation_url(app: App, host: str) -> str:
    parsed: Final = urlsplit(app.html_url)
    parts: Final = parsed.path.rstrip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.query
        or parsed.fragment
        or len(parts) not in (3, 4)
        or parts[1] != "apps"
        or parts[-1] != quote(app.slug, safe="")
        or any(part in (".", "..") for part in parts)
    ):
        pytest.fail(
            "[APP] GitHub returned an unsupported App URL. No browser credentials were sent to it.", pytrace=False
        )
    return f"https://{host}{parsed.path.rstrip('/')}/installations/new"


def _persist(path: Path, values: Mapping[str, str]) -> Mapping[str, str]:
    for key, value in values.items():
        set_key(path, f"E2E_GITHUB_COPILOT_{key}", value)
    path.chmod(0o600)
    return {f"E2E_GITHUB_COPILOT_{key}": value for key, value in values.items()}


@step("Configure the dedicated proxy App credential")
def sync_proxy(environ: Mapping[str, str], *, proxy: ProxyTransport | None = None) -> None:
    transport: Final = proxy or HttpTransport(
        base_url=environ.get("LITELLM_CONTROL_PLANE_URL") or environ["LITELLM_PROXY_URL"],
        master_key=environ["LITELLM_MASTER_KEY"],
    )
    headers: Final = GithubHeaders(authorization=f"Bearer {environ['LITELLM_MASTER_KEY']}")
    name: Final = required(environ, "APP_CREDENTIAL")
    existing: Final = transport.get(
        f"/credentials/by_name/{quote(name, safe='')}", headers=headers, params=NoBody(), response_type=CredentialView
    )
    absent: Final = isinstance(existing, UnknownApiError) and existing.status_code == 404
    if not absent:
        values: Final = upstream(existing, "existing proxy App credential verification").credential_values
        if values.github_copilot_auth_type != "github_app" or values.github_copilot_host != configured_host(environ):
            pytest.fail("[APP] Refusing to replace another authentication mode or host's credential.", pytrace=False)
        if str(values.github_copilot_app_id) != required(environ, "APP_ID"):
            pytest.fail("[APP] Refusing to overwrite a credential belonging to another App.", pytrace=False)
    body: Final = AppCredential(
        credential_name=name,
        credential_values=AppValues(
            github_copilot_host=configured_host(environ),
            github_copilot_app_id=required(environ, "APP_ID"),
            github_copilot_app_installation_id=required(environ, "APP_INSTALLATION_ID"),
            github_copilot_app_private_key=read_secret(environ, "APP_PRIVATE_KEY_FILE").get_secret_value(),
        ),
        credential_values_to_delete=() if absent else ("github_copilot_app_private_key_ref",),
    )
    upstream(
        transport.post("/credentials", headers=headers, json=body, response_type=NoBody)
        if absent
        else transport.patch(f"/credentials/{quote(name, safe='')}", headers=headers, json=body, response_type=NoBody),
        "proxy App credential persistence",
    )


def _app_settings(
    environ: Mapping[str, str],
    env_file: Path,
    host: str,
    organization: str,
    client: UpstreamTransport,
    register: Callable[[str, str, str, UpstreamTransport], RegisteredApp],
) -> Mapping[str, str]:
    app_id: Final = environ.get("E2E_GITHUB_COPILOT_APP_ID", "").strip()
    if app_id:
        return environ
    if environ.get("E2E_GITHUB_COPILOT_APP_INSTALLATION_ID", "").strip():
        pytest.fail("[APP] An installation ID without its App ID is incomplete. No App was registered.", pytrace=False)
    login: Final = required(environ, "APP_LOGIN")
    data_home: Final = Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    private_key: Final = Path(
        environ.get("E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE", "").strip()
        or data_home / "litellm/copilot-e2e/tokens" / host / "app" / organization / "private-key.pem"
    )
    if not private_key.is_absolute() or private_key.exists():
        pytest.fail(
            "[APP] New App private-key path must be absolute and must not overwrite an existing file.",
            pytrace=False,
        )
    registered: Final = register(host, organization, login, client)
    _check_owner(registered, organization)
    save_token(private_key, registered.pem)
    updates: Final = _persist(
        env_file,
        {
            "APP_ID": str(registered.id),
            "APP_PRIVATE_KEY_FILE": str(private_key),
            "APP_CREDENTIAL": environ.get("E2E_GITHUB_COPILOT_APP_CREDENTIAL") or "e2e-copilot-app",
        },
    )
    return {**environ, **updates}


@step("Authorize the configured organization App")
def run(
    environ: Mapping[str, str],
    *,
    github: UpstreamTransport | None = None,
    register: Callable[[str, str, str, UpstreamTransport], RegisteredApp] | None = None,
    install: Callable[[str, str, App, Callable[[], Installation | None]], Installation] | None = None,
    verify: Callable[[Mapping[str, str]], Account] = lambda settings: load_account("github_app", settings),
    sync: Callable[[Mapping[str, str]], None] = sync_proxy,
) -> None:
    organization: Final = required(environ, "APP_ORGANIZATION")
    if _ORG.fullmatch(organization) is None:
        pytest.fail("[APP] APP_ORGANIZATION must be an organization URL name, not a URL.", pytrace=False)
    env_file: Final = Path(environ.get("E2E_GITHUB_COPILOT_ENV_FILE", ""))
    if not env_file.is_file() or not os.access(env_file, os.W_OK):
        pytest.fail(
            "[APP] Setup needs a writable dotenv file. Use --env-file or the provider-local .env.", pytrace=False
        )
    if not environ.get("LITELLM_PROXY_URL") or not environ.get("LITELLM_MASTER_KEY"):
        pytest.fail("[APP] Configure the dedicated proxy URL and master key before registering an App.", pytrace=False)
    host: Final = configured_host(environ)
    client: Final = github or HttpTransport(base_url=github_api_base(host), master_key="")
    with ExitStack() as stack:

        @cache
        def context() -> BrowserContext:
            return stack.enter_context(_app_browser())

        register_action: Final[Callable[[str, str, str, UpstreamTransport], RegisteredApp]] = register or (
            lambda selected_host, org, login, github: register_app(selected_host, org, login, github, context=context())
        )
        install_action: Final[Callable[[str, str, App, Callable[[], Installation | None]], Installation]] = install or (
            lambda selected_host, login, app, lookup: install_app(selected_host, login, app, lookup, context=context())
        )
        saved: Final = _app_settings(environ, env_file, host, organization, client, register_action)
        app: Final = upstream(
            client.get("/app", headers=app_headers(saved), params=NoBody(), response_type=App),
            "saved App identity verification",
        )
        _check_owner(app, organization)
        if required(saved, "APP_ID") not in (str(app.id), app.client_id):
            pytest.fail("[APP] Private key and saved App ID disagree.", pytrace=False)
        installation: Final = read_installation(client, saved, app) or install_action(
            host, required(saved, "APP_LOGIN"), app, lambda: read_installation(client, saved, app)
        )
        _persist(env_file, {"APP_INSTALLATION_ID": str(installation.id)})
        complete: Final = {**saved, "E2E_GITHUB_COPILOT_APP_INSTALLATION_ID": str(installation.id)}
        verify(complete)
        sync(complete)
    sys.stdout.write(
        "[APP] App installation and direct Copilot catalog verified. Proxy credential saved. No tests were run.\n"
    )
