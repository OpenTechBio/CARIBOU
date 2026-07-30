from __future__ import annotations

import time
from pathlib import Path

import pytest

from caribou.control.executor import LocalProcessExecutor
from caribou.control.specs import ADAPTER_PARAMETER, LOCAL_LIFECYCLE_ADAPTER
from caribou.control.store import ExperimentStore, TERMINAL_RUN_STATES
from caribou.domain.models import ExperimentSpec

from .test_domain_models import make_spec


def _queued_local_run(tmp_path: Path) -> tuple[ExperimentStore, str]:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={"parameters": {ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER}}
    )
    spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [condition], "repetitions": 1}
        ).model_dump_json()
    )
    store = ExperimentStore(tmp_path / "store")
    return store, store.submit(spec, "local-launch-gap").runs[0].run_id


def test_child_claim_closes_parent_popen_to_handle_crash_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run_id = _queued_local_run(tmp_path)
    executor = LocalProcessExecutor()
    original_write = store.write_execution_handle
    failed_once = False

    def fail_parent_handle_write(handle) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("injected parent death before durable handle")
        original_write(handle)

    monkeypatch.setattr(store, "write_execution_handle", fail_parent_handle_write)
    with pytest.raises(RuntimeError, match="injected parent death"):
        executor.launch(store, run_id)
    monkeypatch.setattr(store, "write_execution_handle", original_write)

    deadline = time.monotonic() + 10
    while store.execution_handle(run_id) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    child_handle = store.execution_handle(run_id)
    assert child_handle is not None

    replay = executor.launch(store, run_id)
    assert replay.launched is False
    assert replay.handle == child_handle

    while store.run(run_id).state not in TERMINAL_RUN_STATES:
        if time.monotonic() >= deadline:
            raise AssertionError("self-claimed local worker did not finish")
        time.sleep(0.02)
    assert store.run(run_id).state.value == "succeeded"
    assert len(store.artifact_manifest(run_id).artifacts) == 1
