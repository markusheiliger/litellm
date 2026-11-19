from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

import pytest
from e2e_http import URL, AuthHeaders, NoBody, ProbeResult, probe
from pydantic import BaseModel, ValidationError


class Probe(Protocol):
    def __call__(self, url: URL, *, headers: BaseModel, params: BaseModel, timeout: float) -> ProbeResult: ...


def preflight(environ: Mapping[str, str], *, issue: Probe = probe) -> str | None:
    missing: Final = tuple(
        name for name in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY") if not environ.get(name, "").strip()
    )
    if missing:
        return f"Set {' and '.join(missing)} explicitly for the dedicated test proxy."

    base_url: Final = environ["LITELLM_PROXY_URL"].rstrip("/")
    control_url: Final = environ.get("LITELLM_CONTROL_PLANE_URL", base_url).rstrip("/")
    headers: Final = AuthHeaders(authorization=f"Bearer {environ['LITELLM_MASTER_KEY']}")
    ready: Final = issue(URL(f"{base_url}/health/readiness"), headers=headers, params=NoBody(), timeout=10)
    if ready.status_code != 200:
        return f"Proxy readiness check failed with status {ready.status_code}. Check the proxy and its database."

    credentials: Final = issue(URL(f"{control_url}/credentials"), headers=headers, params=NoBody(), timeout=10)
    if credentials.status_code != 200:
        return (
            f"Management authentication check failed with status {credentials.status_code}. "
            "Check the master key and control-plane address."
        )
    return None


def main(args: Sequence[str]) -> int:
    if "--auth" in args:
        if tuple(args) != ("--auth",):
            sys.stderr.write("--auth is setup-only and cannot be combined with pytest arguments.\n")
            return 2
        from llm_translation.github_copilot.auth_setup import run

        try:
            return run(os.environ)
        except pytest.fail.Exception as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        except (OSError, ValidationError):
            sys.stderr.write("Auth setup could not read or write its files, or received an invalid response.\n")
            return 2
    problem: Final = preflight(os.environ)
    if problem is not None:
        sys.stderr.write(f"{problem}\n")
        return 2
    return pytest.main([str(Path(__file__).resolve().parent), "--reruns", "0", "-v", *args])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
