from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NoReturn, Protocol, TypeAlias, assert_never

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, TypeAdapter, ValidationError

import litellm
from litellm.exceptions import (
    APIConnectionError,
    CallerCredentialAuthenticationError,
    CallerCredentialRateLimitError,
    DeploymentCredentialAuthenticationError,
    ModelNotMappedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.types.llms.openai import AllMessageValues
from litellm.types.router import LiteLLM_Params
from litellm.types.utils import ModelInfoBase

from .authenticator import Authenticator
from .common_utils import DEFAULT_GITHUB_COPILOT_API_BASE, get_copilot_default_headers, pin_session_authorization
from .per_user_auth import (
    GITHUB_COPILOT_USER_SESSION_KWARG_KEY,
    GithubCopilotUserSession,
    attach_github_copilot_user_session,
    github_copilot_scoped_session,
    require_github_copilot_user_session,
)

if TYPE_CHECKING:
    from litellm.utils import AvailableModelsCache

_PROVIDER: Final = "github_copilot"
_Mode: TypeAlias = Literal["shared", "per_user_oauth", "github_app"]
_FailureKind: TypeAlias = Literal["authentication", "rate_limit", "timeout", "connection", "upstream", "payload"]
_MODEL_INFO: Final = TypeAdapter(ModelInfoBase)
_REQUEST_FIELDS: Final = TypeAdapter(Mapping[str, object])
_MIXED_API_CALLS: Final = frozenset({"completion", "acompletion", "responses", "aresponses"})


class _CatalogCache(Protocol):
    def get_cache(self, key: str) -> object: ...

    def set_cache(self, key: str, value: object) -> None: ...

    def listing_lock(
        self, custom_llm_provider: str, litellm_params: LiteLLM_Params | None
    ) -> AbstractContextManager[object]: ...


class _RoutingFields(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    mode: StrictStr | None = None
    supported_endpoints: tuple[StrictStr, ...] | None = None

    def routing_mode(self) -> str | None:
        if self.mode is not None:
            return self.mode
        endpoints: Final = frozenset(path.removeprefix("/v1") for path in (self.supported_endpoints or ()))
        if "/responses" in endpoints:
            return "responses"
        if endpoints.intersection(("/chat/completions", "/messages")):
            return "chat"
        return "embedding" if "/embeddings" in endpoints else None


class _PreparedMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    model: StrictStr
    info: ModelInfoBase


class _Limits(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    max_context_window: StrictInt | None = Field(default=None, ge=0)
    max_prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    max_output_tokens: StrictInt | None = Field(default=None, ge=0)


class _Supports(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    streaming: StrictBool | None = None
    tool_calls: StrictBool | None = None
    parallel_tool_calls: StrictBool | None = None
    vision: StrictBool | None = None
    structured_outputs: StrictBool | None = None


class _Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    type: StrictStr | None = None
    limits: _Limits = _Limits()
    supports: _Supports = _Supports()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    id: StrictStr = Field(min_length=1)
    supported_endpoints: tuple[StrictStr, ...] = ()
    capabilities: _Capabilities = _Capabilities()

    def routing_mode(self) -> Literal["responses", "chat", "embedding"] | None:
        endpoints: Final = frozenset(path.removeprefix("/v1") for path in self.supported_endpoints)
        if "/responses" in endpoints:
            return "responses"
        if endpoints.intersection(("/chat/completions", "/messages")):
            return "chat"
        if "/embeddings" in endpoints:
            return "embedding"
        if endpoints:
            return None
        match self.capabilities.type:
            case "chat":
                return "chat"
            case "embeddings":
                return "embedding"
            case _:
                return None

    def metadata(self) -> ModelInfoBase | None:
        mode: Final = self.routing_mode()
        if mode is None:
            return None
        return ModelInfoBase(
            key=f"{_PROVIDER}/{self.id.removeprefix(f'{_PROVIDER}/')}",
            litellm_provider=_PROVIDER,
            mode=mode,
            supported_endpoints=list(f"/v1{path.removeprefix('/v1')}" for path in self.supported_endpoints),
            max_tokens=self.capabilities.limits.max_context_window,
            max_input_tokens=self.capabilities.limits.max_prompt_tokens,
            max_output_tokens=self.capabilities.limits.max_output_tokens,
            input_cost_per_token=None,
            output_cost_per_token=None,
            supports_function_calling=self.capabilities.supports.tool_calls,
            supports_parallel_function_calling=self.capabilities.supports.parallel_tool_calls,
            supports_vision=self.capabilities.supports.vision,
            supports_response_schema=self.capabilities.supports.structured_outputs,
        )


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
    scoped: Final = github_copilot_scoped_session(params, model="github_copilot/*")
    if scoped is not None:
        return _Auth(scoped, "per_user_oauth", scoped.credential_name, scoped.user_id)
    name: Final = params.get("litellm_credential_name")
    credential_name: Final = name if isinstance(name, str) and name else None
    if credential_name is not None and CredentialAccessor.find_credential(credential_name) is None:
        return _Failure("authentication", "The configured GitHub Copilot credential is unavailable.")
    resolved: Final = {key: value for key, value in params.items() if key != GITHUB_COPILOT_USER_SESSION_KWARG_KEY}
    attach_github_copilot_user_session(resolved, model="github_copilot/*")
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
    api_base: Final = authenticator.resolve_api_base(
        requested_base if isinstance(requested_base, str) else None,
        credential_name=credential_name,
        access_token=token,
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


def _catalog(client: HTTPHandler, auth: _Auth) -> _Catalog | _Failure:
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
    return catalog


def _static_metadata(model: str) -> ModelInfoBase | None:
    from litellm.utils import get_model_info_helper

    try:
        metadata: Final = get_model_info_helper(
            model=model, custom_llm_provider=_PROVIDER, catalog_only=True, preserve_unset_costs=True
        )
    except ModelNotMappedError:
        return None
    raw: Final[object] = litellm.model_cost.get(metadata["key"])
    fields: Final = _RoutingFields.model_validate(raw or {})
    return {
        **metadata,
        "supported_endpoints": (
            list(fields.supported_endpoints)
            if fields.supported_endpoints is not None
            else metadata.get("supported_endpoints")
        ),
    }


def _has_routing(metadata: ModelInfoBase) -> bool:
    return _RoutingFields.model_validate(metadata).routing_mode() is not None


def _explicit_auth(token: str | None, api_base: str | None) -> _Auth | _Failure:
    if not token:
        return _Failure("authentication", "GitHub Copilot runtime metadata requires a nonempty explicit token.")
    return _Auth(
        GithubCopilotUserSession(token, Authenticator().resolve_api_base(api_base, access_token=token)),
        "shared",
        None,
        None,
    )


def _model_key(model: str) -> str:
    return f"{_PROVIDER}/{model.removeprefix('responses/').removeprefix(f'{_PROVIDER}/').removeprefix('responses/')}"


def github_copilot_routing_mode(model: str, model_info: object = None) -> str | None:
    request: Final[Mapping[str, object]] = _REQUEST_FIELDS.validate_python(model_info) if model_info is not None else {}
    explicit: Final = _RoutingFields.model_validate(request).routing_mode()
    if explicit is not None:
        return explicit
    existing: Final = _static_metadata(_model_key(model))
    if existing is not None:
        static_mode: Final = _RoutingFields.model_validate(existing).routing_mode()
        if static_mode is not None:
            return static_mode
    prepared: Final = request.get(_PROVIDER)
    if prepared is None:
        return None
    bound: Final = _PreparedMetadata.model_validate(prepared)
    if bound.model != _model_key(model):
        return None
    return _RoutingFields.model_validate(bound.info).routing_mode()


def prepare_github_copilot_model_info(
    params: Mapping[str, object], model: str, call_type: str
) -> Mapping[str, object] | None:
    if call_type not in _MIXED_API_CALLS:
        return None
    request: Final = _REQUEST_FIELDS.validate_python(params.get("model_info") or {})
    configured: Final = {key: value for key, value in request.items() if key != _PROVIDER}
    if github_copilot_routing_mode(model, configured) is not None:
        return configured if _PROVIDER in request else None
    metadata: Final = GithubCopilotModelInfo().get_model_info(_model_key(model), litellm_params=params)
    if metadata is None:
        _raise_failure(_Failure("payload", "GitHub Copilot did not provide model routing metadata."), "shared")
    prepared: Final = _PreparedMetadata(model=_model_key(model), info=metadata)
    return {**configured, _PROVIDER: _REQUEST_FIELDS.validate_python(prepared.model_dump(mode="json"))}


class GithubCopilotModelInfo(BaseLLMModelInfo):
    def __init__(self, client: HTTPHandler | None = None, cache: AvailableModelsCache | None = None) -> None:
        self._client = client
        self._cache = cache

    @property
    def _catalog_cache(self) -> _CatalogCache:
        from litellm.utils import _model_cache  # pyright: ignore[reportPrivateUsage]  # reuse the SDK catalog cache

        return self._cache if self._cache is not None else _model_cache

    def _get_catalog(self, auth: _Auth) -> _Catalog:
        cache: Final = self._catalog_cache
        scope: Final = auth.cache_params()
        key: Final = f"{_PROVIDER}:catalog:{scope.model}"
        with cache.listing_lock(_PROVIDER, scope):
            cached: Final[object] = cache.get_cache(key)
            if isinstance(cached, _Catalog):
                return cached
            result: Final = _catalog(self._client if self._client is not None else litellm.module_level_client, auth)
            if isinstance(result, _Failure):
                _raise_failure(result, auth.mode)
            cache.set_cache(key, result)
            return result

    def discover_models(
        self, litellm_params: Mapping[str, object] | None = None
    ) -> list[str]:  # mutable-ok: existing provider discovery contract returns list[str]
        auth: Final = _resolve_auth(litellm_params or {})
        if isinstance(auth, _Failure):
            _raise_failure(auth, "shared")
        catalog: Final = self._get_catalog(auth)
        return list(dict.fromkeys(f"{_PROVIDER}/{model.id.removeprefix(f'{_PROVIDER}/')}" for model in catalog.data))

    def get_model_info(
        self,
        model: str,
        api_base: str | None = None,
        api_key: str | None = None,
        *,
        litellm_params: Mapping[str, object] | None = None,
    ) -> ModelInfoBase | None:
        existing: Final = _static_metadata(model)
        if existing is not None and _has_routing(existing):
            return existing
        if litellm_params is None and api_key is None:
            return existing
        auth: Final = (
            _resolve_auth({**litellm_params, **({"api_base": api_base} if api_base else {})})
            if litellm_params is not None
            else _explicit_auth(api_key, api_base)
        )
        if isinstance(auth, _Failure):
            _raise_failure(auth, "shared")
        catalog: Final = self._get_catalog(auth)
        base_model: Final = self.get_base_model(model)
        entry: Final = next((entry for entry in catalog.data if self.get_base_model(entry.id) == base_model), None)
        if entry is None:
            _raise_failure(
                _Failure("payload", "The requested model is absent from this GitHub Copilot catalog."), auth.mode
            )
        metadata: Final = entry.metadata()
        if metadata is None:
            _raise_failure(
                _Failure("payload", "GitHub Copilot did not provide routing capabilities for the requested model."),
                auth.mode,
            )
        overrides: Final = {key: value for key, value in (existing or {}).items() if value is not None}
        return _MODEL_INFO.validate_python({**metadata, **overrides})

    def get_models(
        self, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:  # mutable-ok: existing provider discovery contract returns list[str]
        return self.discover_models({"api_base": api_base})

    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return Authenticator().get_stored_access_token()

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str:
        return api_base or os.getenv("GITHUB_COPILOT_API_BASE") or DEFAULT_GITHUB_COPILOT_API_BASE

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
