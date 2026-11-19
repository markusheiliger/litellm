from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from typing import Final, TypeVar

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from dotenv import dotenv_values
from e2e_http import URL, NoBody, Result, Success, UnknownApiError, probe
from llm_translation.github_copilot.auth_app import (
    App,
    AppCredential,
    Installation,
    Owner,
    RegisteredApp,
    callback_receiver,
    installation_url,
    read_installation,
    registration_code,
    run,
    sync_proxy,
)
from llm_translation.github_copilot.support import (
    Account,
    Catalog,
    CatalogModel,
    CredentialValues,
    CredentialView,
    UpstreamTransport,
)
from pydantic import BaseModel, SecretStr

_R = TypeVar("_R", bound=BaseModel)


def test_registration_callback_is_reachable_without_browser_interception_or_secret_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret: Final = "synthetic-private-code"
    with callback_receiver() as callback:
        response: Final = probe(
            URL(f"{callback}?code={secret}&state={secret}"), headers=NoBody(), params=NoBody(), timeout=5
        )
        assert response.status_code == 200
        assert secret not in response.body
        assert registration_code(f"{callback}?code={secret}&state={secret}", callback, secret) == secret
    captured: Final = capsys.readouterr()
    assert secret not in captured.out + captured.err


@dataclass(frozen=True)
class ScriptedGithub:
    replies: SimpleQueue[BaseModel]
    writes: SimpleQueue[tuple[str, str, BaseModel]]

    def get(self, path: str, *, headers: BaseModel, params: BaseModel, response_type: type[_R]) -> Result[_R]:
        reply: Final = self.replies.get_nowait()
        if isinstance(reply, UnknownApiError):
            return reply
        return Success(status_code=200, data=response_type.model_validate(reply.model_dump()))

    def post(self, path: str, *, headers: BaseModel, json: BaseModel, response_type: type[_R]) -> Result[_R]:
        self.writes.put(("post", path, json))
        return Success(status_code=200, data=response_type.model_validate({}))

    def patch(self, path: str, *, headers: BaseModel, json: BaseModel, response_type: type[_R]) -> Result[_R]:
        self.writes.put(("patch", path, json))
        return Success(status_code=200, data=response_type.model_validate({}))


def _client(*responses: BaseModel) -> ScriptedGithub:
    replies: Final = SimpleQueue[BaseModel]()
    for response in responses:
        replies.put(response)
    return ScriptedGithub(replies, SimpleQueue())


@pytest.fixture(scope="module")
def private_key() -> str:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        .decode()
    )


def _environ(tmp_path: Path) -> Mapping[str, str]:
    env_file: Final = tmp_path / "selected.env"
    env_file.write_text("UNRELATED_SETTING=preserved\n")
    return {
        "E2E_GITHUB_COPILOT_ENV_FILE": str(env_file),
        "E2E_GITHUB_COPILOT_APP_ORGANIZATION": "test-org",
        "E2E_GITHUB_COPILOT_APP_LOGIN": "test-admin",
        "E2E_GITHUB_COPILOT_APP_CREDENTIAL": "dedicated-app",
        "LITELLM_PROXY_URL": "http://test-proxy",
        "LITELLM_MASTER_KEY": "synthetic-master",
        "XDG_DATA_HOME": str(tmp_path),
    }


def _app(*, organization: str = "test-org") -> App:
    return App(
        id=123,
        slug="test-app",
        html_url=f"https://github.com/apps/{organization}/test-app",
        owner=Owner(login=organization, type="Organization"),
    )


def _installation(*, app_id: int = 123, organization: str = "test-org") -> Installation:
    return Installation.model_validate(
        {
            "id": 456,
            "app_id": app_id,
            "account": {"login": organization, "type": "Organization"},
            "permissions": {"organization_copilot_requests": "read"},
        }
    )


def _no_registration(host: str, org: str, login: str, github: UpstreamTransport) -> RegisteredApp:
    pytest.fail("A saved App must not register another App or open a browser")


def _no_installation(host: str, login: str, app: App, lookup: Callable[[], Installation | None]) -> Installation:
    pytest.fail("An installed App must not request another browser approval")


def _verify(settings: Mapping[str, str]) -> Account:
    return Account(
        profile="github_app",
        credential=settings["E2E_GITHUB_COPILOT_APP_CREDENTIAL"],
        token=SecretStr("synthetic-installation-token"),
        api_base="https://api.githubcopilot.com",
        catalog=Catalog(data=(CatalogModel(id="test-chat-model"),)),
    )


