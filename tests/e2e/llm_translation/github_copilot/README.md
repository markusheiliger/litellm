# GitHub Copilot live tests

The Copilot suite is opt-in because it needs dedicated entitled accounts or an enabled GitHub App installation.
The default pipeline deselects every Copilot case without checking those credentials. Set `E2E_GITHUB_COPILOT_LIVE=1`
only in a provisioned environment. Once enabled, missing secrets, connections, models and provider errors fail the run.
The existing opt-in mechanism treats any nonempty value as enabled, including `0`. Unset a switch to disable it.

## Host and identity selection

Select one host per run with `E2E_GITHUB_COPILOT_HOST`. It defaults to `github.com` and also accepts a bare
`<enterprise>.ghe.com` hostname. URLs, literal wildcards, nested enterprise subdomains and custom GitHub
Enterprise Server domains are not supported.

```bash
export E2E_GITHUB_COPILOT_HOST=github.com
```

For an enterprise run, set it to your enterprise hostname instead. Use a matching proxy configuration and a
separate set of dedicated accounts, approved connections, App identifiers and secret files for that host.
The runner does not select identities from `gh`, your editor, `GH_HOST`, `GITHUB_TOKEN` or the development
proxy's `GITHUB_COPILOT_HOST`. Missing test credentials fail rather than falling back to those identities.
Your development login does not need to change.

## Proxy setup

Use an externally provisioned proxy built from the revision under test. The tests do not start a proxy, register a
GitHub App or approve device logins. Use the normal Postgres, shared Redis, management and spend-log configuration
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

Set `GITHUB_COPILOT_HOST` explicitly for the shared-auth test proxy process. Its value must match
`E2E_GITHUB_COPILOT_HOST` and the named credentials below. Give the process a dedicated
`GITHUB_COPILOT_TOKEN_DIR`, never your development token directory. Per-user and App authentication select
their host from the named credential rather than the shared-auth process setting.

For a local proxy, use a copy of the development config with a dedicated database and credentials. Point both
`router_settings.redis_host` / `redis_port` and `general_settings.coordination_redis.host` / `port` at the same
dedicated Redis instance, with credentials when required. Pass the test environment explicitly when starting it.

```bash
env -u GITHUB_COPILOT_API_BASE -u GITHUB_COPILOT_DEVICE_CODE_URL -u GITHUB_COPILOT_ACCESS_TOKEN_URL \
  -u GITHUB_COPILOT_CLIENT_ID \
  GITHUB_COPILOT_HOST="$E2E_GITHUB_COPILOT_HOST" \
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
env -u GITHUB_COPILOT_DEVICE_CODE_URL -u GITHUB_COPILOT_ACCESS_TOKEN_URL -u GITHUB_COPILOT_CLIENT_ID \
  GITHUB_COPILOT_HOST="$E2E_GITHUB_COPILOT_HOST" \
  GITHUB_COPILOT_TOKEN_DIR="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/shared" \
  GITHUB_COPILOT_ACCESS_TOKEN_FILE=access-token \
  python -c "from litellm.llms.github_copilot.authenticator import Authenticator; Authenticator().get_access_token()"
```

Mount that login's token directory into the proxy and set its `GITHUB_COPILOT_TOKEN_DIR`. Make the same dedicated
`access-token` available to the runner through a protected file. Do not copy tokens into committed config.
The legacy default token location remains unchanged for GitHub.com. Enterprise defaults are host-specific, but
these tests should always use explicitly selected directories to avoid reusing any development login.

For per-user mode, create two dedicated LiteLLM users with distinct entitled GitHub accounts. As each user, connect
`e2e-copilot-users` through the dashboard, or call `POST /credentials/e2e-copilot-users/user_connection/start`, approve
the returned user code at its verification URI, then call
`POST /credentials/e2e-copilot-users/user_connection/poll` with `{"flow_handle": "<returned handle>"}`.
Respect the returned polling interval and wait for `connected`. Do not send the flow handle or device code to logs.
`GET /credentials/user_connections` reads the caller's saved connection.

Provide an OAuth token for the same dedicated GitHub account and OAuth app to the runner in a protected file.
The start/poll API deliberately does not return the saved token. Obtain the runner token through a separate
human-approved device login using the command above, a separate `GITHUB_COPILOT_TOKEN_DIR` for each user and the
same explicitly selected OAuth client as the proxy. The commands above clear a development
`GITHUB_COPILOT_CLIENT_ID` and use the provider default. For a custom test client, remove that unset option and add
the same explicit `GITHUB_COPILOT_CLIENT_ID` assignment alongside the host in both commands.
This independently authorizes the same account without reading
the proxy database. The suite compares its GitHub identity with the saved connection. Do not substitute a personal
access token or a token from another OAuth app. Supply each user's LiteLLM user ID, not their GitHub login.
Routine tests preserve these users and connections.

For App mode, register a dedicated GitHub App with Copilot Requests read permission and install it on an organization
enabled for App Copilot requests. Installation alone does not grant Copilot entitlement. Configure its App/client ID,
installation ID and RSA private-key secret on the proxy. The runner needs the same identifiers and a protected PEM
file. It uses PyJWT and the shared HTTP transport to mint its own installation token, independently of LiteLLM's
provider implementation. App runs need no human interaction after provisioning.

## Runner configuration

Set the following runner variables for the modes selected. Secret values remain in protected files.

```bash
export E2E_GITHUB_COPILOT_SHARED_CREDENTIAL=e2e-copilot-shared
export E2E_GITHUB_COPILOT_SHARED_TOKEN_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/shared/access-token"
export E2E_GITHUB_COPILOT_USER_A_CREDENTIAL=e2e-copilot-users
export E2E_GITHUB_COPILOT_USER_A_ID='<first-litellm-user-id>'
export E2E_GITHUB_COPILOT_USER_A_TOKEN_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/user-a/access-token"
export E2E_GITHUB_COPILOT_USER_B_CREDENTIAL=e2e-copilot-users
export E2E_GITHUB_COPILOT_USER_B_ID='<second-litellm-user-id>'
export E2E_GITHUB_COPILOT_USER_B_TOKEN_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/user-b/access-token"
export E2E_GITHUB_COPILOT_APP_CREDENTIAL=e2e-copilot-app
export E2E_GITHUB_COPILOT_APP_ID='<app-or-client-id>'
export E2E_GITHUB_COPILOT_APP_INSTALLATION_ID='<installation-id>'
export E2E_GITHUB_COPILOT_APP_PRIVATE_KEY_FILE="/secure/copilot-$E2E_GITHUB_COPILOT_HOST/app.pem"
```

These are runner variables, separate from the proxy's secret environment. Run without shell tracing or pytest
`--showlocals`. Do not publish secret files, full environment dumps or provider authentication responses.
`E2E_GITHUB_COPILOT_HOST` also selects the runner's account, identity and installation-token API endpoints.
Enterprise catalog endpoints must belong to that same tenant. The named proxy credential must match the selected host.

## Running the tests

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
