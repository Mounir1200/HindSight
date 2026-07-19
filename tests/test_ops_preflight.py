from __future__ import annotations

import importlib.util
import json
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ops_preflight.py"
SPEC = importlib.util.spec_from_file_location("ops_preflight", SCRIPT)
assert SPEC and SPEC.loader
ops_preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops_preflight)


def _local_tree(root: Path) -> None:
    for relative in (
        *ops_preflight.LOCAL_FILES,
        *ops_preflight.DEPLOYMENT_FILES,
        ".env.example",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / "tests").mkdir()
    for name in ops_preflight.MIGRATIONS:
        path = root / "migrations" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_local_preflight_uses_a_bounded_uv_probe(tmp_path: Path) -> None:
    _local_tree(tmp_path)
    calls: list[tuple[tuple[str, ...], float]] = []

    def probe(command: tuple[str, ...], timeout: float) -> bool:
        calls.append((command, timeout))
        return True

    report = ops_preflight.run_preflight(
        root=tmp_path,
        python_version=(3, 12, 4),
        command_probe=probe,
    )

    assert report["ok"] is True
    assert calls == [(("uv", "--version"), ops_preflight.COMMAND_TIMEOUT_SECONDS)]


def test_local_preflight_fails_without_required_artifacts(tmp_path: Path) -> None:
    report = ops_preflight.run_preflight(
        root=tmp_path,
        python_version=(3, 11, 9),
        command_probe=lambda _command, _timeout: False,
    )

    assert report["ok"] is False
    assert {check["name"] for check in report["checks"] if not check["ok"]} == {
        "python",
        "uv",
        "project_files",
        "deployment_artifacts",
        "migrations",
        "tests_and_config",
    }


def test_live_preflight_only_checks_presence_and_never_exposes_values() -> None:
    secret = "must-not-appear"
    env = {name: secret for name in ops_preflight.LIVE_ENV}
    env.update({name: "true" for name in ops_preflight.LIVE_FLAGS})
    env["AWS_DEFAULT_REGION"] = "eu-central-1"
    looked_up: list[str] = []

    def which(name: str) -> str:
        looked_up.append(name)
        return f"/tools/{name}"

    report = ops_preflight.run_preflight("live", env=env, which_probe=which)
    payload = json.dumps(report)

    assert report["ok"] is True
    assert looked_up == list(ops_preflight.LIVE_COMMANDS)
    assert secret not in payload
    assert "eu-central-1" not in payload
    assert all(check["ok"] for check in report["checks"] if check["name"].startswith("flag:"))


def test_json_output_and_exit_code_are_stable(tmp_path: Path) -> None:
    output = StringIO()

    exit_code = ops_preflight.main(
        ["--json"],
        root=tmp_path,
        python_version=(3, 12, 0),
        command_probe=lambda _command, _timeout: False,
        stdout=output,
    )
    payload = output.getvalue()

    assert exit_code == ops_preflight.EXIT_NOT_READY
    assert payload == payload.strip() + "\n"
    decoded = json.loads(payload)
    assert payload == json.dumps(decoded, separators=(",", ":"), sort_keys=True) + "\n"
    assert decoded["schema_version"] == 1
