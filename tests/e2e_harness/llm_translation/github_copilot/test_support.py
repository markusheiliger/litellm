from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from types import MappingProxyType
from typing import Final

import pytest
from e2e_http import AuthHeaders, Result, Success
from llm_translation.github_copilot.support import (
    Account,
    Catalog,
    CatalogModel,
    Deployment,
    Profile,
    UpstreamTransport,
    assert_catalog_matches,
    assert_recorded,
    copilot_base,
    load_account,
    read_secret,
    select_model,
)
from models import SpendLogRow
from pydantic import BaseModel, SecretStr

_ROOT: Final = Path(__file__).resolve().parents[4]
_SUITE: Final = _ROOT / "tests/e2e/llm_translation/github_copilot"


def _catalog() -> Catalog:
    return Catalog.model_validate(
        {
            "data": [
                {
                    "id": "test-chat-9",
                    "supported_endpoints": ["/chat/completions", "/responses"],
                    "capabilities": {"supports": {"streaming": True}},
                },
                {
                    "id": "test-chat-10",
                    "supported_endpoints": ["/v1/chat/completions", "/v1/messages"],
                    "capabilities": {"supports": {"streaming": False}},
                },
                {"id": "test-embedding", "capabilities": {"type": "embeddings"}},
            ]
        }
    )


def _deployment() -> Deployment:
    return Deployment(
        account=Account(
            profile="shared",
            credential="test-credential",
            token=SecretStr("synthetic-token"),
            api_base="https://api.githubcopilot.com",
            catalog=Catalog(data=(CatalogModel(id="model-a"), CatalogModel(id="github_copilot/model-b"))),
        ),
        pattern="test-scope/*",
        key="synthetic-key",
    )


def test_model_selection_respects_surface_streaming_and_numeric_versions() -> None:
    catalog: Final = _catalog()
    assert select_model(catalog, "chat_completions").id == "test-chat-10"
    assert select_model(catalog, "chat_completions", stream=True).id == "test-chat-9"
    assert select_model(catalog, "responses").id == "test-chat-9"
    assert select_model(catalog, "messages").id == "test-chat-10"
    assert select_model(catalog, "embeddings").id == "test-embedding"
    with pytest.raises(pytest.fail.Exception, match="no compatible messages model"):
        select_model(catalog, "messages", stream=True)


def test_model_selection_does_not_invent_missing_endpoint_capabilities() -> None:
    catalog: Final = Catalog.model_validate(
        {"data": [{"id": "test-chat", "capabilities": {"type": "chat", "supports": {"streaming": True}}}]}
    )
    assert select_model(catalog, "chat_completions", stream=True).id == "test-chat"
    with pytest.raises(pytest.fail.Exception, match="no compatible responses model"):
        select_model(catalog, "responses")


def test_catalog_comparison_accepts_only_the_full_normalized_namespace() -> None:
    deployment: Final = _deployment()
    assert_catalog_matches(deployment, ("test-scope/model-b", "test-scope/model-a"))
    for incorrect in (
        (),
        ("test-scope/model-a",),
        ("test-scope/model-a", "test-scope/model-b", "unrelated/model"),
        ("test-scope/model-a", "test-scope/model-b", "test-scope/model-b"),
        ("test-scope/*",),
    ):
        with pytest.raises(AssertionError):
            assert_catalog_matches(deployment, incorrect)


@pytest.mark.parametrize(
    ("catalog_id", "recorded_id"),
    (
        ("model-a", "model-a"),
        ("model-a", "github_copilot/model-a"),
        ("github_copilot/model-a", "model-a"),
        ("github_copilot/model-a", "github_copilot/model-a"),
    ),
)
def test_recorded_backend_matches_with_or_without_the_provider_prefix(catalog_id: str, recorded_id: str) -> None:
    rows: Final = (SpendLogRow(status="success", custom_llm_provider="github_copilot", model=recorded_id),)
    assert_recorded(rows, _deployment(), CatalogModel(id=catalog_id))


@pytest.mark.parametrize("recorded_id", (None, "a", "other-model-a", "other-provider/model-a"))
def test_recorded_backend_rejects_missing_or_different_models(recorded_id: str | None) -> None:
    rows: Final = (SpendLogRow(status="success", custom_llm_provider="github_copilot", model=recorded_id),)
    with pytest.raises(AssertionError, match="recorded backend"):
        assert_recorded(rows, _deployment(), CatalogModel(id="model-a"))


def test_recorded_request_accepts_a_stringified_unset_cache_flag() -> None:
    rows: Final = (
        SpendLogRow(status="success", custom_llm_provider="github_copilot", model="model-a", cache_hit="None"),
    )
    assert_recorded(rows, _deployment(), CatalogModel(id="model-a"))


