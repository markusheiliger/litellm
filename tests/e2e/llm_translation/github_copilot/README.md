# GitHub Copilot live tests

The Copilot suite is opt-in because it needs dedicated entitled accounts or an enabled GitHub App installation.
The default pipeline deselects every Copilot case without checking those credentials. Set `E2E_GITHUB_COPILOT_LIVE=1`
only in a provisioned environment. Once enabled, missing secrets, connections, models and provider errors fail the run.
The existing opt-in mechanism treats any nonempty value as enabled, including `0`. Unset a switch to disable it.

## Host and identity selection

Select one host per run with `E2E_GITHUB_COPILOT_HOST`. It defaults to `github.com` and also accepts a bare
`<enterprise>.ghe.com` hostname. URLs, literal wildcards, nested enterprise subdomains and custom GitHub
Enterprise Server domains are not supported.

This selects the test oracle's host. Shared OAuth, per-user OAuth and App authentication use the host stored
on their respective named credentials. Every selected credential must match the test oracle's host.

```bash
export E2E_GITHUB_COPILOT_HOST=github.com
```

For an enterprise run, set it to your enterprise hostname instead. Use a matching proxy configuration and a
separate set of dedicated accounts, approved connections, App identifiers and secret files for that host.
The runner does not select identities from `gh`, your editor, `GH_HOST`, `GITHUB_TOKEN` or the development
proxy's login URL overrides. Missing test credentials fail rather than falling back to those identities.
Your development login does not need to change.

## Proxy setup

Use an externally provisioned proxy built from the revision under test. The tests do not start a proxy, register a
GitHub App or approve device logins during test runs. The separate `--auth` setup mode guides OAuth approval.
Use the normal Postgres, shared Redis, management and spend-log configuration
described in the [e2e setup guide](../../CONTRIBUTING.md).
Point the runner at it with `LITELLM_PROXY_URL` and `LITELLM_MASTER_KEY`. Keep the encryption configuration and database
across runs so approved per-user connections survive. Configure both router Redis and `general_settings.coordination_redis`
for the same dedicated Redis instance. Use two workers when the serving mode supports it.

### Creating the test instance

These tests follow the shared e2e harness pattern. Start the proxy before invoking pytest, either manually or
in a CI setup step. Pytest connects to that instance and creates temporary model deployments and keys, but does
not start or stop the proxy or its infrastructure. One instance can serve all selected authentication modes
and can be reused across runs. Keep the approved user connections and named credentials in place.

