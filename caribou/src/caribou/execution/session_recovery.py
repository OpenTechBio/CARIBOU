"""Durable, transport-neutral checkpoint helpers for resumable web sessions.

The checkpoint deliberately captures reconstructable scientific state rather than
pretending to snapshot a Python process.  A fresh sandbox receives the working
AnnData file, model-visible history, runner cursor, action ledger, memory state,
and output manifest.  Arbitrary globals are rebuilt explicitly by one of the
recovery strategies below.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


CHECKPOINT_SCHEMA = "caribou.web_session_checkpoint.v1"
CHECKPOINT_DIR = ".checkpoints"
LIVE_DATASET = ".caribou-live-checkpoint.h5ad"
CONTAINER_LIVE_DATASET = f"/workspace/outputs/{LIVE_DATASET}"
ROLLING_CHECKPOINT_RETENTION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _checkpoint_root(output_dir: Path) -> Path:
    return output_dir.parent / CHECKPOINT_DIR


def _prune_superseded_checkpoints(root: Path, session: Any, latest_id: str) -> None:
    """Bound rolling storage while retaining checkpoints referenced by attempts."""

    checkpoint_dirs = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and path.name.startswith("checkpoint_")
            and (path / "checkpoint.json").is_file()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    protected = {latest_id}
    protected.update(path.name for path in checkpoint_dirs[:ROLLING_CHECKPOINT_RETENTION])
    forked_from = getattr(session, "forked_from_checkpoint_id", None)
    if forked_from:
        protected.add(str(forked_from))
    for attempt in getattr(session, "attempts", []):
        referenced = attempt.get("source_checkpoint_id")
        if referenced:
            protected.add(str(referenced))
    for path in checkpoint_dirs:
        if path.name not in protected:
            shutil.rmtree(path, ignore_errors=True)


def _action_ledger(events: list[dict[str, Any]], through_turn: int) -> list[dict[str, Any]]:
    """Pair submitted code with its result without inventing missing evidence."""

    pending: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    for event in events:
        turn = int(event.get("turn", 0) or 0)
        if turn > through_turn:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "code_submitted":
            entry = {
                "action_id": data.get("action_id") or f"legacy:{turn}:{len(pending)}",
                "turn": turn,
                "agent_name": data.get("agent_name", ""),
                "source": data.get("source", ""),
                "recorded_result": None,
            }
            pending.append(entry)
            ledger.append(entry)
        elif kind == "code_result":
            action_id = data.get("action_id")
            match = next(
                (
                    item
                    for item in reversed(pending)
                    if item["recorded_result"] is None
                    and (not action_id or item["action_id"] == action_id)
                ),
                None,
            )
            if match is not None:
                match["recorded_result"] = {
                    "success": bool(data.get("success", False)),
                    "stdout": data.get("stdout", ""),
                    "stderr": data.get("stderr", ""),
                    "duration_ms": int(data.get("duration_ms", 0) or 0),
                }
    return ledger


def _memory_snapshot(memory_manager: object | None) -> dict[str, Any] | None:
    if memory_manager is None:
        return None
    exporter = getattr(memory_manager, "export_checkpoint", None)
    if callable(exporter):
        return exporter()
    getter = getattr(memory_manager, "get_state", None)
    return {
        "schema_version": "caribou.memory_checkpoint.summary.v1",
        "restorable": False,
        "summary": getter() if callable(getter) else {},
    }


def _artifact_manifest(output_dir: Path) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    if not output_dir.exists():
        return manifest
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == LIVE_DATASET:
            continue
        relative = path.relative_to(output_dir)
        manifest.append(
            {
                "path": relative.as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _hash_file(path),
            }
        )
    return manifest


def capture_checkpoint(
    *,
    session: Any,
    history: list[dict[str, str]],
    runner_state: dict[str, Any],
) -> dict[str, Any]:
    """Capture one immutable checkpoint and atomically publish it as latest."""

    checkpoint_id = f"checkpoint_{uuid4().hex}"
    root = _checkpoint_root(session.output_dir)
    destination_dir = root / checkpoint_id
    destination_dir.mkdir(parents=True, exist_ok=False)
    source_dataset = session.output_dir / LIVE_DATASET
    dataset_kind = "working_anndata"
    capture_error: str | None = None

    if session.sandbox_manager is not None and int(runner_state.get("turns_completed", 0)) > 0:
        capture_code = (
            "import os as _caribou_os\n"
            "_caribou_adata = globals().get('adata')\n"
            "if _caribou_adata is None or not hasattr(_caribou_adata, 'write_h5ad'):\n"
            "    raise RuntimeError(\"no checkpointable AnnData global named 'adata'\")\n"
            f"_caribou_tmp = {CONTAINER_LIVE_DATASET!r} + '.tmp'\n"
            "_caribou_adata.write_h5ad(_caribou_tmp)\n"
            f"_caribou_os.replace(_caribou_tmp, {CONTAINER_LIVE_DATASET!r})\n"
        )
        try:
            result = session.sandbox_manager.exec_code(capture_code, timeout=600)
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise RuntimeError(str((result or {}).get("stderr", "checkpoint capture failed")))
        except Exception as exc:  # checkpoint failure must not terminate live work
            capture_error = str(exc)

        if capture_error is None and not source_dataset.is_file():
            capture_error = "sandbox reported success but produced no live AnnData checkpoint"

    if not source_dataset.is_file():
        dataset_kind = "original_dataset"
        source_dataset = Path(session.config.dataset_path)
    if not source_dataset.is_file():
        shutil.rmtree(destination_dir, ignore_errors=True)
        raise FileNotFoundError(f"No recoverable dataset is available: {source_dataset}")

    dataset_path = destination_dir / "dataset.h5ad"
    shutil.copy2(source_dataset, dataset_path)
    turn = int(runner_state.get("turns_completed", session.current_turn) or 0)
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "session_id": session.id,
        "created_at": _now(),
        "turn": turn,
        "current_agent": runner_state.get("current_agent_name") or session.current_agent,
        "dataset": {
            "path": "dataset.h5ad",
            "kind": dataset_kind,
            "sha256": _hash_file(dataset_path),
        },
        "history": [
            {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
            for item in history
        ],
        "runner_state": dict(runner_state),
        "memory": _memory_snapshot(session.memory_manager),
        "actions": [
            dict(item) for item in runner_state.get("action_ledger", [])
        ] or _action_ledger(session.events, turn),
        "artifacts": _artifact_manifest(session.output_dir),
        "capture_error": capture_error,
        "complete": capture_error is None or turn == 0,
    }
    _atomic_json(destination_dir / "checkpoint.json", checkpoint)
    _atomic_json(root / "latest.json", {"checkpoint_id": checkpoint_id})
    session.checkpoint_id = checkpoint_id
    session.checkpoint_turn = turn
    session.checkpoint_healthy = bool(checkpoint["complete"])
    _prune_superseded_checkpoints(root, session, checkpoint_id)
    return checkpoint


def load_checkpoint(output_dir: Path, checkpoint_id: str | None = None) -> dict[str, Any] | None:
    root = _checkpoint_root(output_dir)
    if checkpoint_id is None:
        pointer = root / "latest.json"
        if not pointer.is_file():
            return None
        checkpoint_id = json.loads(pointer.read_text(encoding="utf-8"))["checkpoint_id"]
    path = root / checkpoint_id / "checkpoint.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("unsupported web session checkpoint schema")
    return value


def checkpoint_dataset_path(output_dir: Path, checkpoint: dict[str, Any]) -> Path:
    return _checkpoint_root(output_dir) / checkpoint["checkpoint_id"] / checkpoint["dataset"]["path"]


def publish_checkpoint_pointer(output_dir: Path, checkpoint_id: str) -> None:
    """Atomically make an already-copied immutable checkpoint the latest one."""

    _atomic_json(_checkpoint_root(output_dir) / "latest.json", {"checkpoint_id": checkpoint_id})


def copy_output_tree(source: Path, destination: Path) -> None:
    """Copy user-visible outputs while excluding internal rolling state."""

    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.name == LIVE_DATASET:
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def bootstrap_anndata(sandbox: object) -> tuple[bool, str]:
    code = (
        "import anndata as _caribou_anndata\n"
        "adata = _caribou_anndata.read_h5ad('/workspace/dataset.h5ad')\n"
        "print(f'Restored AnnData: {adata.n_obs} observations x {adata.n_vars} variables')\n"
    )
    try:
        result = sandbox.exec_code(code, timeout=600)  # type: ignore[attr-defined]
    except Exception as exc:
        return False, str(exc)
    return result.get("status") == "ok", str(result.get("stderr") or result.get("stdout") or "")


def literal_replay(
    *,
    sandbox: object,
    checkpoint: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
) -> tuple[bool, str]:
    """Replay every recorded attempt in order and report divergences."""

    divergences: list[str] = []
    actions = checkpoint.get("actions", [])
    if not actions:
        return True, "No recorded code attempts required replay."
    for index, action in enumerate(actions, start=1):
        source = action.get("source", "")
        if not source:
            divergences.append(f"attempt {index} has no retained source")
            continue
        emit({"phase": "literal_replay", "step": index, "total": len(actions)})
        try:
            result = sandbox.exec_code(source, timeout=600)  # type: ignore[attr-defined]
            replay_success = result.get("status") == "ok"
        except Exception as exc:
            divergences.append(f"attempt {index} could not execute: {exc}")
            continue
        recorded = action.get("recorded_result")
        if recorded is None:
            divergences.append(f"attempt {index} has no retained result")
        elif replay_success != bool(recorded.get("success")):
            divergences.append(
                f"attempt {index} status diverged (recorded={recorded.get('success')}, replay={replay_success})"
            )
    if divergences:
        return False, "; ".join(divergences)
    return True, f"Replayed {len(actions)} recorded code attempts."


def smart_rebuild(
    *,
    sandbox: object,
    llm_client: object,
    model_name: str,
    current_agent_prompt: str,
    checkpoint: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    maximum_attempts: int = 3,
) -> tuple[bool, str]:
    """Ask the selected agent to rebuild only transient variables it can justify."""

    from caribou.core.io_helpers import extract_python_code_blocks

    ledger = [
        {"turn": item.get("turn"), "source": item.get("source"), "result": item.get("recorded_result")}
        for item in checkpoint.get("actions", [])
    ]
    messages = [
        {
            "role": "system",
            "content": (
                current_agent_prompt
                + "\n\nRECOVERY MODE: A fresh Python process has loaded the checkpointed AnnData as `adata`. "
                "Files and conversation history are durable, but arbitrary Python globals are absent. "
                "Reconstruct only imports, helper functions, and transient variables required to continue. "
                "Do not repeat completed scientific transformations. Return exactly one Python code block "
                "that verifies the rebuilt environment."
            ),
        },
        {"role": "user", "content": "Recorded code ledger:\n" + json.dumps(ledger, default=str)},
    ]
    feedback = ""
    for attempt in range(1, maximum_attempts + 1):
        emit({"phase": "smart_rebuild", "step": attempt, "total": maximum_attempts})
        try:
            response = llm_client.chat.completions.create(  # type: ignore[attr-defined]
                model=model_name,
                messages=messages,
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            blocks = extract_python_code_blocks(content)
            if not blocks:
                feedback = "Recovery agent returned no Python code."
            else:
                result = sandbox.exec_code(blocks[0], timeout=600)  # type: ignore[attr-defined]
                if result.get("status") == "ok":
                    return True, str(result.get("stdout") or "Smart rebuild completed.")
                feedback = str(result.get("stderr") or "Recovery code failed.")
        except Exception as exc:
            feedback = str(exc)
        messages.extend(
            [
                {"role": "assistant", "content": content if 'content' in locals() else ""},
                {"role": "user", "content": f"Recovery attempt failed: {feedback}. Correct it without repeating completed analysis."},
            ]
        )
    return False, feedback or "Smart rebuild exhausted its recovery budget."
