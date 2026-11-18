from typing import Final, Protocol

import pytest
from e2e_config import unique_marker
from lifecycle import ResourceManager
from llm_translation.github_copilot.support import Account, Deployment, Profile, load_account, register_wildcard
from proxy_client import ProxyClient
from pydantic import TypeAdapter

_PROFILE: Final[TypeAdapter[Profile]] = TypeAdapter(Profile)


class AccountRequest(Protocol):
    @property
    def param(self) -> object: ...


@pytest.fixture(scope="module")
def account(request: AccountRequest) -> Account:
    return load_account(_PROFILE.validate_python(request.param))


@pytest.fixture
def deployment(proxy: ProxyClient, resources: ResourceManager, account: Account) -> Deployment:
    return register_wildcard(proxy, resources, account, f"e2e-copilot-{account.profile}-{unique_marker()}/*")
