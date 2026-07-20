"""Strict experiment-spec loading, preflight validation, and local planning."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from caribou.domain.enums import (
    ExecutorKind,
    FailureCategory,
    MemoryStrategy,
    SandboxKind,
    TopologyKind,
)
from caribou.domain.models import ExperimentSpec
from caribou.domain.serialization import model_hash

from .api import ControlError, ExitCode


LOCAL_LIFECYCLE_ADAPTER = "lifecycle_smoke"
AGENT_PATH_SMOKE_ADAPTER = "agent_path_smoke"
CARIBOU_AGENT_ADAPTER = "caribou_agent"
LOCAL_ADAPTERS = frozenset(
    {LOCAL_LIFECYCLE_ADAPTER, AGENT_PATH_SMOKE_ADAPTER, CARIBOU_AGENT_ADAPTER}
)
ADAPTER_PARAMETER = "caribou.execution_adapter"
SMOKE_SECONDS_PARAMETER = "caribou.lifecycle_smoke_seconds"
AGENT_SMOKE_DELAY_PARAMETER = "caribou.agent_smoke_delay_seconds"
MODEL_MAX_OUTPUT_TOKENS_PARAMETER = "max_output_tokens"
MODEL_THINKING_PARAMETER = "thinking"
MODEL_REASONING_EFFORT_PARAMETER = "reasoning_effort"
_SUPPORTED_AGENT_MODEL_PARAMETERS = frozenset(
    {
        MODEL_MAX_OUTPUT_TOKENS_PARAMETER,
        MODEL_THINKING_PARAMETER,
        MODEL_REASONING_EFFORT_PARAMETER,
    }
)
AGENT_RETRYABLE_FAILURES = frozenset(
    {FailureCategory.provider, FailureCategory.timeout}
)


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


def _model_max_output_tokens(parameters: dict[str, Any]) -> int | None:
    unsupported = sorted(set(parameters) - _SUPPORTED_AGENT_MODEL_PARAMETERS)
    if unsupported:
        raise ControlError(
            "AGENT_MODEL_PARAMETERS_UNSUPPORTED",
            "the real agent workload received unsupported model parameters",
            exit_code=ExitCode.validation,
            details={"parameters": unsupported},
        )
    raw = parameters.get(MODEL_MAX_OUTPUT_TOKENS_PARAMETER)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ControlError(
            "AGENT_MODEL_PARAMETER_INVALID",
            "max_output_tokens must be a positive integer",
            exit_code=ExitCode.validation,
            details={"parameter": MODEL_MAX_OUTPUT_TOKENS_PARAMETER},
        )
    return raw


def _model_deepseek_options(
    parameters: dict[str, Any],
) -> tuple[bool | None, str | None]:
    """Validate and return frozen DeepSeek thinking controls."""

    thinking = parameters.get(MODEL_THINKING_PARAMETER)
    if thinking is not None and not isinstance(thinking, bool):
        raise ControlError(
            "AGENT_MODEL_PARAMETER_INVALID",
            "thinking must be a boolean",
            exit_code=ExitCode.validation,
            details={"parameter": MODEL_THINKING_PARAMETER},
        )
    reasoning_effort = parameters.get(MODEL_REASONING_EFFORT_PARAMETER)
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or reasoning_effort not in {"high", "max"}
    ):
        raise ControlError(
            "AGENT_MODEL_PARAMETER_INVALID",
            "reasoning_effort must be high or max",
            exit_code=ExitCode.validation,
            details={"parameter": MODEL_REASONING_EFFORT_PARAMETER},
        )
    if reasoning_effort is not None and thinking is not True:
        raise ControlError(
            "AGENT_MODEL_PARAMETER_INVALID",
            "reasoning_effort requires thinking=true",
            exit_code=ExitCode.validation,
            details={"parameter": MODEL_REASONING_EFFORT_PARAMETER},
        )
    return thinking, reasoning_effort


def _validate_agent_adapter(
    spec: ExperimentSpec, condition_index: int, adapter: str
) -> None:
    condition = spec.conditions[condition_index]
    if len(spec.inputs) != 1:
        raise ControlError(
            "AGENT_INPUTS_UNSUPPORTED",
            "the initial agent workload requires exactly one frozen input",
            exit_code=ExitCode.validation,
            details={"input_count": len(spec.inputs)},
        )
    if adapter == AGENT_PATH_SMOKE_ADAPTER:
        if condition.model.provider != "scripted" or (
            spec.execution.container.sandbox != SandboxKind.offline
        ):
            raise ControlError(
                "AGENT_SMOKE_BOUNDARY_INVALID",
                "agent_path_smoke requires provider=scripted and sandbox=offline",
                exit_code=ExitCode.validation,
                details={"condition_id": condition.condition_id},
            )
        if condition.blueprint.topology != TopologyKind.multi_agent:
            raise ControlError(
                "AGENT_SMOKE_TOPOLOGY_INVALID",
                "agent_path_smoke requires a multi-agent blueprint to exercise delegation",
                exit_code=ExitCode.validation,
                details={"condition_id": condition.condition_id},
            )
        if spec.stop_rules.maximum_turns < 3:
            raise ControlError(
                "AGENT_SMOKE_TURNS_INVALID",
                "agent_path_smoke requires at least three turns",
                exit_code=ExitCode.validation,
                details={"maximum_turns": spec.stop_rules.maximum_turns},
            )
        _agent_smoke_delay(dict(condition.parameters))
        return
    if spec.code.dirty:
        raise ControlError(
            "AGENT_CODE_DIRTY",
            "agent workloads require a frozen clean code commit",
            exit_code=ExitCode.validation,
        )
    if spec.execution.container.sandbox not in {
        SandboxKind.apptainer,
        SandboxKind.singularity,
    }:
        raise ControlError(
            "AGENT_SANDBOX_UNSUPPORTED",
            "the initial real agent workload requires Apptainer or Singularity",
            exit_code=ExitCode.validation,
            details={"sandbox": spec.execution.container.sandbox.value},
        )
    if condition.model.provider not in {"openai", "deepseek"}:
        raise ControlError(
            "AGENT_PROVIDER_UNSUPPORTED",
            "the initial real agent workload requires an explicitly supported provider",
            exit_code=ExitCode.validation,
            details={
                "provider": condition.model.provider,
                "supported": ["deepseek", "openai"],
            },
        )
    if (
        condition.model.artifact is not None
        or condition.model.quantization is not None
        or condition.model.context_length is not None
    ):
        raise ControlError(
            "AGENT_MODEL_FIELDS_UNSUPPORTED",
            "the initial external-model workload does not bind model artifacts, "
            "quantization, or context-length declarations",
            exit_code=ExitCode.validation,
        )
    model_parameters = dict(condition.model.parameters)
    _model_max_output_tokens(model_parameters)
    thinking, reasoning_effort = _model_deepseek_options(model_parameters)
    if condition.model.provider != "deepseek" and (
        thinking is not None or reasoning_effort is not None
    ):
        raise ControlError(
            "AGENT_MODEL_PARAMETERS_UNSUPPORTED",
            "thinking controls are supported only by the DeepSeek provider",
            exit_code=ExitCode.validation,
            details={
                "parameters": sorted(
                    set(model_parameters)
                    & {MODEL_THINKING_PARAMETER, MODEL_REASONING_EFFORT_PARAMETER}
                )
            },
        )
    if condition.memory.strategy != MemoryStrategy.full:
        raise ControlError(
            "AGENT_MEMORY_UNSUPPORTED",
            "the initial real agent workload supports only full-history memory",
            exit_code=ExitCode.validation,
            details={"strategy": condition.memory.strategy.value},
        )
    if condition.blueprint.tools:
        raise ControlError(
            "AGENT_TOOLS_UNSUPPORTED",
            "the initial real agent workload does not bind declared external tools",
            exit_code=ExitCode.validation,
            details={"tool_count": len(condition.blueprint.tools)},
        )
    if spec.execution.container.runtime_version is not None:
        raise ControlError(
            "AGENT_RUNTIME_VERSION_UNSUPPORTED",
            "the initial real agent workload does not validate a declared runtime version",
            exit_code=ExitCode.validation,
            details={"runtime_version": spec.execution.container.runtime_version},
        )
    if spec.execution.container.force_refresh:
        raise ControlError(
            "AGENT_CONTAINER_REFRESH_UNSUPPORTED",
            "frozen agent workloads cannot refresh their container at runtime",
            exit_code=ExitCode.validation,
        )
    if spec.execution.container.network_enabled:
        raise ControlError(
            "AGENT_CONTAINER_NETWORK_UNSUPPORTED",
            "the initial agent workload requires network-disabled generated code",
            exit_code=ExitCode.validation,
        )
    if spec.execution.container.bind_mounts:
        raise ControlError(
            "AGENT_BIND_MOUNTS_UNSUPPORTED",
            "the initial agent workload accepts only its frozen input and output mounts",
            exit_code=ExitCode.validation,
        )
    gpu_requested = spec.execution.resources.gpu_count > 0
    if spec.execution.container.gpu_enabled != gpu_requested:
        raise ControlError(
            "AGENT_GPU_CONTRACT_INVALID",
            "container.gpu_enabled must match whether execution requests a GPU",
            exit_code=ExitCode.validation,
            details={
                "gpu_enabled": spec.execution.container.gpu_enabled,
                "gpu_count": spec.execution.resources.gpu_count,
            },
        )
    limited_budgets = sorted(
        field_name
        for field_name in type(spec.budget).model_fields
        if getattr(spec.budget, field_name).limit is not None
    )
    if limited_budgets:
        raise ControlError(
            "AGENT_BUDGET_ENFORCEMENT_UNAVAILABLE",
            "the initial real agent workload accepts only explicitly unlimited budgets",
            exit_code=ExitCode.validation,
            details={"limited_counters": limited_budgets},
        )
    retry = spec.stop_rules.retry
    categories = set(retry.retryable_categories)
    no_retry = (
        retry.maximum_attempts == 1
        and not categories
        and retry.base_delay_seconds == 0
        and retry.maximum_delay_seconds == 0
    )
    bounded_provider_retry = (
        retry.maximum_attempts > 1
        and FailureCategory.provider in categories
        and categories <= AGENT_RETRYABLE_FAILURES
    )
    if not (no_retry or bounded_provider_retry):
        raise ControlError(
            "AGENT_RETRY_POLICY_UNSUPPORTED",
            "real agent retries must be explicitly provider-scoped and may also "
            "classify timeouts",
            exit_code=ExitCode.validation,
            details={
                "maximum_attempts": retry.maximum_attempts,
                "retryable_categories": sorted(
                    category.value for category in categories
                ),
                "supported_categories": sorted(
                    category.value for category in AGENT_RETRYABLE_FAILURES
                ),
            },
        )


def _agent_smoke_delay(parameters: dict[str, Any]) -> float:
    raw = parameters.get(AGENT_SMOKE_DELAY_PARAMETER, 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ControlError(
            "ADAPTER_PARAMETER_INVALID",
            f"{AGENT_SMOKE_DELAY_PARAMETER} must be a finite number",
            exit_code=ExitCode.validation,
        )
    delay = float(raw)
    if delay < 0 or delay > 5:
        raise ControlError(
            "ADAPTER_PARAMETER_INVALID",
            f"{AGENT_SMOKE_DELAY_PARAMETER} must be between 0 and 5",
            exit_code=ExitCode.validation,
        )
    return delay


def validate_control_spec(
    spec: ExperimentSpec, *, require_submit_adapter: bool = False
) -> list[dict[str, Any]]:
    """Apply control-plane constraints not encoded as scientific fields."""

    _validate_output_root(spec.execution.output_root)
    checks: list[dict[str, Any]] = [
        {"check": "domain_schema", "status": "passed"},
        {"check": "output_path_policy", "status": "passed"},
    ]
    if spec.execution.executor == ExecutorKind.slurm:
        for field_name, value in (
            ("account", spec.execution.account),
            ("qos", spec.execution.qos),
        ):
            if value is not None and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
                raise ControlError(
                    "SLURM_DIRECTIVE_INVALID",
                    f"execution.{field_name} contains unsafe Slurm directive characters",
                    exit_code=ExitCode.validation,
                    details={"field": field_name},
                )
        if require_submit_adapter and len(spec.conditions) * spec.repetitions != 1:
            raise ControlError(
                "SLURM_MATRIX_UNSUPPORTED",
                "the initial Slurm lifecycle supports exactly one planned run",
                exit_code=ExitCode.validation,
                details={
                    "conditions": len(spec.conditions),
                    "repetitions": spec.repetitions,
                },
            )
    adapter_checks = []
    for condition_index, condition in enumerate(spec.conditions):
        adapter = condition.parameters.get(ADAPTER_PARAMETER)
        supported = isinstance(adapter, str) and adapter in LOCAL_ADAPTERS
        if require_submit_adapter and not supported:
            raise ControlError(
                "ADAPTER_UNSUPPORTED",
                "submission requires an explicit supported worker adapter",
                exit_code=ExitCode.validation,
                details={
                    "condition_id": condition.condition_id,
                    "adapter": adapter,
                    "supported": sorted(LOCAL_ADAPTERS),
                },
            )
        if adapter == LOCAL_LIFECYCLE_ADAPTER:
            _smoke_seconds(dict(condition.parameters))
        elif isinstance(adapter, str) and adapter in {
            AGENT_PATH_SMOKE_ADAPTER,
            CARIBOU_AGENT_ADAPTER,
        }:
            _validate_agent_adapter(spec, condition_index, str(adapter))
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

    checks = validate_control_spec(spec, require_submit_adapter=False)
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