def test_registration_persists_recoverable_credentials_before_entitlement_verification(
    tmp_path: Path, private_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings: Final = _environ(tmp_path)
    registrations: Final = SimpleQueue[str]()
    synchronized: Final = SimpleQueue[Mapping[str, str]]()
    client: Final = _client(_app(), UnknownApiError(status_code=404, body="missing"), _installation())

    def register(host: str, org: str, login: str, github: UpstreamTransport) -> RegisteredApp:
        registrations.put(org)
        assert login == settings["E2E_GITHUB_COPILOT_APP_LOGIN"]
        app: Final = _app()
        return RegisteredApp(
            id=app.id, slug=app.slug, html_url=app.html_url, owner=app.owner, pem=SecretStr(private_key)
        )

    def install(host: str, login: str, app: App, lookup: Callable[[], Installation | None]) -> Installation:
        result: Final = lookup()
        assert result is not None
        return result

    def denied(settings: Mapping[str, str]) -> Account:
        pytest.fail("Synthetic organization entitlement denial", pytrace=False)

    with pytest.raises(pytest.fail.Exception, match="entitlement"):
        run(settings, github=client, register=register, install=install, verify=denied, sync=synchronized.put)
    assert registrations.get_nowait() == settings["E2E_GITHUB_COPILOT_APP_ORGANIZATION"]
    assert registrations.empty() and synchronized.empty() and client.writes.empty()
    saved: Final = dotenv_values(settings["E2E_GITHUB_COPILOT_ENV_FILE"])
    key_file: Final = Path(str(saved["E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE"]))
    assert key_file.read_text().strip() == private_key.strip()
    assert key_file.stat().st_mode & 0o077 == 0
    assert saved["UNRELATED_SETTING"] == "preserved"
    retry: Final = {**settings, **{key: value for key, value in saved.items() if value is not None}}
    run(
        retry,
        github=_client(_app(), _installation()),
        register=_no_registration,
        install=_no_installation,
        verify=_verify,
        sync=synchronized.put,
    )
    result: Final = synchronized.get_nowait()
    assert result["E2E_GITHUB_COPILOT_APP_ID"] == str(_app().id)
    assert result["E2E_GITHUB_COPILOT_APP_INSTALLATION_ID"] == str(_installation().id)
    assert synchronized.empty()
    captured: Final = capsys.readouterr()
    assert private_key not in captured.out + captured.err


@pytest.mark.parametrize("mismatch", ("app_owner", "installation_owner", "installation_app", "saved_installation"))
def test_saved_app_or_installation_mismatch_cannot_change_proxy_credentials(
    tmp_path: Path, private_key: str, mismatch: str
) -> None:
    settings: Final = _environ(tmp_path)
    key_file: Final = tmp_path / "existing.pem"
    key_file.write_text(private_key)
    saved: Final = {
        **settings,
        "E2E_GITHUB_COPILOT_APP_ID": str(_app().id),
        "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file),
        **(
            {"E2E_GITHUB_COPILOT_APP_INSTALLATION_ID": "different-installation"}
            if mismatch == "saved_installation"
            else {}
        ),
    }
    client: Final = _client(
        _app(organization="other-org") if mismatch == "app_owner" else _app(),
        _installation(
            app_id=999 if mismatch == "installation_app" else _app().id,
            organization="other-org" if mismatch == "installation_owner" else "test-org",
        ),
    )
    writes: Final = SimpleQueue[Mapping[str, str]]()
    with pytest.raises(pytest.fail.Exception, match="organization|Installation|installation"):
        run(
            saved,
            github=client,
            register=_no_registration,
            install=_no_installation,
            verify=_verify,
            sync=writes.put,
        )
    assert writes.empty()
    assert key_file.read_text() == private_key
    assert dotenv_values(settings["E2E_GITHUB_COPILOT_ENV_FILE"]) == {"UNRELATED_SETTING": "preserved"}


@pytest.mark.parametrize("status", (401, 403, 429, 500))
def test_installation_api_failure_cannot_trigger_another_registration_or_installation(
    tmp_path: Path, private_key: str, status: int
) -> None:
    settings: Final = _environ(tmp_path)
    key_file: Final = tmp_path / "existing.pem"
    key_file.write_text(private_key)
    with pytest.raises(pytest.fail.Exception, match="installation verification"):
        read_installation(
            _client(UnknownApiError(status_code=status, body="sensitive-upstream-body")),
            {
                **settings,
                "E2E_GITHUB_COPILOT_APP_ID": str(_app().id),
                "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file),
            },
            _app(),
        )


@pytest.mark.parametrize(
    "query", ("code=secret", "code=secret&state=wrong", "code=secret&state=expected&state=expected", "state=expected")
)
def test_manifest_callback_rejects_missing_or_ambiguous_state_and_code(query: str) -> None:
    callback: Final = "http://127.0.0.1/copilot-e2e/random-path"
    with pytest.raises(pytest.fail.Exception):
        registration_code(f"{callback}?{query}", callback, "expected")
    assert registration_code("https://unrelated.test/?code=secret&state=expected", callback, "expected") is None
    assert registration_code(f"{callback}?code=secret&state=expected", callback, "expected") == "secret"


@pytest.mark.parametrize("exists", (False, True))
def test_proxy_receives_only_the_selected_app_and_a_serializable_private_key(
    tmp_path: Path, private_key: str, exists: bool
) -> None:
    settings: Final = _environ(tmp_path)
    key_file: Final = tmp_path / "key.pem"
    key_file.write_text(private_key)
    client: Final = _client(
        CredentialView(
            credential_values=CredentialValues(
                github_copilot_auth_type="github_app",
                github_copilot_app_id=str(_app().id),
            )
        )
        if exists
        else UnknownApiError(status_code=404, body="missing")
    )
    sync_proxy(
        {
            **settings,
            "E2E_GITHUB_COPILOT_APP_ID": str(_app().id),
            "E2E_GITHUB_COPILOT_APP_INSTALLATION_ID": str(_installation().id),
            "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file),
        },
        proxy=client,
    )
    method, path, body = client.writes.get_nowait()
    parsed: Final = AppCredential.model_validate_json(body.model_dump_json())
    assert parsed.credential_name == settings["E2E_GITHUB_COPILOT_APP_CREDENTIAL"]
    assert parsed.credential_values.github_copilot_app_id == str(_app().id)
    assert parsed.credential_values.github_copilot_app_installation_id == str(_installation().id)
    assert parsed.credential_values.github_copilot_app_private_key == private_key.strip()
    assert private_key not in repr(body)
    assert (method, path) == (
        ("patch", f"/credentials/{parsed.credential_name}") if exists else ("post", "/credentials")
    )
    assert client.writes.empty()


@pytest.mark.parametrize("mode", ("shared", "per_user_oauth"))
def test_proxy_oauth_credential_cannot_be_overwritten_by_app_setup(tmp_path: Path, mode: str) -> None:
    settings: Final = _environ(tmp_path)
    client: Final = _client(CredentialView(credential_values=CredentialValues(github_copilot_auth_type=mode)))
    with pytest.raises(pytest.fail.Exception, match="Refusing to replace"):
        sync_proxy(settings, proxy=client)
    assert client.writes.empty()


def test_new_registration_cannot_overwrite_an_existing_private_key(tmp_path: Path) -> None:
    settings: Final = _environ(tmp_path)
    key_file: Final = tmp_path / "existing.pem"
    key_file.write_text("existing-protected-content")
    with pytest.raises(pytest.fail.Exception, match="must not overwrite"):
        run(
            {**settings, "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file)},
            github=_client(),
            register=_no_registration,
            install=_no_installation,
            verify=_verify,
        )
    assert key_file.read_text() == "existing-protected-content"
    assert dotenv_values(settings["E2E_GITHUB_COPILOT_ENV_FILE"]) == {"UNRELATED_SETTING": "preserved"}


def test_saved_app_accepts_its_verified_client_id_without_an_administrator_login(
    tmp_path: Path, private_key: str
) -> None:
    settings: Final = _environ(tmp_path)
    key_file: Final = tmp_path / "existing.pem"
    key_file.write_text(private_key)
    client_id: Final = "synthetic-client-id"
    app: Final = App(id=_app().id, client_id=client_id, slug=_app().slug, html_url=_app().html_url, owner=_app().owner)
    synchronized: Final = SimpleQueue[Mapping[str, str]]()
    run(
        {
            **settings,
            "E2E_GITHUB_COPILOT_APP_ID": client_id,
            "E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE": str(key_file),
            "E2E_GITHUB_COPILOT_APP_LOGIN": "",
        },
        github=_client(app, _installation()),
        register=_no_registration,
        install=_no_installation,
        verify=_verify,
        sync=synchronized.put,
    )
    assert synchronized.get_nowait()["E2E_GITHUB_COPILOT_APP_ID"] == app.client_id
    assert synchronized.empty()


@pytest.mark.parametrize("namespaced", (False, True))
def test_installation_preserves_githubs_canonical_app_namespace(namespaced: bool) -> None:
    app: Final = _app() if namespaced else _app().model_copy(update={"html_url": "https://github.com/apps/test-app"})
    assert installation_url(app, "github.com").removesuffix("/installations/new") == app.html_url


@pytest.mark.parametrize(
    "url",
    (
        "https://unrelated.test/apps/test-app",
        "http://github.com/apps/test-app",
        "https://user:password@github.com/apps/test-app",
        "https://github.com/apps/test-app?code=private",
    ),
)
def test_untrusted_canonical_app_url_cannot_receive_browser_credentials(url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="No browser credentials were sent"):
        installation_url(_app().model_copy(update={"html_url": url}), "github.com")
