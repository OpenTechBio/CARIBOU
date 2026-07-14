"""Discovery, schema, validation, and planning through a fresh CLI process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from caribou.domain.models import ExperimentSpec

from ..unit.test_domain_models import make_spec


COMMIT = "d" * 40


def run_cli(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment.update(
        {
            "CARIBOU_HOME": str(tmp_path / "home"),
            "CARIBOU_CODE_COMMIT": COMMIT,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(source_root), environment.get("PYTHONPATH", "")))
            ),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "caribou.cli.main", *arguments],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def response(result: subprocess.CompletedProcess[str]) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, (result.stdout, result.stderr)
    return json.loads(lines[0])


def write_spec(
    tmp_path: Path,
    *,
    output_root: str = "runs/smoke",
    repetitions: int = 5,
    smoke_seconds: float = 0.01,
) -> Path:
    spec = make_spec()
    condition = spec.conditions[0].model_copy(
        update={
            "parameters": {
                "caribou.execution_adapter": "lifecycle_smoke",
                "caribou.lifecycle_smoke_seconds": smoke_seconds,
            }
        }
    )
    execution = spec.execution.model_copy(update={"output_root": output_root})
    candidate = spec.model_copy(
        update={
            "conditions": [condition],
            "execution": execution,
            "repetitions": repetitions,
        }
    )
    spec = ExperimentSpec.model_validate_json(candidate.model_dump_json())
    path = tmp_path / "experiment.yaml"
    path.write_text(
        yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def test_capabilities_are_one_object_and_side_effect_free(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "capabilities", "--json")
    assert result.returncode == 0, result.stderr
    payload = response(result)
    assert payload["schema_version"] == "caribou.machine_response.v1"
    assert payload["command"] == "capabilities"
    assert payload["ok"] is True
    assert payload["caribou"]["commit"] == COMMIT
    assert payload["data"]["commands"]["experiment.submit"]["status"] == "implemented"
    assert (
        payload["data"]["execution_boundaries"]["local_lifecycle_smoke"]
        == "validated_control_plane_probe"
    )
    assert (
        payload["data"]["execution_boundaries"]["scripted_agent_path"]
        == "validated_actual_runner_with_test_boundaries"
    )
    assert payload["data"]["execution_boundaries"]["local_agent_analysis"] == (
        "implemented_not_validated_real_provider_container"
    )
    assert not (tmp_path / "home").exists()


def test_schema_discovery_returns_strict_experiment_spec(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "schema", "experiment", "--json")
    assert result.returncode == 0, result.stderr
    payload = response(result)
    assert payload["object"] == {
        "type": "schema",
        "id": "experiment",
        "state": "available",
    }
    schema = payload["data"]["schema"]
    assert schema["properties"]["schema_version"]["const"] == (
        "caribou.experiment_spec.v1"
    )
    assert schema["additionalProperties"] is False


def test_schema_discovery_exposes_strict_provider_call_receipt(tmp_path: Path) -> None:
    capabilities = run_cli(tmp_path, "capabilities", "--json")
    assert capabilities.returncode == 0, capabilities.stderr
    assert "provider-call-receipt" in response(capabilities)["data"]["schema_names"]

    result = run_cli(tmp_path, "schema", "provider-call-receipt", "--json")
    assert result.returncode == 0, result.stderr
    payload = response(result)
    assert payload["object"] == {
        "type": "schema",
        "id": "provider-call-receipt",
        "state": "available",
    }
    schema = payload["data"]["schema"]
    assert schema["properties"]["schema_version"]["const"] == (
        "caribou.provider_call_receipt.v1"
    )
    assert schema["properties"]["cost_basis"]["const"] == "unavailable"
    assert schema["properties"]["sdk_retries"]["const"] == 0
    assert schema["additionalProperties"] is False
    usage_reference = schema["properties"]["usage"]["$ref"]
    usage_name = usage_reference.rsplit("/", 1)[-1]
    assert schema["$defs"][usage_name]["additionalProperties"] is False


def test_validate_and_plan_never_prompt_or_mutate(tmp_path: Path) -> None:
    specification = write_spec(tmp_path)
    validated = run_cli(
        tmp_path, "experiment", "validate", str(specification), "--json"
    )
    assert validated.returncode == 0, validated.stderr
    validation = response(validated)
    assert validation["object"]["state"] == "validated"
    assert validation["data"]["spec_hash"].startswith("sha256:")

    planned = run_cli(tmp_path, "experiment", "plan", str(specification), "--json")
    assert planned.returncode == 0, planned.stderr
    plan = response(planned)["data"]
    assert plan["schema_version"] == "caribou.experiment_plan.v1"
    assert plan["run_count"] == 5
    assert plan["plan_hash"].startswith("sha256:")
    assert all(item["adapter"] == "lifecycle_smoke" for item in plan["runs"])
    assert not (tmp_path / "home").exists()


def test_machine_validation_failure_is_structured(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"schema_version":"caribou.experiment_spec.v1","extra":true}')
    result = run_cli(tmp_path, "experiment", "validate", str(path), "--json")
    assert result.returncode == 10
    payload = response(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "SPEC_INVALID"
    assert "Traceback" not in result.stderr


def test_unsafe_output_root_is_rejected_by_control_plane(tmp_path: Path) -> None:
    specification = write_spec(tmp_path, output_root="../escape")
    result = run_cli(tmp_path, "experiment", "plan", str(specification), "--json")
    assert result.returncode == 10
    assert response(result)["error"]["code"] == "OUTPUT_ROOT_UNSAFE"


def test_machine_command_without_json_never_prompts(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "capabilities")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "requires --json" in result.stderr


def test_unhashable_adapter_value_fails_as_typed_validation(tmp_path: Path) -> None:
    specification = write_spec(tmp_path, repetitions=1)
    payload = yaml.safe_load(specification.read_text(encoding="utf-8"))
    payload["conditions"][0]["parameters"]["caribou.execution_adapter"] = [
        "lifecycle_smoke"
    ]
    specification.write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "experiment",
        "submit",
        str(specification),
        "--idempotency-key",
        "invalid-adapter-shape",
        "--json",
    )

    assert result.returncode == 10
    assert response(result)["error"]["code"] == "ADAPTER_UNSUPPORTED"
