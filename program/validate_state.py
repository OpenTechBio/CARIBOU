#!/usr/bin/env python3
"""Validate CARIBOU's durable autonomous-program state without mutating it."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment bootstrap failure
    raise SystemExit(
        "Install program validation dependencies with "
        "`python -m pip install -r program/requirements.txt`."
    ) from exc

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:  # pragma: no cover - environment bootstrap failure
    raise SystemExit(
        "Install program validation dependencies with "
        "`python -m pip install -r program/requirements.txt`."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PROGRAM = ROOT / "program"
ALLOWED_STATES = {
    "Pending",
    "In progress",
    "Evidence collected",
    "Validated",
    "Not claimed",
    "Blocked",
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse {display_path(path)}: {exc}") from exc


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot parse {display_path(path)}: {exc}") from exc


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def missing_keys(instance: Any, required: set[str], label: str) -> list[str]:
    if not isinstance(instance, dict):
        return [f"{label}: expected an object"]
    return [f"{label}: missing required key {key}" for key in sorted(required - instance.keys())]


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_against_schema(instance: Any, schema: Any, label: str) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path)
        prefix = f"{label}.{location}" if location else label
        errors.append(f"{prefix}: {error.message}")
    return errors


def path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def policy_path(value: str) -> Path:
    # Approved roots may use a trailing recursive glob to describe the whole tree.
    return Path(value.removesuffix("/**")).expanduser().resolve()


def validate_policy(policy: Any) -> list[str]:
    required = {
        "policy_version",
        "policy_id",
        "issued_at",
        "decision_policy",
        "repository",
        "delegation",
        "local_execution",
        "containers",
        "external_model_apis",
        "local_models",
        "slurm",
        "data",
        "external_actions",
        "completion",
    }
    errors = missing_keys(policy, required, "policy")
    if errors:
        return errors
    if policy.get("policy_version") != 1:
        errors.append("policy: policy_version must be 1")
    if not valid_timestamp(policy.get("issued_at")):
        errors.append("policy: issued_at must be an RFC 3339 timestamp")
    if policy.get("decision_policy", {}).get("mode") != "autonomous_within_policy":
        errors.append("policy: decision mode must be autonomous_within_policy")
    for section in ("delegation", "containers", "external_actions", "completion"):
        if not isinstance(policy.get(section), dict) or not all(
            isinstance(value, bool) for value in policy[section].values()
        ):
            errors.append(f"policy: {section} must contain only boolean policy values")
    for section in ("external_model_apis", "local_models", "slurm"):
        if not isinstance(policy.get(section, {}).get("allowed"), bool):
            errors.append(f"policy: {section}.allowed must be boolean")

    repository = policy.get("repository", {})
    remote = repository.get("remote_write_policy", {})
    denied = remote.get("denied_branches", [])
    external_actions = policy.get("external_actions", {})
    if repository.get("push_remote"):
        if remote.get("allowed_ref_kind") != "branch":
            errors.append("policy: remote writes may target branch refs only")
        if "main" not in denied:
            errors.append("policy: main must be listed in repository.remote_write_policy.denied_branches")
    if remote.get("push_tags"):
        errors.append("policy: tag pushes are not authorized by this program contract")
    if external_actions.get("push_main") or external_actions.get("merge_main"):
        errors.append("policy: main push and merge must remain disabled")

    slurm = policy.get("slurm", {})
    if slurm.get("allowed"):
        if slurm.get("required_partition") != "peerd" or slurm.get("partitions") != ["peerd"]:
            errors.append("policy: authorized Slurm work must use partition peerd exclusively")

    data = policy.get("data", {})
    approved_roots = [policy_path(value) for value in data.get("approved_paths", [])]
    for approved_root in approved_roots:
        if not path_within(approved_root, ROOT):
            errors.append(f"policy: approved data root must be inside CARIBOU: {approved_root}")
    download_paths = [policy_path(value) for value in data.get("external_dataset_download_paths", [])]
    if data.get("external_dataset_download_allowed") and not download_paths:
        errors.append("policy: dataset downloads require at least one governed download path")
    for download_path in download_paths:
        if not path_within(download_path, ROOT):
            errors.append(f"policy: dataset download path must be inside CARIBOU: {download_path}")
        if approved_roots and not any(path_within(download_path, root) for root in approved_roots):
            errors.append(f"policy: dataset download path is outside approved data roots: {download_path}")
    return errors


def validate_state_shape(state: Any) -> list[str]:
    required = {
        "schema_version",
        "program_id",
        "goal_reference",
        "policy_reference",
        "status",
        "created_at",
        "updated_at",
        "code",
        "active_task",
        "next_action",
        "milestones",
        "budgets",
        "indexes",
        "documents",
    }
    errors = missing_keys(state, required, "state")
    if errors:
        return errors
    if state.get("schema_version") != "caribou-program-state-v1":
        errors.append("state: unsupported schema_version")
    if state.get("status") not in ALLOWED_STATES:
        errors.append("state: invalid program status")
    for key in ("created_at", "updated_at"):
        if not valid_timestamp(state.get(key)):
            errors.append(f"state: {key} must be an RFC 3339 timestamp")
    commit = state.get("code", {}).get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        errors.append("state: code.commit must be a full hexadecimal Git commit")
    if not isinstance(state.get("milestones"), list) or not state["milestones"]:
        errors.append("state: milestones must be a non-empty list")
    return errors


def validate_decisions(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"decisions.jsonl line {line_number}: {exc}")
            continue
        decision_id = record.get("decision_id")
        if not decision_id:
            errors.append(f"decisions.jsonl line {line_number}: missing decision_id")
        elif decision_id in seen:
            errors.append(f"decisions.jsonl line {line_number}: duplicate {decision_id}")
        else:
            seen.add(decision_id)
        records.append(record)
    return records, errors


def resolve_reference(value: str, program_root: Path) -> Path | None:
    if not value:
        return None
    if value.startswith("repo:"):
        return ROOT / value.removeprefix("repo:")
    if value.startswith("program:"):
        return program_root / value.removeprefix("program:")
    if value.startswith(("http://", "https://", "doi:", "s3://")):
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--program-root",
        type=Path,
        help=(
            "Live program-state directory. Defaults to CARIBOU_PROGRAM_HOME; "
            "when unset, validates the public program/template example."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configured = args.program_root or os.environ.get("CARIBOU_PROGRAM_HOME")
    program_root = Path(configured).expanduser().resolve() if configured else (PUBLIC_PROGRAM / "template").resolve()
    if not program_root.is_dir():
        raise SystemExit(f"Program root does not exist: {program_root}")

    errors: list[str] = []
    policy = load_yaml(program_root / "policy.yaml")
    state = load_json(program_root / "state.json")
    blockers = load_json(program_root / "blockers.json")
    decisions, decision_errors = validate_decisions(program_root / "decisions.jsonl")
    errors.extend(decision_errors)

    policy_schema = load_json(PUBLIC_PROGRAM / "schemas/policy.schema.json")
    state_schema = load_json(PUBLIC_PROGRAM / "schemas/state.schema.json")
    errors.extend(validate_against_schema(policy, policy_schema, "policy"))
    errors.extend(validate_against_schema(state, state_schema, "state"))
    errors.extend(validate_policy(policy))
    errors.extend(validate_state_shape(state))

    milestone_ids = [item.get("milestone_id") for item in state.get("milestones", [])]
    if len(milestone_ids) != len(set(milestone_ids)):
        errors.append("state: milestone IDs must be unique")

    active_blockers = {item.get("blocker_id") for item in blockers.get("active", [])}
    resolved_blockers = {item.get("blocker_id") for item in blockers.get("resolved", [])}
    if active_blockers & resolved_blockers:
        errors.append("blockers: IDs cannot be both active and resolved")

    for milestone in state.get("milestones", []):
        if milestone.get("status") not in ALLOWED_STATES:
            errors.append(f"state: invalid status for {milestone.get('milestone_id')}")
        for blocker_id in milestone.get("blocked_by", []):
            if blocker_id not in active_blockers:
                errors.append(
                    f"state: {milestone.get('milestone_id')} references inactive blocker {blocker_id}"
                )

    required_refs = [state.get("goal_reference"), state.get("policy_reference")]
    required_refs.extend(state.get("indexes", {}).values())
    required_refs.extend(state.get("documents", {}).values())
    required_refs.extend(state.get("active_task", {}).get("evidence", []))
    for reference in required_refs:
        path = resolve_reference(reference, program_root) if isinstance(reference, str) else None
        if path is None or not path.exists():
            errors.append(f"state: missing local reference {reference!r}")

    next_action = state.get("next_action", {})
    if next_action.get("milestone_id") not in set(milestone_ids):
        errors.append("state: next_action references an unknown milestone")
    skill_name = next_action.get("skill")
    if skill_name and not (ROOT / "skills" / skill_name / "SKILL.md").exists():
        errors.append(f"state: next_action references missing skill {skill_name}")

    for name, path_value in state.get("indexes", {}).items():
        path = resolve_reference(path_value, program_root)
        if path is None or not path.exists():
            errors.append(f"state: missing index {path_value!r}")
            continue
        index = load_json(path)
        expected_key = "claims" if name == "evidence" else name
        if not isinstance(index.get(expected_key), list):
            errors.append(f"{path_value}: expected list field {expected_key}")

    budgets = state.get("budgets", {})
    api = policy.get("external_model_apis", {})
    slurm = policy.get("slurm", {})
    if not api.get("allowed") and any(
        budgets.get(key, 0) != 0
        for key in ("external_api_calls_used", "external_api_tokens_used", "external_api_spend_usd")
    ):
        errors.append("state: external API consumption is nonzero while policy disallows it")
    if not slurm.get("allowed") and any(
        budgets.get(key, 0) != 0
        for key in ("slurm_jobs_submitted", "slurm_cpu_hours_used", "slurm_gpu_hours_used")
    ):
        errors.append("state: Slurm consumption is nonzero while policy disallows it")

    result = {
        "valid": not errors,
        "program_id": state.get("program_id"),
        "program_root": str(program_root),
        "status": state.get("status"),
        "milestones": len(milestone_ids),
        "active_blockers": len(active_blockers),
        "decisions": len(decisions),
        "next_action": next_action.get("task_id"),
        "errors": errors,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
