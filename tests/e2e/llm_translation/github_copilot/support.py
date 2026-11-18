from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias
from urllib.parse import quote, urlsplit

import pytest
from _pytest.mark.structures import ParameterSet
from e2e_http import AuthHeaders, NoBody, Result, Success, unwrap, without_retries
from e2e_metadata import Domain, Mode, Provider, Route, Subject, meta
from lifecycle import ResourceManager
from models import LiteLLMParamsBody, ModelInfoBody, ModelNewBody, ModelsListParams, ModelsListResponse
from proxy_client import ProxyClient
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from transport import HttpTransport

Profile: TypeAlias = Literal["shared", "per_user_oauth_a", "per_user_oauth_b", "github_app"]
Endpoint: TypeAlias = Literal["chat_completions", "responses", "messages", "embeddings"]
AuthMode: TypeAlias = Literal["shared", "per_user_oauth", "github_app"]
PROFILES: Final[tuple[Profile, ...]] = ("shared", "per_user_oauth_a", "per_user_oauth_b", "github_app")
ENDPOINTS: Final[tuple[Endpoint, ...]] = ("chat_completions", "responses", "messages", "embeddings")
_ENTERPRISE_HOST: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com")


class UpstreamTransport(Protocol):
    def get[R: BaseModel](
        self, path: str, *, headers: BaseModel, params: BaseModel, response_type: type[R]
    ) -> Result[R]: ...

    def post[R: BaseModel](
        self, path: str, *, headers: BaseModel, json: BaseModel, response_type: type[R]
    ) -> Result[R]: ...


def _transport(base_url: str) -> UpstreamTransport:
    return HttpTransport(base_url=base_url, master_key="")


def configured_host(environ: Mapping[str, str]) -> str:
    host: Final = environ.get("E2E_GITHUB_COPILOT_HOST", "github.com").lower()
    if host != "github.com" and _ENTERPRISE_HOST.fullmatch(host) is None:
        pytest.fail(
            "E2E_GITHUB_COPILOT_HOST must be github.com or a single enterprise hostname ending in .ghe.com.",
            pytrace=False,
        )
    return host


def github_api_base(host: str) -> str:
    return "https://api.github.com" if host == "github.com" else f"https://api.{host}"


def default_copilot_base(host: str) -> str:
    return "https://api.githubcopilot.com" if host == "github.com" else f"https://copilot-api.{host}"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)


class CopilotHeaders(AuthHeaders):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    integration_id: str = Field(serialization_alias="copilot-integration-id")
    editor_version: str = Field(default="vscode/1.95.0", alias="editor-version")


class GithubHeaders(AuthHeaders):
    model_config = ConfigDict(frozen=True)

    accept: str = "application/vnd.github+json"


class Supports(FrozenModel):
    streaming: bool = False


class Capabilities(FrozenModel):
    type: str = ""
    supports: Supports = Supports()


class CatalogModel(FrozenModel):
    id: str = Field(min_length=1)
    supported_endpoints: tuple[str, ...] = ()
    capabilities: Capabilities = Capabilities()

    def supports(self, endpoint: Endpoint, *, stream: bool) -> bool:
        paths: Final = frozenset(path.removeprefix("/v1") for path in self.supported_endpoints)
        path: Final = "/chat/completions" if endpoint == "chat_completions" else f"/{endpoint}"
        compatible: Final = path in paths or (
            not paths
            and self.capabilities.type == ("embeddings" if endpoint == "embeddings" else "chat")
            and endpoint in ("chat_completions", "embeddings")
        )
        return compatible and (not stream or self.capabilities.supports.streaming)


class Catalog(FrozenModel):
    data: tuple[CatalogModel, ...]


class UserEndpoints(FrozenModel):
    api: str


class CopilotUser(FrozenModel):
    endpoints: UserEndpoints


class GithubUser(FrozenModel):
    login: str = Field(repr=False)


class InstallationToken(FrozenModel):
    token: SecretStr


