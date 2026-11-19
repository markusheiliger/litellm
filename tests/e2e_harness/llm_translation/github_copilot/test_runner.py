from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from queue import SimpleQueue
from typing import Final

import pytest
from e2e_http import URL, AuthHeaders, ProbeResult
from llm_translation.github_copilot.runner import main, preflight
from pydantic import BaseModel, TypeAdapter

_ROOT: Final = Path(__file__).resolve().parents[4]
_RUNNER: Final = _ROOT / "tests/e2e/llm_translation/github_copilot/run-e2e.sh"


@pytest.mark.parametrize(("ready", "management"), ((200, 200), (503, 200), (302, 200), (-1, 200), (200, 401)))
def test_preflight_requires_ready_proxy_and_authenticated_management(ready: int, management: int) -> None:
    calls: Final = SimpleQueue[tuple[str, str | None]]()

    def issue(url: URL, *, headers: BaseModel, params: BaseModel, timeout: float) -> ProbeResult:
        calls.put((str(url), AuthHeaders.model_validate(headers).authorization))
        return ProbeResult(
            status_code=ready if str(url).endswith("/health/readiness") else management,
            body="synthetic-secret-must-not-be-printed",
        )

    problem: Final = preflight(
        {
            "LITELLM_PROXY_URL": "http://data-plane/",
            "LITELLM_CONTROL_PLANE_URL": "http://control-plane/",
            "LITELLM_MASTER_KEY": "synthetic-master-key",
        },
        issue=issue,
    )
    assert (problem is None) == (ready == management == 200)
    assert "synthetic" not in (problem or "")
    assert calls.get_nowait() == ("http://data-plane/health/readiness", "Bearer synthetic-master-key")
    if ready == 200:
        assert calls.get_nowait() == ("http://control-plane/credentials", "Bearer synthetic-master-key")
    assert calls.empty()


@pytest.mark.parametrize("missing", ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY"))
def test_preflight_rejects_missing_settings_before_any_request(missing: str) -> None:
    def issue(url: URL, *, headers: BaseModel, params: BaseModel, timeout: float) -> ProbeResult:
        pytest.fail("A missing setting must fail before contacting a proxy.")

    problem: Final = preflight(
        {
            name: value
            for name, value in {
                "LITELLM_PROXY_URL": "http://test-proxy",
                "LITELLM_MASTER_KEY": "synthetic-key",
            }.items()
            if name != missing
        },
        issue=issue,
    )
    assert problem is not None and missing in problem


@pytest.mark.parametrize(
    ("with_env_file", "with_fallback", "expected_proxy"),
    (
        (False, False, "http://exported-proxy"),
        (False, True, "http://folder-proxy"),
        (True, False, "http://dedicated-proxy"),
        (True, True, "http://dedicated-proxy"),
    ),
)
@pytest.mark.parametrize("auth_position", ("none", "before", "after"))
def test_shell_runner_forwards_options_and_preserves_failure_status(
    tmp_path: Path, with_env_file: bool, with_fallback: bool, expected_proxy: str, auth_position: str
) -> None:
    capture: Final = tmp_path / "capture.json"
    suite: Final = tmp_path / "checkout/tests/e2e/llm_translation/github_copilot"
    suite.mkdir(parents=True)
    script: Final = suite / "run-e2e.sh"
    script.write_text(_RUNNER.read_text())
    (suite / "runner.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAPTURE_FILE']).write_text(json.dumps([\n"
        "    sys.argv[1:],\n"
        "    {name: os.environ.get(name, '') for name in (\n"
        "        'LITELLM_PROXY_URL', 'PYTHON_DOTENV_DISABLED',\n"
        "        'E2E_GITHUB_COPILOT_LIVE', 'E2E_PROXY_WIDE_CONFIG_STACK',\n"
        "        'E2E_GITHUB_COPILOT_ENV_FILE',\n"
        "    )},\n"
        "]))\n"
        "raise SystemExit(17)\n"
    )
    env_file: Final = tmp_path / "private environment.env"
    env_file.write_text(
        "LITELLM_PROXY_URL=http://dedicated-proxy\n"
        "PYTHON_DOTENV_DISABLED=0\n"
        "E2E_GITHUB_COPILOT_LIVE=0\n"
        "E2E_PROXY_WIDE_CONFIG_STACK=0\n"
        "E2E_GITHUB_COPILOT_ENV_FILE=/unselected.env\n"
    )
    if with_fallback:
        (suite / ".env").write_text("LITELLM_PROXY_URL=http://folder-proxy\n")
    unexpected_env: Final = tmp_path / "unexpected.env"
    unexpected_env.write_text("LITELLM_PROXY_URL=http://unexpected-proxy\n")
    result: Final = subprocess.run(
        [
            "bash",
            str(script),
            *(("--auth",) if auth_position == "before" else ()),
            *(("--env-file", env_file.name) if with_env_file else ()),
            *(("--auth",) if auth_position == "after" else ()),
            "--",
            *(
                ("-k", "shared and not unconnected", "--junitxml=result with spaces.xml")
                if auth_position == "none"
                else ()
            ),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "LITELLM_PROXY_URL": "http://exported-proxy",
            "UV_ENV_FILE": str(unexpected_env),
            "E2E_GITHUB_COPILOT_ENV_FILE": "/unselected-export.env",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 17, (result.stdout, result.stderr)
    arguments, environment = TypeAdapter(tuple[tuple[str, ...], dict[str, str]]).validate_json(capture.read_text())
    assert environment == {
        "LITELLM_PROXY_URL": expected_proxy,
        "PYTHON_DOTENV_DISABLED": "1",
        "E2E_GITHUB_COPILOT_LIVE": "1",
        "E2E_PROXY_WIDE_CONFIG_STACK": "1",
        "E2E_GITHUB_COPILOT_ENV_FILE": (
            str(env_file) if with_env_file else str(suite / ".env") if with_fallback else ""
        ),
    }
    assert arguments == (
        ("-k", "shared and not unconnected", "--junitxml=result with spaces.xml")
        if auth_position == "none"
        else ("--auth",)
    )


@pytest.mark.parametrize("explicit", (False, True))
def test_invalid_environment_file_fails_instead_of_using_another_source(tmp_path: Path, explicit: bool) -> None:
    suite: Final = tmp_path / "checkout/tests/e2e/llm_translation/github_copilot"
    suite.mkdir(parents=True)
    script: Final = suite / "run-e2e.sh"
    script.write_text(_RUNNER.read_text())
    if explicit:
        (suite / ".env").write_text("LITELLM_PROXY_URL=http://folder-proxy\n")
    else:
        (suite / ".env").mkdir()
    result: Final = subprocess.run(
        ["bash", str(script), *(("--env-file", str(tmp_path / "missing.env")) if explicit else ())],
        cwd=tmp_path,
        env={**os.environ, "LITELLM_PROXY_URL": "http://exported-proxy"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "dotenv file" in result.stderr


def test_auth_setup_rejects_test_arguments_without_running_tests(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(("--auth", "--collect-only")) == 2
    assert "setup-only" in capsys.readouterr().err
