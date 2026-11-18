from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NoReturn, TypeAlias, assert_never

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

import litellm
from litellm.exceptions import (
    APIConnectionError,
    CallerCredentialAuthenticationError,
    CallerCredentialRateLimitError,
    DeploymentCredentialAuthenticationError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.types.llms.openai import AllMessageValues
from litellm.types.router import LiteLLM_Params

from .authenticator import Authenticator, shared_copilot_api_base
from .common_utils import get_copilot_default_headers, pin_session_authorization
from .per_user_auth import (
    GITHUB_COPILOT_USER_SESSION_KWARG_KEY,
    GithubCopilotUserSession,
    attach_github_copilot_user_session,
    require_github_copilot_user_session,
)

if TYPE_CHECKING:
    from litellm.utils import AvailableModelsCache

_PROVIDER: Final = "github_copilot"
_Mode: TypeAlias = Literal["shared", "per_user_oauth", "github_app"]
_FailureKind: TypeAlias = Literal["authentication", "rate_limit", "timeout", "connection", "upstream", "payload"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    id: StrictStr = Field(min_length=1)


class _Catalog(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    data: tuple[_Model, ...]


class _Caller(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    user_provider_credentials_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Failure:
    kind: _FailureKind
    message: str


@dataclass(frozen=True, slots=True)
class _Auth:
    session: GithubCopilotUserSession
    mode: _Mode
    credential_name: str | None
    user_id: str | None

    def cache_params(self) -> LiteLLM_Params:
        scope: Final = (
            self.mode,
            self.credential_name,
            self.user_id,
            self.session.api_base,
            self.session.integration_id,
            hashlib.sha256(self.session.token.encode()).hexdigest(),
        )
        return LiteLLM_Params(model=hashlib.sha256(json.dumps(scope).encode()).hexdigest())


def _resolve_auth(params: Mapping[str, object]) -> _Auth | _Failure:
    name: Final = params.get("litellm_credential_name")
    credential_name: Final = name if isinstance(name, str) and name else None
    if credential_name is not None and CredentialAccessor.find_credential(credential_name) is None:
        return _Failure("authentication", "The configured GitHub Copilot credential is unavailable.")
    resolved: Final = {key: value for key, value in params.items() if key != GITHUB_COPILOT_USER_SESSION_KWARG_KEY}
    attach_github_copilot_user_session(resolved)
    session: Final = require_github_copilot_user_session(resolved)
    if session is not None:
        caller_id: Final = (
            _Caller.model_validate(params.get("secret_fields") or {}).user_provider_credentials_user_id
            if not session.is_app
            else None
        )
        return _Auth(
            session,
            "github_app" if session.is_app else "per_user_oauth",
            credential_name,
            caller_id,
        )
    authenticator: Final = Authenticator()
    token: Final = authenticator.get_stored_access_token()
    if token is None:
        return _Failure(
            "authentication",
            "GitHub Copilot model discovery requires a stored access token. Complete device-code login separately.",
        )
    requested_base: Final = params.get("api_base")
    api_base: Final = shared_copilot_api_base(
        authenticator, requested_base if isinstance(requested_base, str) else None, access_token=token
    )
    return _Auth(GithubCopilotUserSession(token, api_base), "shared", credential_name, None)


def _raise_failure(failure: _Failure, mode: _Mode) -> NoReturn:
    match failure.kind:
        case "authentication":
            error: Final = (
                CallerCredentialAuthenticationError
                if mode == "per_user_oauth"
                else DeploymentCredentialAuthenticationError
            )
            raise error(message=failure.message, llm_provider=_PROVIDER, model="")
        case "rate_limit":
            rate_error: Final = CallerCredentialRateLimitError if mode == "per_user_oauth" else RateLimitError
            raise rate_error(message=failure.message, llm_provider=_PROVIDER, model="")
        case "timeout":
            raise Timeout(message=failure.message, llm_provider=_PROVIDER, model="")
        case "connection":
            raise APIConnectionError(message=failure.message, llm_provider=_PROVIDER, model="")
        case "upstream" | "payload":
            raise ServiceUnavailableError(message=failure.message, llm_provider=_PROVIDER, model="")
    assert_never(failure.kind)


def _catalog(client: HTTPHandler, auth: _Auth) -> tuple[str, ...] | _Failure:
    headers: Final = get_copilot_default_headers(auth.session.token)
    pin_session_authorization(headers, auth.session.token, auth.session.integration_id)
    try:
        response: Final = client.client.get(
            f"{auth.session.api_base.rstrip('/')}/models",
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
        )
    except httpx.TimeoutException:
        return _Failure("timeout", "GitHub Copilot model discovery timed out.")
    except httpx.RequestError:
        return _Failure("connection", "GitHub Copilot model discovery could not reach the provider.")
    if response.status_code in (401, 403):
        return _Failure("authentication", "GitHub Copilot rejected the configured model discovery credentials.")
    if response.status_code == 429:
        return _Failure("rate_limit", "GitHub Copilot rate limited model discovery.")
    if not response.is_success:
        return _Failure("upstream", f"GitHub Copilot model discovery failed with status {response.status_code}.")
    try:
        catalog: Final = _Catalog.model_validate_json(response.content)
    except ValidationError:
        return _Failure("payload", "GitHub Copilot model discovery returned an invalid catalog.")
    return tuple(dict.fromkeys(f"{_PROVIDER}/{model.id.removeprefix(f'{_PROVIDER}/')}" for model in catalog.data))


class GithubCopilotModelInfo(BaseLLMModelInfo):
    def __init__(self, client: HTTPHandler | None = None, cache: AvailableModelsCache | None = None) -> None:
        self._client = client
        self._cache = cache

    def discover_models(
        self, litellm_params: Mapping[str, object] | None = None
    ) -> list[str]:  # mutable-ok: existing provider discovery contract returns list[str]
        from litellm.utils import _model_cache  # pyright: ignore[reportPrivateUsage]  # reuse the SDK catalog cache

        auth: Final = _resolve_auth(litellm_params or {})
        if isinstance(auth, _Failure):
            _raise_failure(auth, "shared")
        cache: Final = self._cache if self._cache is not None else _model_cache
        scope: Final = auth.cache_params()
        with cache.listing_lock(_PROVIDER, scope):
            cached: Final = cache.get_cached_model_info(_PROVIDER, scope)
            if cached is not None:
                return cached
            result: Final = _catalog(self._client if self._client is not None else litellm.module_level_client, auth)
            if isinstance(result, _Failure):
                _raise_failure(result, auth.mode)
            models: Final = list(result)
            cache.set_cached_model_info(_PROVIDER, scope, models)
            return models

    def get_models(
        self, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:  # mutable-ok: existing provider discovery contract returns list[str]
        return self.discover_models({"api_base": api_base})

    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return Authenticator().get_stored_access_token()

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str:
        return shared_copilot_api_base(Authenticator(), api_base)

    def validate_environment(
        self,
        headers: Mapping[str, str],
        model: str,
        messages: Sequence[AllMessageValues],
        optional_params: Mapping[str, object],
        litellm_params: Mapping[str, object],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict[str, str]:  # mutable-ok: existing provider environment contract returns dict[str, str]
        auth: Final = _resolve_auth(litellm_params)
        if isinstance(auth, _Failure):
            _raise_failure(auth, "shared")
        validated: Final = {**get_copilot_default_headers(auth.session.token), **headers}
        pin_session_authorization(validated, auth.session.token, auth.session.integration_id)
        return validated

    @staticmethod
    def get_base_model(model: str) -> str:
        return model.removeprefix(f"{_PROVIDER}/")