class InstallationPermissions(FrozenModel):
    organization_copilot_requests: Literal["read"] = "read"


class InstallationRequest(FrozenModel):
    permissions: InstallationPermissions = InstallationPermissions()


class Connection(FrozenModel):
    credential_name: str
    connected: bool
    github_login: str | None = Field(default=None, repr=False)


class Connections(FrozenModel):
    connections: tuple[Connection, ...]


class CredentialValues(FrozenModel):
    github_copilot_auth_type: str | None = None
    github_copilot_app_id: str | int | None = None
    github_copilot_app_installation_id: str | int | None = None
    github_copilot_host: str = "github.com"


class CredentialView(FrozenModel):
    credential_values: CredentialValues


def required(environ: Mapping[str, str], name: str) -> str:
    value: Final = environ.get(f"E2E_GITHUB_COPILOT_{name}", "").strip()
    if not value:
        pytest.fail(
            f"Copilot live setup requires E2E_GITHUB_COPILOT_{name}. "
            "See tests/e2e/llm_translation/github_copilot/README.md.",
            pytrace=False,
        )
    return value


def read_secret(environ: Mapping[str, str], name: str) -> SecretStr:
    path: Final = Path(required(environ, name))
    try:
        value: Final = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        pytest.fail(f"Copilot live setup could not read E2E_GITHUB_COPILOT_{name}.", pytrace=False)
    if not value:
        pytest.fail(f"E2E_GITHUB_COPILOT_{name} must point to a nonempty secret file.", pytrace=False)
    return SecretStr(value)


def upstream[R: BaseModel](result: Result[R], operation: str) -> R:
    match result:
        case Success(data=data):
            return data
        case _:
            pytest.fail(f"Copilot prerequisite failed during {operation} ({result.kind}).", pytrace=False)


def copilot_base(value: str, host: str = "github.com") -> str:
    parsed: Final = urlsplit(value)
    permitted: Final = (
        parsed.hostname == "api.githubcopilot.com"
        or (parsed.hostname is not None and parsed.hostname.endswith(".githubcopilot.com"))
        if host == "github.com"
        else parsed.hostname == f"copilot-api.{host}"
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not permitted
        or parsed.netloc != parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail("Copilot returned an unsupported account API endpoint. No token was sent to it.", pytrace=False)
    return value.rstrip("/")


def auth_mode(profile: Profile) -> AuthMode:
    return "per_user_oauth" if profile in ("per_user_oauth_a", "per_user_oauth_b") else profile


def _prefix(profile: Profile) -> str:
    match profile:
        case "shared":
            return "SHARED"
        case "per_user_oauth_a":
            return "USER_A"
        case "per_user_oauth_b":
            return "USER_B"
        case "github_app":
            return "APP"


def _app_token(github: UpstreamTransport, environ: Mapping[str, str]) -> SecretStr:
    import jwt

    app_id: Final = required(environ, "APP_ID")
    installation_id: Final = required(environ, "APP_INSTALLATION_ID")
    if not installation_id.isascii() or not installation_id.isdecimal() or int(installation_id) <= 0:
        pytest.fail("E2E_GITHUB_COPILOT_APP_INSTALLATION_ID must be a positive installation ID.", pytrace=False)
    private_key: Final = read_secret(environ, "APP_PRIVATE_KEY_FILE")
    now: Final = int(time.time())
    assertion: Final = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key.get_secret_value(),
        algorithm="RS256",
    )
    return upstream(
        github.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=GithubHeaders(authorization=f"Bearer {assertion}"),
            json=InstallationRequest(),
            response_type=InstallationToken,
        ),
        "App installation token creation",
    ).token


@dataclass(frozen=True, slots=True)
class Account:
    profile: Profile
    credential: str
    token: SecretStr = field(repr=False)
    api_base: str
    catalog: Catalog
    host: str = "github.com"
    user_id: str | None = None
    github_login: str | None = field(default=None, repr=False)
    app_id: str | None = None
    installation_id: str | None = None