def test_recorded_request_rejects_a_cache_hit() -> None:
    rows: Final = (
        SpendLogRow(status="success", custom_llm_provider="github_copilot", model="model-a", cache_hit="True"),
    )
    with pytest.raises(AssertionError, match="proxy cache"):
        assert_recorded(rows, _deployment(), CatalogModel(id="model-a"))


@pytest.mark.parametrize(
    "value",
    (
        "http://api.githubcopilot.com",
        "https://api.githubcopilot.com.attacker.example",
        "https://user:password@api.githubcopilot.com",
        "https://api.githubcopilot.com:8443",
        "https://api.githubcopilot.com/another-api",
        "https://api.githubcopilot.com?token=untrusted",
        "https://api.githubcopilot.com#fragment",
    ),
)
def test_account_endpoint_is_validated_before_forwarding_a_token(value: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="No token was sent"):
        copilot_base(value)


def test_account_endpoint_keeps_the_entitlement_specific_host() -> None:
    assert copilot_base("https://api.business.githubcopilot.com/") == "https://api.business.githubcopilot.com"


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://api.githubcopilot.com",
        "https://copilot-api.another-test.ghe.com",
        "https://copilot-api.test-enterprise.ghe.com.attacker.example",
        "http://copilot-api.test-enterprise.ghe.com",
        "https://user@copilot-api.test-enterprise.ghe.com",
        "https://copilot-api.test-enterprise.ghe.com:8443",
        "https://copilot-api.test-enterprise.ghe.com/path",
    ),
)
def test_enterprise_catalog_cannot_redirect_a_token_outside_its_tenant(endpoint: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="No token was sent"):
        copilot_base(endpoint, "test-enterprise.ghe.com")


@pytest.mark.parametrize("host", ("https://test.ghe.com", "a.b.ghe.com", "*.ghe.com", "other.example", ""))
def test_invalid_test_host_fails_before_credentials_are_loaded(host: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="E2E_GITHUB_COPILOT_HOST must"):
        load_account("shared", {"E2E_GITHUB_COPILOT_HOST": host})


@dataclass(frozen=True, slots=True)
class OracleTransport:
    base_url: str
    payloads: Mapping[str, str]
    requests: SimpleQueue[tuple[str, str, str | None]]

    def _response[R: BaseModel](self, method: str, path: str, headers: BaseModel, response_type: type[R]) -> Result[R]:
        assert isinstance(headers, AuthHeaders)
        url: Final = f"{self.base_url}{path}"
        self.requests.put((method, url, headers.authorization))
        return Success(status_code=200, data=response_type.model_validate_json(self.payloads[url]))

    def get[R: BaseModel](
        self, path: str, *, headers: BaseModel, params: BaseModel, response_type: type[R]
    ) -> Result[R]:
        return self._response("GET", path, headers, response_type)

    def post[R: BaseModel](
        self, path: str, *, headers: BaseModel, json: BaseModel, response_type: type[R]
    ) -> Result[R]:
        return self._response("POST", path, headers, response_type)


