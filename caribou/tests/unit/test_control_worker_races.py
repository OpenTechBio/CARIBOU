from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from caribou.control.api import ExitCode
from caribou.control.specs import ADAPTER_PARAMETER, LOCAL_LIFECYCLE_ADAPTER
from caribou.control.store import ExperimentStore
from caribou.control.worker import execute
from caribou.domain.enums import RunState
from caribou.domain.models import ExperimentSpec

from .test_domain_models import make_spec


def _queued_store(tmp_path: Path) -> tuple[ExperimentStore, str]:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={
            "parameters": {
                ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER,
                "caribou.lifecycle_smoke_seconds": 0.0,
            }
        }
    )
    spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [condition], "repetitions": 1}
        ).model_dump_json()
    )
    store = ExperimentStore(tmp_path / "store")
    run_id = store.submit(spec, "worker-cancel-race").runs[0].run_id
    return store, run_id


@pytest.mark.parametrize("raced_target", [RunState.running, RunState.succeeded])
def test_cancellation_wins_worker_transition_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_target: RunState,
) -> None:
    store, run_id = _queued_store(tmp_path)
    original_transition = store.transition_run
    raced = False

    def transition_with_cancel(
        target_run_id: str,
        target: RunState,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        nonlocal raced
        if target == raced_target and not raced:
            raced = True
            store.request_cancel(
                target_run_id,
                actor="race-test",
                reason=f"cancel before {target.value}",
            )
        return original_transition(target_run_id, target, **kwargs)

    monkeypatch.setattr(store, "transition_run", transition_with_cancel)

    assert execute(store, run_id) == int(ExitCode.cancelled)
    assert raced is True
    assert store.run(run_id).state == RunState.cancelled