def load_account(
    profile: Profile,
    environ: Mapping[str, str] = os.environ,
    *,
    transport_factory: Callable[[str], UpstreamTransport] = _transport,
) -> Account:
    host: Final = configured_host(environ)
    prefix: Final = _prefix(profile)
    credential: Final = required(environ, f"{prefix}_CREDENTIAL")
    github: Final = transport_factory(github_api_base(host))
    with without_retries():
        token: Final = (
            _app_token(github, environ) if profile == "github_app" else read_secret(environ, f"{prefix}_TOKEN_FILE")
        )
        headers: Final = GithubHeaders(authorization=f"Bearer {token.get_secret_value()}")
        base: Final = (
            default_copilot_base(host)
            if profile == "github_app"
            else copilot_base(
                upstream(
                    github.get("/copilot_internal/user", headers=headers, params=NoBody(), response_type=CopilotUser),
                    f"{profile} account endpoint lookup",
                ).endpoints.api,
                host,
            )
        )
        catalog: Final = upstream(
            transport_factory(base).get(
                "/models",
                headers=CopilotHeaders(
                    authorization=f"Bearer {token.get_secret_value()}",
                    integration_id="copilot-developer-cli" if profile == "github_app" else "vscode-chat",
                ),
                params=NoBody(),
                response_type=Catalog,
            ),
            f"{profile} upstream catalog",
        )
        login: Final = (
            upstream(
                github.get("/user", headers=headers, params=NoBody(), response_type=GithubUser),
                f"{profile} GitHub identity",
            ).login
            if auth_mode(profile) == "per_user_oauth"
            else None
        )
    assert catalog.data, f"{profile} upstream catalog is empty. Check the dedicated account's Copilot entitlement."
    return Account(
        profile=profile,
        credential=credential,
        token=token,
        api_base=base,
        catalog=catalog,
        host=host,
        user_id=required(environ, f"{prefix}_ID") if auth_mode(profile) == "per_user_oauth" else None,
        github_login=login,
        app_id=required(environ, "APP_ID") if profile == "github_app" else None,
        installation_id=required(environ, "APP_INSTALLATION_ID") if profile == "github_app" else None,
    )


def check_credential(proxy: ProxyClient, account: Account) -> None:
    credential: Final = unwrap(
        proxy.transport.get(
            f"/credentials/by_name/{quote(account.credential, safe='')}",
            headers=proxy.transport.master,
            params=NoBody(),
            response_type=CredentialView,
        )
    ).credential_values
    expected: Final = auth_mode(account.profile)
    assert credential.github_copilot_auth_type in ((None, "shared") if expected == "shared" else (expected,)), (
        f"The named credential is not configured for {account.profile}."
    )
    assert credential.github_copilot_host.lower() == account.host, (
        "The named credential host does not match E2E_GITHUB_COPILOT_HOST."
    )
    if expected == "github_app":
        assert str(credential.github_copilot_app_id) == account.app_id, "The proxy and oracle use different Apps."
        assert str(credential.github_copilot_app_installation_id) == account.installation_id, (
            "The proxy and oracle use different App installations."
        )


def check_connection(proxy: ProxyClient, account: Account, key: str) -> None:
    if account.user_id is None:
        return
    connections: Final = unwrap(
        proxy.transport.get(
            "/credentials/user_connections",
            headers=proxy.transport.bearer(key),
            params=NoBody(),
            response_type=Connections,
        )
    )
    assert any(
        item.credential_name == account.credential and item.connected and item.github_login == account.github_login
        for item in connections.connections
    ), f"{account.profile} must have an approved connection matching its runner-side token."


def _model_order(model: CatalogModel) -> tuple[str, ...]:
    return tuple(part.zfill(20) if part.isdecimal() else part for part in re.split(r"(\d+)", model.id))


