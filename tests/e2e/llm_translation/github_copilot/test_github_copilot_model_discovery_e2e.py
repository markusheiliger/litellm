from typing import Final

import pytest
from e2e_config import unique_marker
from e2e_metadata import Domain, Provider, Route, Subject, meta
from lifecycle import ResourceManager
from llm_translation.conversational_matrix import build_surfaces
from llm_translation.github_copilot.support import (
    Deployment,
    assert_catalog_matches,
    assert_recorded,
    cases,
    check_connection,
    listed,
    load_account,
    register_wildcard,
    select_model,
)
from llm_translation.sdk_clients import SdkClients
from proxy_client import ProxyClient

pytestmark: Final = [
    pytest.mark.e2e,
    pytest.mark.github_copilot_live,
    pytest.mark.proxy_wide_config,
    pytest.mark.provider_live,
]


class TestGithubCopilotDiscovery:
    @pytest.mark.parametrize("account", cases("discovery"), indirect=True)
    def test_wildcard_catalog_matches_upstream_and_a_listed_model_is_callable(
        self, proxy: ProxyClient, deployment: Deployment, sdk: SdkClients
    ) -> None:
        names: Final = listed(proxy, deployment)
        assert_catalog_matches(deployment, names)
        model: Final = select_model(deployment.account.catalog, "chat_completions")
        assert deployment.name(model) in names
        response: Final = build_surfaces(sdk)["chat_completions"].reply(
            deployment.key, deployment.name(model), f"Say hello briefly. Request marker {unique_marker()}."
        )
        assert response.response_id and response.text.strip()
        assert_recorded(proxy, deployment, model)

    @pytest.mark.covers("other.model_discovery.github_copilot.per_user_oauth.caller_isolation")
    @meta(Subject(domain=Domain.LLM_TRANSLATION, route=Route.MODEL_MANAGEMENT, providers=(Provider.GITHUB_COPILOT,)))
    def test_per_user_oauth_catalogs_remain_bound_to_each_caller(
        self, proxy: ProxyClient, resources: ResourceManager
    ) -> None:
        first: Final = load_account("per_user_oauth_a")
        second: Final = load_account("per_user_oauth_b")
        assert first.user_id != second.user_id, "The two dedicated LiteLLM users must be different."
        assert first.github_login != second.github_login, "Use two different dedicated GitHub accounts."
        assert first.credential == second.credential, "Both users must connect to the same named credential."
        deployment: Final = register_wildcard(proxy, resources, first, f"e2e-copilot-isolation-{unique_marker()}/*")
        second_key: Final = resources.key(models=[deployment.pattern], user_id=second.user_id)
        check_connection(proxy, second, second_key)
        other: Final = Deployment(account=second, pattern=deployment.pattern, key=second_key)
        for caller in (deployment, other, deployment):
            assert_catalog_matches(caller, listed(proxy, caller))
