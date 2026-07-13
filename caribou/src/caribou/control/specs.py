"""Strict experiment-spec loading, preflight validation, and local planning."""

from __future__ import annotations

import json
import hashlib
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from caribou.domain.enums import ExecutorKind
from caribou.domain.models import ExperimentSpec
from caribou.domain.serialization import model_hash

from .api import ControlError, ExitCode


LOCAL_LIFECYCLE_ADAPTER = "lifecycle_smoke"
ADAPTER_PARAMETER = "caribou.execution_adapter"
SMOKE_SECONDS_PARAMETER = "caribou.lifecycle_smoke_seconds"


def load_experiment_spec(path: Path) -> ExperimentSpec:
    """Load JSON or YAML and enforce the frozen Pydantic contract."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ControlError(
            "SPEC_NOT_FOUND",
            f"experiment specification is not a readable file: {resolved}",
            exit_code=ExitCode.not_found,
            details={"path": str(resolved)},
        )
    try:
        text = resolved.read_text(encoding="utf-8")
        if resolved.suffix.lower() == ".json":
            value = json.loads(text)
        else:
            value = yaml.safe_load(text)
        return ExperimentSpec.model_validate_json(json.dumps(value))
    except (
        OSError,
        TypeError,
        json.JSONDecodeError,
        yaml.YAMLError,
        ValidationError,
    ) as exc:
        raise ControlError(
            "SPEC_INVALID",
            "experiment specification failed strict validation",
            exit_code=ExitCode.validation,
            details={"path": str(resolved), "reason": str(exc)},
        ) from exc


def _validate_output_root(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
        raise ControlError(
            "OUTPUT_ROOT_UNSAFE",
            "execution.output_root must be a nontraversing relative path",
            exit_code=ExitCode.validation,
            details={"output_root": value},
        )


def _smoke_seconds(parameters: dict[str, Any]) -> float:
    raw = parameters.get(SMOKE_SECONDS_PARAMETER, 0.05)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ControlError(
            "ADAPTER_PARAMETER_INVALID",
            f"{SMOKE_SECONDS_PARAMETER} must be a finite number",
            exit_code=ExitCode.validation,
        )
    seconds = float(raw)
    if seconds < 0 or seconds > 3600:
        raise ControlError(
            "ADAPTER_PARAMETER_INVALID",
            f"{SMOKE_SECONDS_PARAMETER} must be between 0 and 3600",
            exit_code=ExitCode.validation,
        )
    return seconds


def validate_control_spec(
    spec: ExperimentSpec, *, require_local_adapter: bool = False
) -> list[dict[str, Any]]:
    """Apply control-plane constraints not encoded as scientific fields."""

    _validate_output_root(spec.execution.output_root)
    checks: list[dict[str, Any]] = [
        {"check": "domain_schema", "status": "passed"},
        {"check": "output_path_policy", "status": "passed"},
    ]
    if require_local_adapter and spec.execution.executor != ExecutorKind.local:
        raise ControlError(
            "EXECUTOR_UNSUPPORTED",
            "M2 local submission requires execution.executor=local",
            exit_code=ExitCode.validation,
            details={"executor": spec.execution.executor.value},
        )
    adapter_checks = []
    for condition in spec.conditions:
        adapter = condition.parameters.get(ADAPTER_PARAMETER)
        supported = adapter == LOCAL_LIFECYCLE_ADAPTER
        if require_local_adapter and not supported:
            raise ControlError(
                "ADAPTER_UNSUPPORTED",
                "M2 local execution supports only the explicit lifecycle_smoke adapter",
                exit_code=ExitCode.validation,
                details={
                    "condition_id": condition.condition_id,
                    "adapter": adapter,
                    "supported": [LOCAL_LIFECYCLE_ADAPTER],
                },
            )
        if supported:
            _smoke_seconds(dict(condition.parameters))
        adapter_checks.append(
            {
                "condition_id": condition.condition_id,
                "adapter": adapter,
                "supported": supported,
            }
        )
    checks.append(
        {
            "check": "execution_adapter",
            "status": "passed"
            if all(item["supported"] for item in adapter_checks)
            else "informational",
            "conditions": adapter_checks,
        }
    )
    return checks


def build_local_plan(spec: ExperimentSpec) -> dict[str, Any]:
    """Return a deterministic, non-mutating resource and attempt plan."""

    checks = validate_control_spec(spec, require_local_adapter=False)
    run_count = len(spec.conditions) * spec.repetitions
    resources = spec.execution.resources
    wall_seconds = Decimal(resources.wall_seconds) * run_count
    cpu_seconds = Decimal(resources.cpu_cores) * wall_seconds
    gpu_seconds = Decimal(resources.gpu_count) * wall_seconds
    runs = []
    for condition in spec.conditions:
        for replicate_index in range(spec.repetitions):
            runs.append(
                {
                    "condition_id": condition.condition_id,
                    "replicate_index": replicate_index,
                    "attempt_index": 1,
                    "adapter": condition.parameters.get(ADAPTER_PARAMETER),
                }
            )
    plan = {
        "schema_version": "caribou.experiment_plan.v1",
        "spec_id": spec.spec_id,
        "spec_hash": model_hash(spec),
        "executor": spec.execution.executor.value,
        "run_count": run_count,
        "estimated_maximums": {
            "wall_seconds": int(wall_seconds),
            "cpu_seconds": int(cpu_seconds),
            "gpu_seconds": int(gpu_seconds),
            "memory_bytes_per_run": resources.memory_bytes,
            "storage_bytes_per_run": resources.storage_bytes,
        },
        "checks": checks,
        "runs": runs,
    }
    canonical = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return {
        **plan,
        "plan_hash": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    }