def select_model(catalog: Catalog, endpoint: Endpoint, *, stream: bool = False) -> CatalogModel:
    compatible: Final = tuple(model for model in catalog.data if model.supports(endpoint, stream=stream))
    if not compatible:
        pytest.fail(
            f"Copilot catalog advertises no compatible {endpoint} model (stream={stream}). "
            "Select supported endpoints with pytest -k, or provision an account that supports this case.",
            pytrace=False,
        )
    return max(compatible, key=_model_order)


@dataclass(frozen=True, slots=True)
class Deployment:
    account: Account
    pattern: str
    key: str = field(repr=False)

    def name(self, model: CatalogModel) -> str:
        return f"{self.pattern.removesuffix('*')}{model.id.removeprefix('github_copilot/')}"


def register_wildcard(proxy: ProxyClient, resources: ResourceManager, account: Account, pattern: str) -> Deployment:
    check_credential(proxy, account)
    key: Final = resources.key(models=[pattern], user_id=account.user_id)
    check_connection(proxy, account, key)
    model_id: Final = proxy.register_model(
        ModelNewBody(
            model_name=pattern,
            litellm_params=LiteLLMParamsBody(model="github_copilot/*", litellm_credential_name=account.credential),
            model_info=ModelInfoBody(),
        ),
        listed_for=key,
        provider_live=True,
    )
    resources.defer(lambda: proxy.delete_model(model_id))
    return Deployment(account=account, pattern=pattern, key=key)


def listed(proxy: ProxyClient, deployment: Deployment) -> tuple[str, ...]:
    response: Final = unwrap(
        proxy.transport.get(
            "/v1/models",
            headers=proxy.transport.bearer(deployment.key),
            params=ModelsListParams(return_wildcard_routes=False),
            response_type=ModelsListResponse,
        )
    )
    return tuple(entry.id for entry in response.data)


def assert_catalog_matches(deployment: Deployment, actual: tuple[str, ...]) -> None:
    expected: Final = frozenset(deployment.name(model) for model in deployment.account.catalog.data)
    assert actual and len(actual) == len(set(actual)), "Wildcard discovery returned empty or duplicated IDs."
    assert frozenset(actual) == expected, (
        f"{deployment.account.profile} proxy catalog differs from its upstream catalog. "
        f"Missing {sorted(expected - frozenset(actual))}, unexpected {sorted(frozenset(actual) - expected)}"
    )


def assert_recorded(proxy: ProxyClient, deployment: Deployment, model: CatalogModel) -> None:
    rows: Final = proxy.poll_logs_for_key(deployment.key)
    assert len(rows) == 1, f"Expected one real request for this fresh key, got {len(rows)} spend rows."
    row: Final = rows[0]
    assert row.status == "success" and row.custom_llm_provider == "github_copilot", (
        "The Copilot request was not recorded as a successful provider call."
    )
    assert row.model and model.id.endswith(row.model), "The recorded backend differs from the selected catalog model."
    assert row.cache_hit in (None, "False", "false"), "The inference request was served from the proxy cache."
    if deployment.account.user_id is not None:
        assert row.user == deployment.account.user_id, "The request was attributed to another LiteLLM user."


def cases(endpoint: Endpoint | Literal["discovery"], *, stream: bool = False) -> tuple[ParameterSet, ...]:
    route: Final = Route.MODEL_MANAGEMENT if endpoint == "discovery" else Route(endpoint)
    return tuple(
        pytest.param(
            profile,
            id=profile,
            marks=(
                meta(
                    Subject(
                        domain=Domain.LLM_TRANSLATION,
                        route=route,
                        providers=(Provider.GITHUB_COPILOT,),
                        mode=None if endpoint == "discovery" else Mode.STREAM if stream else Mode.NONSTREAM,
                    )
                ),
                pytest.mark.covers(
                    f"other.model_discovery.github_copilot.{auth_mode(profile)}.catalog_matches"
                    if endpoint == "discovery"
                    else f"llm.{endpoint}.github_copilot.basic."
                    f"{'stream' if stream else 'nonstream'}.{auth_mode(profile)}_works"
                ),
            ),
        )
        for profile in PROFILES
    )