For local development, follow the [existing e2e setup instructions](../../CONTRIBUTING.md#running-the-tests-locally)
to run Postgres and Redis in Docker and the proxy from your checkout, using the Copilot configuration below.
For a containerized proxy, follow the
[Docker development guide](../../../../litellm/deploy/docker/README.md#building-and-running-the-application)
and reuse the [existing Compose stack](../../../../litellm/deploy/compose/docker-compose.yml).
Build from the checkout with `--build` rather than testing an unrelated published image. The Compose stack
provides a proxy and Postgres, but still needs Redis, the Copilot configuration and its credential mounts.

The current Compose file fixes the database volume name to `litellm_postgres_data`. Override that volume name
for an isolated test database. A different Compose project name alone does not isolate it. Postgres is reachable
inside the Compose network, but its port is not published to the host. A host-run proxy needs an explicitly
published database port or a separately provisioned database.

Use a non-production test environment. Inference-only cases can share a suitably configured test proxy.
Discovery cases require exclusive use of a stack configured with `litellm_settings.check_provider_endpoint: true`,
without unrelated suites sharing its deployments or database. `E2E_PROXY_WIDE_CONFIG_STACK=1` opts into those
cases, but does not provision or reconfigure the stack.

The provider resolves GitHub and Copilot endpoints from the same host rules for all three authentication modes.
Shared OAuth uses its named credential's host for both catalog discovery and inference, and caches discovered
endpoints separately for each host and token. An unsuccessful enterprise lookup never selects the public
Copilot default. Explicit shared deployment API-base overrides retain their existing behavior.

Set `GITHUB_COPILOT_DEVICE_CODE_URL` and `GITHUB_COPILOT_ACCESS_TOKEN_URL` explicitly for the standalone login
process. Derive both URLs from `E2E_GITHUB_COPILOT_HOST`, as below. The same explicit settings on a shared-auth
test proxy avoid inheriting development login URLs. These device-login settings do not select the catalog host.

Give each test host and account a dedicated `GITHUB_COPILOT_TOKEN_DIR`, never your development token directory.
All three modes select their API host from the named credential. All selected hosts must match the runner.
The commands below clear inherited development overrides.

For a local proxy, use a copy of the development config with a dedicated database and credentials. Point both
`router_settings.redis_host` / `redis_port` and `general_settings.coordination_redis.host` / `port` at the same
dedicated Redis instance, with credentials when required. Pass the test environment explicitly when starting it.

```bash
env -u GITHUB_COPILOT_API_BASE -u GITHUB_COPILOT_API_KEY_URL -u GITHUB_COPILOT_CLIENT_ID \
  GITHUB_COPILOT_DEVICE_CODE_URL="https://$E2E_GITHUB_COPILOT_HOST/login/device/code" \
  GITHUB_COPILOT_ACCESS_TOKEN_URL="https://$E2E_GITHUB_COPILOT_HOST/login/oauth/access_token" \
  GITHUB_COPILOT_TOKEN_DIR="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/shared" \
  GITHUB_COPILOT_ACCESS_TOKEN_FILE=access-token \
  python litellm/proxy/proxy_cli.py --config /secure/copilot-e2e-config.yaml \
  --num_workers 2 --detailed_debug --reload --use_v2_migration_resolver \
  2>&1 | tee /secure/copilot-e2e.log
```

Keep logs private. Check that two worker PIDs are serving. If reload reports that it ignores the worker count,
record that limitation instead. For Docker, pass the same host variables explicitly and mount only the
dedicated test token directory and App secrets.

### Named credentials

Provision distinct named credentials for shared and App authentication and one per-user credential shared by the two
dedicated users. The proxy config can contain the following credential definitions, using private environment values
on the proxy host. No permanent model deployments are needed.

```yaml
credential_list:
  - credential_name: e2e-copilot-shared
    credential_values:
      github_copilot_auth_type: shared
      github_copilot_host: os.environ/E2E_GITHUB_COPILOT_HOST
    credential_info:
      custom_llm_provider: github_copilot
  - credential_name: e2e-copilot-users
    credential_values:
      github_copilot_auth_type: per_user_oauth
      github_copilot_host: os.environ/E2E_GITHUB_COPILOT_HOST
    credential_info:
      custom_llm_provider: github_copilot
  - credential_name: e2e-copilot-app
    credential_values:
      github_copilot_auth_type: github_app
      github_copilot_host: os.environ/E2E_GITHUB_COPILOT_HOST
      github_copilot_app_id: os.environ/GITHUB_COPILOT_APP_ID
      github_copilot_app_installation_id: os.environ/GITHUB_COPILOT_APP_INSTALLATION_ID
      github_copilot_app_private_key_ref: GITHUB_COPILOT_APP_PRIVATE_KEY
    credential_info:
      custom_llm_provider: github_copilot
```

## Account and App prerequisites

For shared mode, complete device login outside the proxy with the selected host and a dedicated directory.
Approve the device code using the intended test account, using a separate browser profile if necessary.

```bash
umask 077
env -u GITHUB_COPILOT_API_KEY_URL -u GITHUB_COPILOT_CLIENT_ID \
  GITHUB_COPILOT_DEVICE_CODE_URL="https://$E2E_GITHUB_COPILOT_HOST/login/device/code" \
  GITHUB_COPILOT_ACCESS_TOKEN_URL="https://$E2E_GITHUB_COPILOT_HOST/login/oauth/access_token" \
  GITHUB_COPILOT_TOKEN_DIR="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/shared" \
  GITHUB_COPILOT_ACCESS_TOKEN_FILE=access-token \
  python -c "from litellm.llms.github_copilot.authenticator import Authenticator; Authenticator().get_access_token()"
```

Mount that login's token directory into the proxy and set its `GITHUB_COPILOT_TOKEN_DIR`. Make the same dedicated
`access-token` available to the runner through a protected file. Do not copy tokens into committed config.
The provider's default token directory is shared across hosts. Always use explicitly selected directories
for these tests to avoid reusing a development login or another tenant's token.

For per-user mode, create two dedicated LiteLLM users with distinct entitled GitHub accounts. As each user, connect
`e2e-copilot-users` through the dashboard, or call `POST /credentials/e2e-copilot-users/user_connection/start`, approve
the returned user code at its verification URI, then call
`POST /credentials/e2e-copilot-users/user_connection/poll` with `{"flow_handle": "<returned handle>"}`.
Respect the returned polling interval and wait for `connected`. Do not send the flow handle or device code to logs.
`GET /credentials/user_connections` reads the caller's saved connection.

Use `run-e2e.sh --auth` to copy the proxy's persisted per-user token into the runner's protected token file.
Setup calls the existing proxy credential loader against the dedicated test database, using the proxy's encryption
configuration. It does not change production code or expose tokens through an HTTP endpoint. Valid connections are
reused after direct GitHub identity verification. Missing or expired connections need one proxy device approval,
after which setup reads the newly persisted token. There is no separate runner device authorization.

Routine tests read the protected files and call GitHub directly for identity and catalog verification. They do not
read the database or require its encryption key. Supply each user's LiteLLM user ID, not their GitHub login.
Tests preserve these users and connections.

For App mode, set `E2E_GITHUB_COPILOT_APP_ORGANIZATION` to the organization's URL name on the selected host and
`E2E_GITHUB_COPILOT_APP_LOGIN` to the administrator's sign-in. Run `--auth` to register a dedicated private App
through GitHub's manifest flow and approve installation on that organization. The manifest requests only
Copilot Requests read permission. Organization policy must allow registration and installation, and the organization
must be enabled for App Copilot requests. Installation alone does not grant that entitlement.
For a new App, leave `APP_ID`, `APP_INSTALLATION_ID` and `APP_PRIVATE_KEY_FILE` blank so setup generates them.

Setup saves the App ID, installation ID and private-key path into the selected dotenv file. The generated RSA key
is saved outside the checkout with owner-only permissions. By default its path is
`$XDG_DATA_HOME/litellm/copilot-e2e/tokens/<host>/app/<organization>/private-key.pem`,
using `~/.local/share` when `XDG_DATA_HOME` is unset. An explicit `APP_PRIVATE_KEY_FILE` can select another absolute
path. Setup never overwrites an existing key during registration. Existing App credentials are reused, and an
interrupted installation resumes without registering another App.
Registration and installation share one isolated administrator browser session. When resuming an uninstalled
private App, setup signs into organization settings before opening its installation page.
Installation uses GitHub's returned App URL, preserving organization namespaces on enterprise hosts.

Setup verifies the App's owner and installation through GitHub, checks direct Copilot catalog access and creates or
updates the selected proxy credential through the existing management API. The proxy encrypts the stored private key.
It refuses to overwrite an OAuth credential, another host's credential or another App's credential.
Use a dedicated database-managed credential name, not a name owned by the proxy's YAML configuration.
For guided setup, omit the App entry from the YAML example above because `--auth` creates that credential in the database.
Registration credentials are saved before entitlement checks so an organization-policy failure can be corrected
and retried without losing the App.

The runner uses PyJWT and the shared HTTP transport to mint its own installation token independently of the provider.
Normal App tests require no browser, database credentials or human interaction after provisioning.

Direct catalog checks send `x-github-api-version: 2025-04-01`, matching the proxy's API contract.
An unversioned GitHub catalog can advertise a different model set.

## Runner configuration

Set the following runner variables for the modes selected. Secret values remain in protected files.

```bash
export E2E_GITHUB_COPILOT_SHARED_CREDENTIAL=e2e-copilot-shared
export E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/shared/access-token"
export E2E_GITHUB_COPILOT_USER_A_CREDENTIAL=e2e-copilot-users
export E2E_GITHUB_COPILOT_USER_A_ID='<first-litellm-user-id>'
export E2E_GITHUB_COPILOT_USER_B_CREDENTIAL=e2e-copilot-users
export E2E_GITHUB_COPILOT_USER_B_ID='<second-litellm-user-id>'
export E2E_GITHUB_COPILOT_APP_CREDENTIAL=e2e-copilot-app
export E2E_GITHUB_COPILOT_APP_ORGANIZATION='<organization-url-name>'
export E2E_GITHUB_COPILOT_APP_LOGIN='<organization-administrator-sign-in>'
export E2E_GITHUB_COPILOT_APP_ID='<app-or-client-id>'
export E2E_GITHUB_COPILOT_APP_INSTALLATION_ID='<installation-id>'
export E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/app.pem"
```

These are runner variables, separate from the proxy's secret environment. Run without shell tracing or pytest
`--showlocals`. Do not publish secret files, full environment dumps or provider authentication responses.
`E2E_GITHUB_COPILOT_HOST` also selects the runner's account, identity and installation-token API endpoints.
Enterprise catalog endpoints must belong to that same tenant. The named proxy credential must match the selected host.

Per-user token paths default to
`$XDG_DATA_HOME/litellm/copilot-e2e/tokens/<host>/user-a/access-token` and the corresponding `user-b` path.
When `XDG_DATA_HOME` is unset, it defaults to `~/.local/share`. Setup and tests use the same path derivation.
`E2E_GITHUB_COPILOT_USER_A_TOKEN_FILE` and `E2E_GITHUB_COPILOT_USER_B_TOKEN_FILE` remain optional overrides.
The shared token-file setting stays explicit because it must match the file mounted or read by the proxy.

## Running the tests

Use `run-e2e.sh` as the entry point after provisioning the proxy and account prerequisites.
It runs all Copilot API tests by default, including discovery, so the proxy stack must be exclusive to this run
and have `litellm_settings.check_provider_endpoint: true`. Running the script explicitly enables both suite opt-ins.
It checks proxy readiness and authenticated management access before starting pytest.
Account, connection and provider checks remain in the existing test fixtures.

```bash
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --env-file /secure/copilot-e2e.env
```

Without `--env-file`, the script loads `.env` beside `run-e2e.sh` when present, regardless of the working directory.
Otherwise it uses exported settings. An explicit file replaces the folder-local file, and either file overrides
exported values. An invalid explicit file fails rather than falling back.

Copy [.env.example](.env.example) to `.env`, set the proxy master key and fill in the selected profiles.
The repository ignores `.env`. Keep account tokens and PEM files outside the repository and reference their paths.
Do not commit the local environment file or share its contents.

```bash
umask 077
cp tests/e2e/llm_translation/github_copilot/.env.example tests/e2e/llm_translation/github_copilot/.env
chmod 600 tests/e2e/llm_translation/github_copilot/.env
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --collect-only
```

The script requires `LITELLM_PROXY_URL` and `LITELLM_MASTER_KEY` in the selected file or environment.
It disables all other dotenv loading and does not read development GitHub logins.
Prepare the checkout with the dependencies described in the
[e2e setup guide](../../CONTRIBUTING.md#running-the-tests-locally). The runner uses that existing environment
without installing or synchronizing packages. Normal test runs do not start services, approve device logins or run browser tests.
GitHub.com and enterprise runs still need separate matching proxy and credential settings.

### Browser authentication setup

Use `--auth` to provision the OAuth accounts in the selected environment file before running tests.
It uses the same `--env-file` selection and folder-local `.env` fallback as normal runs.
For per-user setup, provide `DATABASE_URL` and `LITELLM_SALT_KEY` matching the dedicated test proxy.
If that proxy encrypts with its master key instead of a separate salt key, use that encryption key as
`LITELLM_SALT_KEY` in the setup environment. These settings are needed only by setup, not routine API tests.

```bash
uv run --no-sync playwright install chromium
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --auth
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --env-file /secure/copilot-e2e.env --auth
```

Chromium also needs its system libraries. On Linux, install them with
`sudo -A .venv/bin/python -m playwright install-deps chromium` when they are missing.
Run setup in a visible graphical session.

Set `E2E_GITHUB_COPILOT_SHARED_LOGIN`, `E2E_GITHUB_COPILOT_USER_A_LOGIN` and
`E2E_GITHUB_COPILOT_USER_B_LOGIN` to the dedicated accounts' sign-in names.
Blank or undefined login fields skip that account during setup only. A selected account with missing credential,
per-user ID or required shared token-file settings fails instead of being skipped. With no selected accounts, setup fails.
Selected accounts need separate token-file paths, and per-user accounts need distinct LiteLLM user IDs.
App setup is selected separately by a nonempty `E2E_GITHUB_COPILOT_APP_ORGANIZATION`.
Blank organization settings skip it. App-only setup is supported without any OAuth login fields.
App setup requires a writable dotenv file so registration results persist. Pass `--env-file` or use the provider-local
`.env`. `APP_LOGIN` prefills browser sign-in when registration or installation approval is needed.

Setup verifies and reuses saved tokens first. It opens a browser only for a missing or expired connection,
with an isolated session for that account and a label identifying the mode, account and purpose.
It fills visible username and device-code fields as pages load. Complete passwords, MFA and final consent yourself.
Per-user accounts need at most one approval through the proxy. Setup then copies that exact saved token for direct
GitHub verification. Its GitHub identity must match the saved connection, and users A and B must use distinct accounts.
Temporary LiteLLM user keys are deleted when their setup step ends, including on failure.

Browser failures report the account, approval purpose, operation, exception type and last browser origin.
Known failures distinguish timeouts, closed pages, interrupted navigation and browser network errors.
Failed top-level redirects also report their HTTP status or safe network-error category instead of waiting for approval.
Installation advice is limited to missing browser executables or system libraries. Full URLs, raw browser
errors and call logs are withheld because they can contain authentication data.

Tokens are written atomically to the configured protected files. Browser cookies are not retained, and setup does
not capture screenshots, traces, passwords or device codes in logs. For shared mode, the proxy must read the same
token file from its dedicated token directory. Completing enterprise shared login does not fix the shared
inference-routing limitation described above.

The default OAuth client matches the provider. If the proxy explicitly sets `GITHUB_COPILOT_CLIENT_ID`, set
`E2E_GITHUB_COPILOT_OAUTH_CLIENT_ID` to the same value in the runner environment.
`--auth` is setup-only and cannot be combined with pytest arguments. Run the script again without it to execute
the API tests. Their selected cases still fail on missing account configuration rather than inheriting setup's skips.

### Selecting API tests

Pass pytest arguments after the optional env-file argument. Collection still performs the proxy preflight but makes
no provider calls. The runner defaults to `--reruns 0 -v` and preserves pytest's exit status.

```bash
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --env-file /secure/copilot-e2e.env \
  -k "shared and not unconnected" --junitxml=/tmp/copilot-shared.xml
bash tests/e2e/llm_translation/github_copilot/run-e2e.sh --env-file /secure/copilot-e2e.env --collect-only
```

The direct pytest commands below remain available when controlling the suite opt-ins yourself.

The runner fetches the real account-specific `/models` catalog and uses its advertised endpoint and streaming
capabilities to select models. It prefers newer numeric model IDs within a family rather than pinning names.
Discovery compares the entire expanded, scoped proxy namespace with that catalog and calls a listed model.
Two users may legitimately have the same catalog. The caller-isolation test alternates their requests and compares
each with its own catalog rather than requiring different lists.

Run inference independently of the discovery-stack flag.

```bash
E2E_GITHUB_COPILOT_LIVE=1 uv run --group e2e-dev pytest \
  tests/e2e/llm_translation/github_copilot/test_github_copilot_inference_e2e.py --reruns 0 -v
```

For discovery, start a separate proxy configuration with `litellm_settings.check_provider_endpoint: true` and
enable both switches. Do not run that proxy alongside other suites sharing its deployments or database.

```bash
E2E_GITHUB_COPILOT_LIVE=1 E2E_PROXY_WIDE_CONFIG_STACK=1 uv run --group e2e-dev pytest \
  tests/e2e/llm_translation/github_copilot/ --reruns 0 -v
```

Qualify GitHub.com and your enterprise separately, each against its matching proxy and secret configuration.
Load the matching runner variables before each command. These commands do not switch proxies or secret files.
Keep separate result files. A passing run on one host does not qualify the other.

```bash
E2E_GITHUB_COPILOT_HOST=github.com E2E_GITHUB_COPILOT_LIVE=1 E2E_PROXY_WIDE_CONFIG_STACK=1 \
  uv run --group e2e-dev pytest tests/e2e/llm_translation/github_copilot/ \
  --reruns 0 -v --junitxml=/tmp/copilot-github-com.xml
E2E_GITHUB_COPILOT_HOST='<enterprise>.ghe.com' E2E_GITHUB_COPILOT_LIVE=1 E2E_PROXY_WIDE_CONFIG_STACK=1 \
  uv run --group e2e-dev pytest tests/e2e/llm_translation/github_copilot/ \
  --reruns 0 -v --junitxml=/tmp/copilot-enterprise.xml
```

Select one mode or endpoint using normal pytest node selection. For example, append
`-k "github_app and chat_completions"` for App chat, or `-k "per_user_oauth and not unconnected"` for the connected
user cases. A selected endpoint with no compatible advertised model fails explicitly. Responses tests qualify the
public API, not native upstream Responses dispatch. Inference bypasses proxy response caching and checks the
resulting spend-log record without requiring a fixed price or positive cost.

The unconnected-user test additionally requires shared and user-A prerequisites. It first verifies shared inference,
then creates a disposable unconnected LiteLLM user and verifies authentication is rejected rather than falling back
to the mounted shared login. It does not disconnect either approved user.
Reconnect externally if a real token expires or is revoked.
