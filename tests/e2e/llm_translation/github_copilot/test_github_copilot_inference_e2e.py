import math
from typing import Final, Literal

import openai
import pytest
from e2e_config import unique_marker
from e2e_http import unwrap
from e2e_metadata import Domain, Provider, Route, Subject, meta
from lifecycle import ResourceManager
from llm_translation.conversational_matrix import build_surfaces
from llm_translation.github_copilot.support import (
    Deployment,
    assert_recorded,
    cases,
    load_account,
    register_wildcard,
    select_model,
)
from llm_translation.sdk_clients import NO_PROXY_CACHE, SdkClients
from models import UserNewBody, UserNewResponse
from proxy_client import ProxyClient

pytestmark: Final = [pytest.mark.e2e, pytest.mark.github_copilot_live, pytest.mark.provider_live]


class TestGithubCopilotInference:
    @pytest.mark.parametrize("account", cases("chat_completions"), indirect=True)
    def test_chat_completions(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "chat_completions", stream=False)

    @pytest.mark.parametrize("account", cases("chat_completions", stream=True), indirect=True)
    def test_chat_completions_stream(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "chat_completions", stream=True)

    @pytest.mark.parametrize("account", cases("responses"), indirect=True)
    def test_responses(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "responses", stream=False)

    @pytest.mark.parametrize("account", cases("responses", stream=True), indirect=True)
    def test_responses_stream(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "responses", stream=True)

    @pytest.mark.parametrize("account", cases("messages"), indirect=True)
    def test_messages(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "messages", stream=False)

    @pytest.mark.parametrize("account", cases("messages", stream=True), indirect=True)
    def test_messages_stream(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        _reply(proxy, deployment, sdk, "messages", stream=True)

    @pytest.mark.parametrize("account", cases("embeddings"), indirect=True)
    def test_embeddings(self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients) -> None:
        model: Final = select_model(deployment.account.catalog, "embeddings")
        response: Final = sdk.openai(deployment.key).embeddings.create(
            model=deployment.name(model),
            input=[f"A short greeting {unique_marker()}", f"A short farewell {unique_marker()}"],
            extra_body=NO_PROXY_CACHE,
        )
        assert tuple(item.index for item in response.data) == (0, 1)
        assert all(item.embedding and all(math.isfinite(value) for value in item.embedding) for item in response.data)
        assert any(value != 0 for value in response.data[0].embedding), "The embedding vector is all zeros."
        assert response.usage.prompt_tokens > 0 and response.usage.total_tokens >= response.usage.prompt_tokens
        assert_recorded(proxy, deployment, model)

    @pytest.mark.covers("other.auth.github_copilot.unconnected_denied")
    @meta(Subject(domain=Domain.PROXY_AUTH, route=Route.CHAT_COMPLETIONS, providers=(Provider.GITHUB_COPILOT,)))
    def test_unconnected_user_cannot_fall_back_to_shared_auth(
        self, proxy: ProxyClient, resources: ResourceManager, sdk: SdkClients
    ) -> None:
        shared: Final = load_account("shared")
        control: Final = register_wildcard(proxy, resources, shared, f"e2e-copilot-shared-control-{unique_marker()}/*")
        _reply(proxy, control, sdk, "chat_completions", stream=False)
        connected: Final = load_account("per_user_oauth_a")
        registered: Final = register_wildcard(
            proxy, resources, connected, f"e2e-copilot-unconnected-{unique_marker()}/*"
        )
        marker: Final = unique_marker()
        user: Final = unwrap(
            proxy.transport.post(
                "/user/new",
                headers=proxy.transport.master,
                json=UserNewBody(
                    user_id=f"e2e-copilot-{marker}",
                    user_email=f"{marker}@example.com",
                    user_role="internal_user",
                    auto_create_key=False,
                ),
                response_type=UserNewResponse,
            )
        )
        resources.defer(lambda: proxy.delete_user(user.user_id))
        key: Final = resources.key(models=[registered.pattern], user_id=user.user_id)
        model: Final = select_model(connected.catalog, "chat_completions")
        with pytest.raises(openai.AuthenticationError):
            sdk.openai(key).chat.completions.create(
                model=registered.name(model),
                messages=[{"role": "user", "content": f"Say hello {unique_marker()}"}],
                max_completion_tokens=64,
                extra_body=NO_PROXY_CACHE,
            )


def _reply(
    proxy: ProxyClient,
    deployment: Deployment,
    sdk: SdkClients,
    endpoint: Literal["chat_completions", "responses", "messages"],
    *,
    stream: bool,
) -> None:
    model: Final = select_model(deployment.account.catalog, endpoint, stream=stream)
    surface: Final = build_surfaces(sdk)[endpoint]
    prompt: Final = f"Say hello in one short sentence. Request marker {unique_marker()}."
    if stream:
        response: Final = surface.stream(deployment.key, deployment.name(model), prompt)
        assert response.text.strip(), "The Copilot stream returned no text."
        assert response.event_count > 1 and response.finished, "The stream lost its deltas or terminal event."
        assert response.usage_reported, "The stream omitted usage."
    else:
        reply: Final = surface.reply(deployment.key, deployment.name(model), prompt)
        assert reply.response_id and reply.text.strip(), "The Copilot response returned no ID or text."
        assert reply.call_id_header, "The proxy omitted the request correlation header."
        assert reply.usage is not None and reply.usage.input_tokens > 0 and reply.usage.output_tokens > 0
    assert_recorded(proxy, deployment, model)
