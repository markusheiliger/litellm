from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from queue import SimpleQueue
from typing import Final

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import litellm
from litellm.exceptions import (
    AuthenticationError,
    CallerCredentialAuthenticationError,
    ModelNotMappedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.llms.github_copilot import app_auth
from litellm.llms.github_copilot.app_auth import AppTokenManager
from litellm.llms.github_copilot.chat.transformation import GithubCopilotConfig
from litellm.llms.github_copilot.github_host import github_host_endpoints
from litellm.llms.github_copilot.model_info import GithubCopilotModelInfo
from litellm.llms.github_copilot.per_user_auth import attach_github_copilot_user_session
from litellm.types.utils import CredentialItem
from litellm.utils import AvailableModelsCache, get_model_info_helper

_NOW: Final = 1_800_000_000.0
_USER_API: Final = "https://api.user.githubcopilot.com"
_SHARED_API: Final = "https://api.shared.githubcopilot.com"


@pytest.fixture
def shared_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path))
    path: Final = tmp_path / "access-token"
    path.write_text(f"shared-{tmp_path.name}")
    return path


@pytest.fixture
def user_credential(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        litellm,
        "credential_list",
        [
            CredentialItem(
                credential_name="personal",
                credential_values={"github_copilot_auth_type": "per_user_oauth"},
                credential_info={"custom_llm_provider": "github_copilot"},
            )
        ],
    )
    return "personal"


def _user_params(name: str, user: str, token: str, host: str = "github.com") -> Mapping[str, object]:
    return {
        "litellm_credential_name": name,
        "secret_fields": {
            "user_provider_credentials_user_id": user,
            "user_provider_credentials": {name: {"access_token": token, "github_host": host}},
        },
    }


def _catalog_for_bearer(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/copilot_internal/user":
        return httpx.Response(200, json={"endpoints": {"api": _USER_API}, "chat_enabled": True})
    assert request.url.path == "/models", request.url
    token: Final = request.headers["authorization"].removeprefix("Bearer ")
    return httpx.Response(200, json={"data": [{"id": f"{token}-only"}, {"id": f"{token}-only"}]})


@pytest.mark.parametrize("mode", ["shared", "per_user_oauth", "github_app"])
@pytest.mark.parametrize("host", ["github.com", "octo.ghe.com"])
@respx.mock
def test_catalog_and_inference_use_the_same_account_endpoint_and_integration(
    mode: str, host: str, shared_token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()
    endpoints: Final = github_host_endpoints(host)
    oauth_base: Final = (
        endpoints.default_copilot_api_base if host != "github.com" else _SHARED_API if mode == "shared" else _USER_API
    )
    private_key: Final = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    monkeypatch.setattr(
        litellm,
        "credential_list",
        [
            CredentialItem(
                credential_name="selected",
                credential_values={
                    "github_copilot_host": host,
                    **(
                        {
                            "github_copilot_auth_type": "github_app",
                            "github_copilot_app_id": "test-client",
                            "github_copilot_app_installation_id": "123",
                            "github_copilot_app_private_key": private_key,
                        }
                        if mode == "github_app"
                        else {"github_copilot_auth_type": mode}
                    ),
                },
                credential_info={"custom_llm_provider": "github_copilot"},
            )
        ],
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        if request.url.path == "/app/installations/123/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "app-installation",
                    "expires_at": datetime.fromtimestamp(_NOW + 3600, timezone.utc).isoformat(),
                    "permissions": {"organization_copilot_requests": "read"},
                },
            )
        if request.url.path == "/copilot_internal/user":
            return httpx.Response(
                200,
                json={"endpoints": {"api": oauth_base}, "chat_enabled": True},
            )
        return _catalog_for_bearer(request)

    transport: Final = httpx.MockTransport(respond)
    if mode == "shared":
        respx.get(endpoints.copilot_user_url).mock(side_effect=respond)
    with httpx.Client(transport=transport) as http_client:
        client: Final = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "module_level_client", client)
        monkeypatch.setattr(
            app_auth,
            "APP_TOKEN_MANAGER",
            AppTokenManager(
                client_factory=lambda: httpx.Client(transport=transport), clock=lambda: _NOW, monotonic=lambda: 0
            ),
        )
        params: Final = (
            {"model": "github_copilot/synthetic", "litellm_credential_name": "selected"}
            if mode == "shared"
            else dict(_user_params("selected", "caller", f"user-{shared_token.parent.name}", host))
        )
        info: Final = GithubCopilotModelInfo(client=client, cache=AvailableModelsCache())
        models: Final = info.discover_models(params)
        inference_params: Final = dict(params)
        attach_github_copilot_user_session(inference_params)
        config: Final = GithubCopilotConfig()
        inference_base: Final = inference_params.get("api_base")
        assert inference_base is None or isinstance(inference_base, str)
        provider_info: Final = (
            config.get_openai_compatible_provider_info("synthetic", inference_base, None, "github_copilot")
            if mode == "shared"
            else (None, None, "github_copilot")
        )
        inference_headers: Final = config.validate_environment(
            {}, "synthetic", [], {}, inference_params, api_key=provider_info[1]
        )
        selected_requests: Final = tuple(requests.get_nowait() for _ in range(requests.qsize()))
        catalog_request: Final = next(request for request in selected_requests if request.url.path == "/models")
        inference_token: Final = inference_headers["Authorization"].removeprefix("Bearer ")
        expected_base: Final = endpoints.default_copilot_api_base if mode == "github_app" else oauth_base

    assert models == [f"github_copilot/{inference_token}-only"]
    assert str(catalog_request.url) == f"{expected_base}/models"
    if mode == "shared":
        assert provider_info[0] == expected_base
    assert catalog_request.headers["authorization"] == inference_headers["Authorization"]
    assert catalog_request.headers["copilot-integration-id"] == inference_headers["copilot-integration-id"]
    assert catalog_request.headers["copilot-integration-id"] == (
        "copilot-developer-cli" if mode == "github_app" else "vscode-chat"
    )