@pytest.mark.parametrize("host", ("github.com", "test-enterprise.ghe.com"))
@pytest.mark.parametrize(
    ("profile", "prefix"),
    (("shared", "SHARED"), ("per_user_oauth_a", "USER_A"), ("per_user_oauth_b", "USER_B"), ("github_app", "APP")),
)
def test_oracle_uses_only_the_selected_host_and_explicit_test_identity(
    host: str, profile: Profile, prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("GH_HOST", "development.ghe.com")
    monkeypatch.setenv("GITHUB_COPILOT_ACCESS_TOKEN_URL", "https://development.ghe.com/login/oauth/access_token")
    monkeypatch.setenv("GITHUB_COPILOT_API_KEY_URL", "https://api.development.ghe.com/copilot_internal/v2/token")
    monkeypatch.setenv("GITHUB_TOKEN", "development-token")
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "development"))
    token_file: Final = tmp_path / "runner-token"
    token_file.write_text("test-account-token")
    key_file: Final = tmp_path / "app.pem"
    key_file.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    github: Final = "https://api.github.com" if host == "github.com" else f"https://api.{host}"
    copilot: Final = "https://api.business.githubcopilot.com" if host == "github.com" else f"https://copilot-api.{host}"
    catalog_base: Final = (
        "https://api.githubcopilot.com" if profile == "github_app" and host == "github.com" else copilot
    )
    payloads: Final = MappingProxyType(
        {
            f"{github}/copilot_internal/user": json.dumps({"endpoints": {"api": copilot}}),
            f"{github}/user": json.dumps({"login": "dedicated-test-account"}),
            f"{github}/app/installations/42/access_tokens": json.dumps({"token": "test-installation-token"}),
            f"{catalog_base}/models": json.dumps({"data": [{"id": "test-model"}]}),
        }
    )
    requests: Final = SimpleQueue[tuple[str, str, str | None]]()

    def factory(base: str) -> UpstreamTransport:
        return OracleTransport(base, payloads, requests)

    account: Final = load_account(
        profile,
        {
            "E2E_GITHUB_COPILOT_HOST": host,
            f"E2E_GITHUB_COPILOT_{prefix}_CREDENTIAL": "test-credential",
            f"E2E_GITHUB_COPILOT_{prefix}_TOKEN_FILE": str(token_file),
            f"E2E_GITHUB_COPILOT_{prefix}_ID": "test-user",
            "E2E_GITHUB_COPILOT_APP_ID": "test-app",
            "E2E_GITHUB_COPILOT_APP_INSTALLATION_ID": "42",
            "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file),
        },
        transport_factory=factory,
    )
    observed: Final = tuple(requests.get_nowait() for _ in range(requests.qsize()))
    assert account.host == host and account.api_base == catalog_base
    assert account.catalog.data[0].id == "test-model"
    if profile == "github_app":
        assert tuple((method, url) for method, url, _ in observed) == (
            ("POST", f"{github}/app/installations/42/access_tokens"),
            ("GET", f"{catalog_base}/models"),
        )
        assert observed[-1][2] == "Bearer test-installation-token"
    else:
        expected_urls: Final = (f"{github}/copilot_internal/user", f"{catalog_base}/models") + (
            (f"{github}/user",) if profile.startswith("per_user") else ()
        )
        assert tuple(url for _, url, _ in observed) == expected_urls
        assert all(authorization == "Bearer test-account-token" for _, _, authorization in observed)


def test_secret_setup_fails_explicitly_without_exposing_file_contents(tmp_path: Path) -> None:
    path: Final = tmp_path / "access-token"
    path.write_text("\ufeff  synthetic-access-token\n", encoding="utf-8")
    assert read_secret({"E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE": str(path)}, "SHARED_TOKEN_FILE").get_secret_value() == (
        "synthetic-access-token"
    )
    with pytest.raises(pytest.fail.Exception, match="could not read E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE"):
        read_secret({"E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE": str(tmp_path / "missing")}, "SHARED_TOKEN_FILE")
    with pytest.raises(pytest.fail.Exception, match="requires E2E_GITHUB_COPILOT_SHARED_CREDENTIAL"):
        load_account("shared", {})


@pytest.mark.parametrize(
    ("copilot", "discovery"),
    (("", ""), ("", "1"), ("1", ""), ("1", "1")),
)
def test_live_cases_are_selected_only_by_their_explicit_opt_ins(copilot: str, discovery: str) -> None:
    result: Final = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            str(_SUITE),
            "--collect-only",
            "--reruns",
            "0",
            "-q",
        ],
        cwd=_ROOT,
        env={
            **{name: value for name, value in os.environ.items() if not name.startswith("E2E_GITHUB_COPILOT_")},
            "LITELLM_MASTER_KEY": "sk-harness",
            "E2E_GITHUB_COPILOT_LIVE": copilot,
            "E2E_PROXY_WIDE_CONFIG_STACK": discovery,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == (0 if copilot else 5), (result.stdout, result.stderr)
    assert ("::test_chat_completions[shared]" in result.stdout) == bool(copilot), result.stdout
    assert (
        "::test_wildcard_catalog_matches_upstream_and_a_listed_model_is_callable[github_app]" in result.stdout
    ) == bool(copilot and discovery), result.stdout


@pytest.mark.parametrize("enabled", ("", "1"))
def test_deselected_copilot_cases_are_coverage_gaps(enabled: str) -> None:
    result: Final = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import sys; from pathlib import Path; "
            "from coverage_registry.collector import collect_markers; "
            "result = collect_markers(Path(sys.argv[1])); "
            "print('\\n'.join(sorted(result.covered))); "
            "raise SystemExit(bool(result.collection_errors))",
            str(_SUITE),
        ],
        cwd=_ROOT,
        env={
            **{name: value for name, value in os.environ.items() if not name.startswith("E2E_GITHUB_COPILOT_")},
            "PYTHONPATH": str(_ROOT / "tests/e2e"),
            "LITELLM_MASTER_KEY": "sk-harness",
            "E2E_GITHUB_COPILOT_LIVE": enabled,
            "E2E_PROXY_WIDE_CONFIG_STACK": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ("other.model_discovery.github_copilot.github_app.catalog_matches" in result.stdout) == bool(enabled)
    assert ("llm.chat_completions.github_copilot.basic.nonstream.shared_works" in result.stdout) == bool(enabled)
