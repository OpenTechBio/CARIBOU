from __future__ import annotations

import stat
from pathlib import Path

from typer.testing import CliRunner

import caribou.cli.config_cli as config_cli
from caribou.core.control_access import (
    CONTROL_TOKEN_ENV,
    resolve_control_access_token,
)


def test_generated_control_token_is_persisted_and_reused(tmp_path: Path) -> None:
    env_file = tmp_path / "caribou" / ".env"
    environment: dict[str, str] = {}

    generated = resolve_control_access_token(
        create=True,
        env_file=env_file,
        environment=environment,
        token_factory=lambda: "persistent-control-token",
    )

    assert generated is not None
    assert generated.value == "persistent-control-token"
    assert generated.source == "generated"
    assert environment[CONTROL_TOKEN_ENV] == "persistent-control-token"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    reused = resolve_control_access_token(
        create=False,
        env_file=env_file,
        environment={},
    )
    assert reused is not None
    assert reused.value == generated.value
    assert reused.source == "caribou_env_file"


def test_environment_control_token_takes_precedence_without_persistence(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    resolved = resolve_control_access_token(
        create=True,
        env_file=env_file,
        environment={CONTROL_TOKEN_ENV: "operator-token"},
        token_factory=lambda: "must-not-be-used",
    )

    assert resolved is not None
    assert resolved.value == "operator-token"
    assert resolved.source == "environment"
    assert not env_file.exists()


def test_get_control_token_cli_prints_and_reuses_exact_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_cli, "ENV_FILE", env_file)
    monkeypatch.delenv(CONTROL_TOKEN_ENV, raising=False)
    runner = CliRunner()

    first = runner.invoke(config_cli.config_app, ["get-control-token", "--raw"])
    second = runner.invoke(config_cli.config_app, ["get-control-token", "--raw"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_token = first.output.strip()
    assert len(first_token) >= 32
    assert second.output.strip() == first_token
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