def test_interleaved_users_rotation_and_disconnect_do_not_reuse_another_catalog(
    shared_token: Path, user_credential: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        return _catalog_for_bearer(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client: Final = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "module_level_client", client)
        info: Final = GithubCopilotModelInfo(client=client, cache=AvailableModelsCache())
        first: Final = _user_params(user_credential, "first", f"first-{shared_token.parent.name}")
        second: Final = _user_params(user_credential, "second", f"second-{shared_token.parent.name}")
        rotated: Final = _user_params(user_credential, "first", f"rotated-{shared_token.parent.name}")
        catalogs: Final = tuple(info.discover_models(params) for params in (first, second, first, rotated, second))
        with pytest.raises(CallerCredentialAuthenticationError, match="Connect GitHub Copilot"):
            info.discover_models({"litellm_credential_name": user_credential})

    assert catalogs == tuple(
        [f"github_copilot/{token}-{shared_token.parent.name}-only"]
        for token in ("first", "second", "first", "rotated", "second")
    )
    seen: Final = tuple(requests.get_nowait() for _ in range(requests.qsize()))
    assert sum(request.url.path == "/models" for request in seen) == 3
    assert all(shared_token.read_text() not in request.headers.get("authorization", "") for request in seen)


def test_missing_shared_token_fails_without_initiating_login(
    shared_token: Path,
) -> None:
    shared_token.unlink()

    def reject_network(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Discovery without a token must not make a request to {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(reject_network)) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        with pytest.raises(AuthenticationError, match="Complete device-code login separately"):
            info.get_models(api_key="must-not-replace-the-configured-account")


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthenticationError), (403, AuthenticationError), (429, RateLimitError), (503, ServiceUnavailableError)],
)
def test_rejected_catalogs_are_not_cached_or_exposed(shared_token: Path, status: int, error: type[Exception]) -> None:
    statuses: Final = iter((status, 200))

    def respond(request: httpx.Request) -> httpx.Response:
        code: Final = next(statuses)
        return httpx.Response(code, json={"data": [{"id": "recovered"}], "secret": "sensitive-upstream-content"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        with pytest.raises(error) as failure:
            info.get_models(api_base=_SHARED_API)
        assert info.get_models(api_base=_SHARED_API) == ["github_copilot/recovered"]

    assert "sensitive-upstream-content" not in str(failure.value)
    assert shared_token.read_text() not in str(failure.value)


def test_bridged_session_preserves_the_discovery_cache_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "credential_list",
        [
            CredentialItem(
                credential_name=name,
                credential_info={},
                credential_values={"github_copilot_auth_type": "per_user_oauth"},
            )
            for name in ("personal", "other")
        ],
    )
    catalogs: Final = SimpleQueue[httpx.Request]()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/copilot_internal/user":
            return httpx.Response(200, json={"endpoints": {"api": _USER_API}})
        assert request.url.path == "/models"
        catalogs.put(request)
        return httpx.Response(200, json={"data": [{"id": "scope-model"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client: Final = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "module_level_client", client)
        info: Final = GithubCopilotModelInfo(client=client, cache=AvailableModelsCache())
        for name, user in (("personal", "first"), ("personal", "second"), ("other", "first")):
            original: Final = dict(_user_params(name, user, "same-account-token"))
            attach_github_copilot_user_session(original)
            expected: Final = info.discover_models(original)
            bridged: Final = {
                "github_copilot_auth_type": "per_user_oauth",
                "github_copilot_user_session": original["github_copilot_user_session"],
            }
            assert info.discover_models(bridged) == expected == ["github_copilot/scope-model"]
    assert catalogs.qsize() == 3


def test_concurrent_listings_single_flight_and_shared_token_rotation(shared_token: Path) -> None:
    requests: Final = SimpleQueue[httpx.Request]()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        return _catalog_for_bearer(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        with ThreadPoolExecutor(max_workers=8) as pool:
            catalogs: Final = tuple(pool.map(lambda _: info.get_models(api_base=_SHARED_API), range(16)))
        first_token: Final = shared_token.read_text()
        shared_token.write_text(f"rotated-{first_token}")
        rotated: Final = info.get_models(api_base=_SHARED_API)

    assert catalogs == tuple([f"github_copilot/{first_token}-only"] for _ in range(16))
    assert rotated == [f"github_copilot/rotated-{first_token}-only"]
    assert requests.qsize() == 2


@pytest.mark.parametrize("payload", [b"not-json", b'{"data":[{"id":7}]}', b'{"data":[{}]}'])
def test_invalid_catalogs_fail_explicitly(shared_token: Path, payload: bytes) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        with pytest.raises(ServiceUnavailableError, match="invalid catalog"):
            info.get_models(api_base=_SHARED_API)


def test_catalog_timeout_is_not_a_successful_empty_listing(shared_token: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive-upstream-content", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        with pytest.raises(Timeout, match="model discovery timed out"):
            info.get_models(api_base=_SHARED_API)


@pytest.mark.parametrize(
    ("endpoints", "expected_mode"),
    [
        (("/chat/completions", "/v1/responses"), "responses"),
        (("/v1/chat/completions",), "chat"),
        (("/embeddings",), "embedding"),
    ],
)
def test_discovered_model_metadata_reaches_the_core_without_global_registration(
    shared_token: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoints: tuple[str, ...],
    expected_mode: str,
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()
    model: Final = f"catalog-metadata-{shared_token.parent.name}"
    limits: Final = {"max_context_window": 32000, "max_prompt_tokens": 24000, "max_output_tokens": 8000}
    supports: Final = {"tool_calls": True, "vision": True, "streaming": True}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        assert request.url.path == "/models"
        assert request.headers["authorization"] == f"Bearer {shared_token.read_text()}"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": model,
                        "supported_endpoints": endpoints,
                        "capabilities": {"limits": limits, "supports": supports},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client: Final = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "module_level_client", client)
        discovered: Final = GithubCopilotModelInfo(client=client).get_models(api_base=_SHARED_API)
        metadata: Final = get_model_info_helper(
            model=discovered[0],
            custom_llm_provider="github_copilot",
            api_base=_SHARED_API,
            api_key=shared_token.read_text(),
        )

    assert discovered == [f"github_copilot/{model}"]
    assert metadata["mode"] == expected_mode
    assert metadata["max_tokens"] == limits["max_context_window"]
    assert metadata["max_input_tokens"] == limits["max_prompt_tokens"]
    assert metadata["max_output_tokens"] == limits["max_output_tokens"]
    assert metadata.get("supports_function_calling") == supports["tool_calls"]
    assert metadata.get("supports_vision") == supports["vision"]
    assert metadata["input_cost_per_token"] is None
    assert metadata["output_cost_per_token"] is None
    assert requests.qsize() == 1
    assert discovered[0] not in litellm.model_cost


def test_runtime_metadata_isolated_across_callers_rotation_and_disconnect(
    shared_token: Path, user_credential: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()
    model: Final = f"caller-metadata-{shared_token.parent.name}"
    first_token: Final = f"first-{shared_token.parent.name}"
    second_token: Final = f"second-{shared_token.parent.name}"
    rotated_token: Final = f"rotated-{shared_token.parent.name}"
    first: Final = _user_params(user_credential, "first", first_token)
    second: Final = _user_params(user_credential, "second", second_token)
    rotated: Final = _user_params(user_credential, "first", rotated_token)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/copilot_internal/user":
            return _catalog_for_bearer(request)
        requests.put(request)
        token: Final = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": model,
                        "supported_endpoints": ["/responses" if token == first_token else "/chat/completions"],
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client: Final = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "module_level_client", client)
        info: Final = GithubCopilotModelInfo(client=client, cache=AvailableModelsCache())
        assert info.discover_models(first) == [f"github_copilot/{model}"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results: Final = tuple(
                pool.map(lambda params: info.get_model_info(model, litellm_params=params), (first, second) * 8)
            )
        rotated_info: Final = info.get_model_info(model, litellm_params=rotated)
        with pytest.raises(CallerCredentialAuthenticationError, match="Connect GitHub Copilot"):
            info.get_model_info(model, litellm_params={"litellm_credential_name": user_credential})

    assert tuple(result["mode"] for result in results) == ("responses", "chat") * 8
    assert rotated_info["mode"] == "chat"
    assert requests.qsize() == 3
    assert model not in litellm.model_cost


@pytest.mark.parametrize("mode", ["chat", "responses"])
def test_registered_routing_and_pricing_do_not_require_catalog_access(
    shared_token: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    model: Final = f"github_copilot/registered-metadata-{shared_token.parent.name}"
    registered: Final = {
        "mode": mode,
        "litellm_provider": "github_copilot",
        "max_tokens": 16000,
        "max_input_tokens": 24000,
        "max_output_tokens": 16000,
        "input_cost_per_token": 0.000002,
        "output_cost_per_token": 0.000004,
    }
    monkeypatch.setattr(litellm, "model_cost", {**litellm.model_cost, model: registered})
    shared_token.unlink()

    def reject_network(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Registered routing must not require discovery at {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(reject_network)) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        metadata: Final = info.get_model_info(model)

    assert metadata["mode"] == registered["mode"]
    assert metadata["max_tokens"] == registered["max_tokens"]
    assert metadata["input_cost_per_token"] == registered["input_cost_per_token"]
    assert metadata["output_cost_per_token"] == registered["output_cost_per_token"]


@pytest.mark.parametrize("entry", [{"id": "id-only"}, {"id": "other", "supported_endpoints": ["/responses"]}])
def test_missing_model_capabilities_do_not_become_chat_metadata(
    shared_token: Path, entry: Mapping[str, object]
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [entry]}))
    ) as http_client:
        info: Final = GithubCopilotModelInfo(client=HTTPHandler(client=http_client), cache=AvailableModelsCache())
        assert info.get_models(api_base=_SHARED_API) == [f"github_copilot/{entry['id']}"]
        with pytest.raises(ServiceUnavailableError, match="routing capabilities|absent"):
            info.get_model_info("id-only", api_base=_SHARED_API, api_key=shared_token.read_text())


def test_credential_free_metadata_does_not_cache_a_saved_accounts_catalog(
    shared_token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()
    model: Final = f"unscoped-metadata-{shared_token.parent.name}"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        return httpx.Response(200, json={"data": [{"id": model, "supported_endpoints": ["/responses"]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(litellm, "module_level_client", HTTPHandler(client=http_client))
        with pytest.raises(ModelNotMappedError):
            litellm.get_model_info(model=model, custom_llm_provider="github_copilot", api_base=_SHARED_API)

    assert requests.empty()


def test_explicit_metadata_credentials_do_not_borrow_the_saved_account(
    shared_token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: Final = SimpleQueue[httpx.Request]()
    model: Final = f"explicit-metadata-{shared_token.parent.name}"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.put(request)
        endpoint: Final = (
            "/responses" if request.headers["authorization"] == "Bearer explicit-first" else "/chat/completions"
        )
        return httpx.Response(200, json={"data": [{"id": model, "supported_endpoints": [endpoint]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(litellm, "module_level_client", HTTPHandler(client=http_client))
        metadata: Final = tuple(
            litellm.get_model_info(
                model=model, custom_llm_provider="github_copilot", api_base=_SHARED_API, api_key=token
            )
            for token in ("explicit-first", "explicit-second", "explicit-first")
        )

    assert tuple(info["mode"] for info in metadata) == ("responses", "chat", "responses")
    seen: Final = tuple(requests.get_nowait() for _ in range(requests.qsize()))
    assert tuple(request.headers["authorization"] for request in seen) == (
        "Bearer explicit-first",
        "Bearer explicit-second",
    )
