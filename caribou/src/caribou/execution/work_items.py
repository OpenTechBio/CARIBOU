"""Enforced, Git-backed work items shared by CLI and web execution.

The public identifier is a small per-run integer.  Git commit identifiers are
kept as provenance only, so agents and operators never need to pass SHAs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


WORK_ITEM_SCHEMA = "caribou.work_item.v1"
WORK_ITEM_INDEX_SCHEMA = "caribou.work_item_index.v1"
WORK_ITEM_STATUSES = ("Backlog", "Ready", "In progress", "In review", "Done")
QcMode = Literal["optional", "required"]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


class WorkItemError(ValueError):
    """Base class for user-visible work-item failures."""


class WorkItemNotFound(WorkItemError):
    pass


class WorkItemConflict(WorkItemError):
    pass


class WorkItemPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkItemPolicy:
    qc_mode: QcMode = "optional"

    @classmethod
    def from_dict(cls, raw: object) -> "WorkItemPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("work_item_policy must be an object")
        qc_mode = raw.get("qc_mode", "optional")
        if qc_mode not in {"optional", "required"}:
            raise ValueError(
                "work_item_policy.qc_mode must be 'optional' or 'required'"
            )
        return cls(qc_mode=qc_mode)

    def to_dict(self) -> Dict[str, str]:
        return {"qc_mode": self.qc_mode}


@dataclass(frozen=True)
class WorkItemCommand:
    name: Literal[
        "open_work_item", "close_work_item", "list_work_items", "read_work_item"
    ]
    item_id: Optional[int] = None
    title: str = ""
    body: str = ""
    completion_summary: str = ""


@dataclass(frozen=True)
class WorkItemCommandResult:
    feedback: str
    success: bool
    changed_item: Optional[Dict[str, Any]] = None


def parse_work_item_command(message: str) -> Optional[WorkItemCommand]:
    """Parse one exact command-only assistant message.

    Quoting follows shell-like rules solely for tokenization; nothing is ever
    passed through a shell.  Messages containing prose, code, or extra lines do
    not become commands accidentally.
    """
    if not message or len(message.splitlines()) != 1:
        return None
    try:
        tokens = shlex.split(message.strip())
    except ValueError:
        return None
    if not tokens:
        return None
    name = tokens[0]
    if name == "list_work_items" and len(tokens) == 1:
        return WorkItemCommand(name="list_work_items")
    if name == "read_work_item" and len(tokens) == 2 and tokens[1].isdigit():
        return WorkItemCommand(name="read_work_item", item_id=int(tokens[1]))
    if name == "open_work_item" and len(tokens) == 3:
        title, body = tokens[1], tokens[2]
        if title.strip() and body.strip():
            return WorkItemCommand(name="open_work_item", title=title, body=body)
    if name == "close_work_item" and len(tokens) == 3 and tokens[1].isdigit():
        if tokens[2].strip():
            return WorkItemCommand(
                name="close_work_item",
                item_id=int(tokens[1]),
                completion_summary=tokens[2],
            )
    return None


def parse_delegation_item_id(message: str, command_name: str) -> Optional[int]:
    """Return the optional item id from an exact delegation command.

    ``None`` also represents a legacy delegation without an item.  Callers use
    this only after ordinary delegation detection has matched ``command_name``.
    """
    if not message or len(message.splitlines()) != 1:
        return None
    try:
        tokens = shlex.split(message.strip())
    except ValueError:
        return None
    if len(tokens) == 2 and tokens[0] == command_name and tokens[1].isdigit():
        return int(tokens[1])
    return None


class WorkItemStore:
    """A per-run, commit-backed work-item ledger."""

    _lock_registry_guard = threading.Lock()
    _locks: Dict[str, threading.RLock] = {}

    def __init__(self, base_dir: Path, run_id: str, policy: WorkItemPolicy) -> None:
        self.root = Path(base_dir) / "work-items"
        self.items_dir = self.root / "items"
        self.run_id = run_id
        self.policy = policy
        lock_key = str(self.root.resolve())
        with self._lock_registry_guard:
            self._lock = self._locks.setdefault(lock_key, threading.RLock())
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.items_dir.mkdir(parents=True, exist_ok=True)
            if not (self.root / ".git").exists():
                self._git("init", "--quiet")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.root,
                check=check,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "CARIBOU",
                    "GIT_AUTHOR_EMAIL": "caribou@localhost",
                    "GIT_COMMITTER_NAME": "CARIBOU",
                    "GIT_COMMITTER_EMAIL": "caribou@localhost",
                },
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorkItemPersistenceError(
                f"work-item Git operation failed: {detail}"
            ) from exc

    def _has_head(self) -> bool:
        result = self._git("rev-parse", "--verify", "HEAD", check=False)
        return result.returncode == 0

    def _read_head_json(self, relative_path: str, default: Any = None) -> Any:
        if not self._has_head():
            return default
        result = self._git("show", f"HEAD:{relative_path}", check=False)
        if result.returncode != 0:
            return default
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorkItemPersistenceError(
                f"committed work-item data is invalid: {relative_path}"
            ) from exc

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _restore_committed_tree(self) -> None:
        if self._has_head():
            self._git("restore", "--source=HEAD", "--staged", "--worktree", "--", ".")

    def _commit(self, message: str, index: Dict[str, Any], item: Dict[str, Any]) -> str:
        self._restore_committed_tree()
        self._atomic_json(self.root / "index.json", index)
        self._atomic_json(self.items_dir / f"{item['id']}.json", item)
        self._git("add", "index.json", f"items/{item['id']}.json")
        self._git("commit", "--quiet", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _index(self) -> Dict[str, Any]:
        index = self._read_head_json(
            "index.json",
            {
                "schema_version": WORK_ITEM_INDEX_SCHEMA,
                "run_id": self.run_id,
                "qc_mode": self.policy.qc_mode,
                "next_id": 0,
                "items": [],
            },
        )
        committed_mode = index.get("qc_mode")
        if committed_mode in {"optional", "required"}:
            self.policy = WorkItemPolicy(qc_mode=committed_mode)
        return index

    def _item(self, item_id: int) -> Dict[str, Any]:
        item = self._read_head_json(f"items/{item_id}.json")
        if item is None:
            raise WorkItemNotFound(f"work item {item_id} was not found")
        return item

    @staticmethod
    def _summary(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "id",
                "title",
                "status",
                "owner",
                "created_turn",
                "created_at",
                "completed_turn",
                "completed_at",
            )
        }

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            index = self._index()
            return [dict(value) for value in index.get("items", [])]

    def read(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            item = dict(self._item(item_id))
            log = self._git(
                "log", "--format=%H", "--", f"items/{item_id}.json", check=False
            )
            commits = [line for line in log.stdout.splitlines() if line]
            item["latest_commit"] = commits[0] if commits else None
            item["opening_commit"] = commits[-1] if commits else None
            return item

    def _update_index(self, index: Dict[str, Any], item: Dict[str, Any]) -> None:
        summaries = [
            summary
            for summary in index.get("items", [])
            if summary.get("id") != item["id"]
        ]
        summaries.append(self._summary(item))
        summaries.sort(key=lambda value: int(value["id"]))
        index["items"] = summaries

    def open(self, title: str, body: str, owner: str, turn: int) -> Dict[str, Any]:
        if not title.strip() or not body.strip():
            raise WorkItemConflict("work-item title and body must be non-empty")
        with self._lock:
            index = self._index()
            item_id = int(index.get("next_id", 0))
            timestamp = utc_now()
            item = {
                "schema_version": WORK_ITEM_SCHEMA,
                "run_id": index.get("run_id", self.run_id),
                "id": item_id,
                "title": title.strip(),
                "body": body.strip(),
                "status": "In progress",
                "owner": owner,
                "created_turn": turn,
                "created_at": timestamp,
                "completion_summary": None,
                "closed_turn": None,
                "closed_at": None,
                "completed_turn": None,
                "completed_at": None,
                "transitions": [
                    {
                        "kind": "opened",
                        "actor": owner,
                        "turn": turn,
                        "timestamp": timestamp,
                        "from_status": None,
                        "to_status": "In progress",
                        "from_owner": None,
                        "to_owner": owner,
                    }
                ],
                "reviews": [],
            }
            index["next_id"] = item_id + 1
            self._update_index(index, item)
            self._commit(f"work-item {item_id}: opened", index, item)
            return self.read(item_id)

    def close(
        self, item_id: int, summary: str, actor: str, turn: int
    ) -> Dict[str, Any]:
        if not summary.strip():
            raise WorkItemConflict("completion summary must be non-empty")
        with self._lock:
            index = self._index()
            item = self._item(item_id)
            if item["owner"] != actor:
                raise WorkItemConflict(
                    f"work item {item_id} is owned by {item['owner']}, not {actor}"
                )
            if item["status"] != "In progress":
                raise WorkItemConflict(
                    f"work item {item_id} cannot close from {item['status']}"
                )
            timestamp = utc_now()
            destination = "Done" if self.policy.qc_mode == "optional" else "In review"
            item["status"] = destination
            item["completion_summary"] = summary.strip()
            item["closed_turn"] = turn
            item["closed_at"] = timestamp
            if destination == "Done":
                item["completed_turn"] = turn
                item["completed_at"] = timestamp
            item["transitions"].append(
                {
                    "kind": "closed",
                    "actor": actor,
                    "turn": turn,
                    "timestamp": timestamp,
                    "from_status": "In progress",
                    "to_status": destination,
                    "from_owner": actor,
                    "to_owner": actor,
                }
            )
            self._update_index(index, item)
            self._commit(f"work-item {item_id}: closed to {destination}", index, item)
            return self.read(item_id)

    def transfer(
        self, item_id: int, from_owner: str, to_owner: str, turn: int
    ) -> Dict[str, Any]:
        with self._lock:
            index = self._index()
            item = self._item(item_id)
            if item["owner"] != from_owner:
                raise WorkItemConflict(
                    f"work item {item_id} is owned by {item['owner']}, not {from_owner}"
                )
            if item["status"] == "Done":
                raise WorkItemConflict(f"work item {item_id} is already Done")
            timestamp = utc_now()
            item["owner"] = to_owner
            item["transitions"].append(
                {
                    "kind": "reassigned",
                    "actor": from_owner,
                    "turn": turn,
                    "timestamp": timestamp,
                    "from_status": item["status"],
                    "to_status": item["status"],
                    "from_owner": from_owner,
                    "to_owner": to_owner,
                }
            )
            self._update_index(index, item)
            self._commit(
                f"work-item {item_id}: reassigned {from_owner} to {to_owner}",
                index,
                item,
            )
            return self.read(item_id)

    def blocking_for_owner(self, owner: str) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.list()
            if item.get("owner") == owner and item.get("status") != "Done"
        ]

    def review_diff(self, item_id: int) -> str:
        with self._lock:
            item = self.read(item_id)
            opening, latest = item.get("opening_commit"), item.get("latest_commit")
            if not opening or not latest or opening == latest:
                return (
                    self._git(
                        "show", "--format=", latest, "--", f"items/{item_id}.json"
                    ).stdout
                    if latest
                    else ""
                )
            return self._git(
                "diff", opening, latest, "--", f"items/{item_id}.json"
            ).stdout

    def record_review(
        self,
        item_id: int,
        *,
        evaluator: str,
        turn: int,
        verdict: Optional[Literal["approve", "reject"]],
        assessment: str,
        provider_receipt: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            index = self._index()
            item = self._item(item_id)
            if self.policy.qc_mode == "required" and item["status"] != "In review":
                raise WorkItemConflict(
                    f"required-QC review needs In review status, found {item['status']}"
                )
            if self.policy.qc_mode == "optional" and item["status"] != "Done":
                raise WorkItemConflict(
                    f"optional-QC review needs Done status, found {item['status']}"
                )
            timestamp = utc_now()
            review = {
                "evaluator": evaluator,
                "turn": turn,
                "timestamp": timestamp,
                "verdict": verdict,
                "assessment": assessment,
                "provider_receipt": dict(provider_receipt or {}),
                "error": error,
            }
            item["reviews"].append(review)
            if not error and self.policy.qc_mode == "required":
                previous = item["status"]
                destination = "Done" if verdict == "approve" else "In progress"
                item["status"] = destination
                if destination == "Done":
                    item["completed_turn"] = turn
                    item["completed_at"] = timestamp
                item["transitions"].append(
                    {
                        "kind": "reviewed",
                        "actor": evaluator,
                        "turn": turn,
                        "timestamp": timestamp,
                        "from_status": previous,
                        "to_status": destination,
                        "from_owner": item["owner"],
                        "to_owner": item["owner"],
                    }
                )
            self._update_index(index, item)
            label = "failed" if error else str(verdict)
            self._commit(f"work-item {item_id}: review {label}", index, item)
            return self.read(item_id)


def render_work_item_prompt(policy: WorkItemPolicy) -> str:
    close_result = "Done" if policy.qc_mode == "optional" else "In review"
    return (
        "\n\nWork items are enforced in this interactive run. Use exactly one command on "
        "a standalone line, with no prose, Markdown, or code in the same message:\n"
        '- `open_work_item "<title>" "<body>"`\n'
        '- `close_work_item <id> "<completion summary>"`\n'
        "- `list_work_items`\n"
        "- `read_work_item <id>`\n"
        "To transfer one item during delegation, use `delegate_to_<agent> <id>`. "
        "Delegation without an id transfers nothing. "
        f"Closing moves the item to {close_result}. You cannot use `end_session` "
        "while you own a work item that is not Done."
    )


def execute_work_item_command(
    store: WorkItemStore,
    command: WorkItemCommand,
    *,
    owner: str,
    turn: int,
) -> WorkItemCommandResult:
    """Apply one parsed command with transport-neutral feedback."""
    try:
        if command.name == "open_work_item":
            item = store.open(command.title, command.body, owner, turn)
            feedback = (
                f"Opened work item {item['id']} ({item['status']}) for {item['owner']}."
            )
            return WorkItemCommandResult(feedback, True, item)
        if command.name == "close_work_item":
            if command.item_id is None:
                raise WorkItemConflict("close_work_item needs an item id")
            item = store.close(command.item_id, command.completion_summary, owner, turn)
            feedback = f"Work item {item['id']} moved to {item['status']}."
            return WorkItemCommandResult(feedback, True, item)
        if command.name == "list_work_items":
            return WorkItemCommandResult(
                "Work items: " + json.dumps(store.list(), sort_keys=True), True
            )
        if command.item_id is None:
            raise WorkItemConflict("read_work_item needs an item id")
        return WorkItemCommandResult(
            "Work item: " + json.dumps(store.read(command.item_id), sort_keys=True),
            True,
        )
    except WorkItemError as exc:
        return WorkItemCommandResult(f"Work-item command refused: {exc}", False)
