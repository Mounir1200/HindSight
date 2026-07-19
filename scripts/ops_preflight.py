from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_USAGE = 2
COMMAND_TIMEOUT_SECONDS = 2.0

ROOT = Path(__file__).resolve().parents[1]
LOCAL_FILES = (
    "pyproject.toml",
    "uv.lock",
    "src/hindsight/__init__.py",
)
DEPLOYMENT_FILES = (
    "Dockerfile",
    "Dockerfile.lambda",
    "deploy/apprunner-service.yaml",
    "deploy/tariff-ingestion.yaml",
    "deploy/cdr-ingestion.yaml",
)
MIGRATIONS = (
    "001_sources.sql",
    "002_assertions.sql",
    "003_assertion_indexes.sql",
    "004_decisions.sql",
    "005_remediation.sql",
    "006_agent_runs.sql",
    "007_memory_embeddings.sql",
    "008_investigation_contexts.sql",
    "009_investigation_context_snapshots.sql",
    "010_workspace_indexes.sql",
)
LIVE_COMMANDS = ("docker", "aws", "ccloud")
LIVE_ENV = (
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "BEDROCK_MODEL_ID",
    "COCKROACH_MCP_CLUSTER_ID",
    "COCKROACH_MCP_API_KEY",
)
LIVE_FLAGS = (
    "HINDSIGHT_DEMO_BEDROCK",
    "HINDSIGHT_DEMO_VECTOR",
    "HINDSIGHT_DEMO_MCP",
)
_ENABLED = frozenset({"1", "true", "yes", "on"})

CommandProbe = Callable[[Sequence[str], float], bool]
WhichProbe = Callable[[str], str | None]


def _command_available(command: Sequence[str], timeout: float) -> bool:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _paths_check(root: Path, paths: Sequence[str]) -> tuple[bool, str]:
    missing = [path for path in paths if not (root / path).is_file()]
    return (not missing, "available" if not missing else f"missing: {','.join(missing)}")


def _local_checks(
    root: Path,
    python_version: tuple[int, int, int],
    command_probe: CommandProbe,
) -> list[dict[str, Any]]:
    python_ok = (3, 12) <= python_version[:2] < (3, 15)
    python_detail = (
        f"{python_version[0]}.{python_version[1]}" if python_ok else "requires Python 3.12-3.14"
    )
    uv_ok = command_probe(("uv", "--version"), COMMAND_TIMEOUT_SECONDS)
    files_ok, files_detail = _paths_check(root, LOCAL_FILES)
    deployment_ok, deployment_detail = _paths_check(root, DEPLOYMENT_FILES)
    migrations_ok, migrations_detail = _paths_check(
        root, tuple(f"migrations/{name}" for name in MIGRATIONS)
    )
    tests_ok = (root / "tests").is_dir()
    config_ok = (root / ".env.example").is_file()
    return [
        _check("python", python_ok, python_detail),
        _check("uv", uv_ok, "available" if uv_ok else "missing or timed out"),
        _check("project_files", files_ok, files_detail),
        _check("deployment_artifacts", deployment_ok, deployment_detail),
        _check("migrations", migrations_ok, migrations_detail),
        _check(
            "tests_and_config",
            tests_ok and config_ok,
            "available" if tests_ok and config_ok else "missing",
        ),
    ]


def _live_checks(env: Mapping[str, str], which_probe: WhichProbe) -> list[dict[str, Any]]:
    checks = []
    for name in LIVE_COMMANDS:
        present = bool(which_probe(name))
        checks.append(_check(f"command:{name}", present, "available" if present else "missing"))
    for name in LIVE_ENV:
        present = bool(env.get(name, "").strip())
        checks.append(_check(f"env:{name}", present, "set" if present else "missing"))
    for name in LIVE_FLAGS:
        enabled = env.get(name, "").strip().lower() in _ENABLED
        checks.append(_check(f"flag:{name}", enabled, "enabled" if enabled else "disabled"))
    region_present = bool((env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip())
    checks.append(
        _check(
            "env:AWS_REGION_OR_AWS_DEFAULT_REGION",
            region_present,
            "set" if region_present else "missing",
        )
    )
    return checks


def run_preflight(
    mode: str = "local",
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    python_version: tuple[int, int, int] | None = None,
    command_probe: CommandProbe = _command_available,
    which_probe: WhichProbe = shutil.which,
) -> dict[str, Any]:
    if mode == "local":
        version = python_version or sys.version_info[:3]
        checks = _local_checks(root, version, command_probe)
    elif mode == "live":
        checks = _live_checks(os.environ if env is None else env, which_probe)
    else:
        raise ValueError(f"unsupported mode: {mode}")
    return {
        "schema_version": 1,
        "mode": mode,
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
    }


def _render_text(report: Mapping[str, Any]) -> str:
    state = "ready" if report["ok"] else "not ready"
    lines = [f"{report['mode']}: {state}"]
    lines.extend(
        f"[{'ok' if check['ok'] else 'fail'}] {check['name']}: {check['detail']}"
        for check in report["checks"]
    )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    python_version: tuple[int, int, int] | None = None,
    command_probe: CommandProbe = _command_available,
    which_probe: WhichProbe = shutil.which,
    stdout: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description="Check HindSight operational prerequisites")
    parser.add_argument("--mode", choices=("local", "live"), default="local")
    parser.add_argument("--json", action="store_true", help="emit compact machine-readable output")
    args = parser.parse_args(argv)

    report = run_preflight(
        args.mode,
        root=root,
        env=env,
        python_version=python_version,
        command_probe=command_probe,
        which_probe=which_probe,
    )
    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True), file=stdout)
    else:
        print(_render_text(report), file=stdout)
    return EXIT_READY if report["ok"] else EXIT_NOT_READY


if __name__ == "__main__":
    raise SystemExit(main())
