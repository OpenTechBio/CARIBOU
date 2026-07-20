"""Web acceptance journey over the shared durable experiment control plane."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

import caribou.control.agent_workload as agent_workload
from caribou.control.records import CancelRequest
from caribou.control.service import ExperimentService
from caribou.control.specs import ADAPTER_PARAMETER, LOCAL_LIFECYCLE_ADAPTER
from caribou.control.store import ExperimentStore
from caribou.control.worker import execute as execute_worker
from caribou.domain.enums import InterfaceOrigin, RunState
from caribou.domain.models import ExperimentSpec
from caribou.domain.serialization import read_model
from caribou.server.routes.experiments import (
    CheckpointCreationRequest,
    ExperimentSubmissionRequest,
    RunCancellationRequest,
    RunResumeRequest,
    _call,
    cancel_run,
    checkpoint_run,
    control_http_exception_response,
    download_artifact,
    list_artifacts,
    list_checkpoints,
    request_validation_error_response,
    require_control_access,
    resume_run,
    run_events,
    run_status,
    submit_experiment,
    verify_artifacts,
)

from ..unit.test_control_checkpoint_workload import _smoke_spec
from ..unit.test_domain_models import make_spec


class DeferredExecutor:
    """Keep submitted runs queued so the test can control worker boundaries."""

    def launch(self, _store: ExperimentStore, _run_id: str) -> SimpleNamespace:
        return SimpleNamespace(launched=False)


def _lifecycle_spec(*, seconds: float = 0.0) -> ExperimentSpec:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={
            "parameters": {
                ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER,
                "caribou.lifecycle_smoke_seconds": seconds,
            }
        }
    )
    return ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [condition], "repetitions": 1}
        ).model_dump_json()
    )


@pytest.fixture
def control_service(tmp_path: Path) -> ExperimentService:
    store = ExperimentStore(tmp_path / "web-control-store")
    return ExperimentService(store=store, executor=DeferredExecutor())  # type: ignore[arg-type]


def _payload(response: JSONResponse) -> dict:
    return json.loads(response.body)


def _submit(service: ExperimentService, spec: ExperimentSpec, key: str) -> dict:
    response = submit_experiment(
        ExperimentSubmissionRequest(
            specification=spec,
            idempotency_key=key,
            expected_plan_hash=None,
        ),
        service,
    )
    assert response.status_code == 202, response.body
    return _payload(response)


def test_web_submit_status_events_and_verified_artifact_use_shared_store(
    control_service: ExperimentService,
) -> None:
    service = control_service
    specification = _lifecycle_spec()
    submitted = _submit(service, specification, "web-lifecycle-success")
    run_id = submitted["data"]["run_ids"][0]

    persisted = service.store.run(run_id)
    assert persisted.interface == InterfaceOrigin.web
    assert submitted["data"]["runs"][0]["interface"] == "web"
    assert submitted["data"]["workers_launched"] == 0

    assert execute_worker(service.store, run_id) == 0
    status = run_status(run_id, service)
    assert status.status_code == 200
    snapshot = _payload(status)
    assert snapshot["data"]["run"]["state"] == "succeeded"
    assert snapshot["data"]["run"]["interface"] == "web"
    assert snapshot["caribou"]["commit"]

    events = _payload(run_events(run_id, after=0, limit=1000, service=service))
    cursor = events["data"]["next_cursor"]
    assert [event["sequence"] for event in events["data"]["events"]] == list(
        range(1, cursor + 1)
    )
    assert events["data"]["has_more"] is False
    assert (
        _payload(run_events(run_id, after=cursor, limit=1000, service=service))["data"][
            "events"
        ]
        == []
    )

    listed = _payload(list_artifacts(run_id, service))
    assert listed["data"]["count"] == 1
    artifact = listed["data"]["artifacts"][0]
    verified = _payload(verify_artifacts(run_id, service))
    assert verified["data"] == {
        "verified": 1,
        "artifact_ids": [artifact["artifact_id"]],
    }

    downloaded = download_artifact(run_id, artifact["artifact_id"], service)
    assert isinstance(downloaded, FileResponse)
    assert downloaded.status_code == 200
    assert downloaded.headers["x-caribou-run-id"] == run_id
    assert downloaded.headers["x-caribou-content-hash"] == artifact["content_hash"]
    assert json.loads(Path(downloaded.path).read_bytes())["run_id"] == run_id

    replay = _submit(service, specification, "web-lifecycle-success")
    assert replay["data"]["run_ids"] == [run_id]
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["workers_launched"] == 0


def test_web_checkpoint_resume_and_cancel_preserve_interface_and_actor(
    control_service: ExperimentService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = control_service
    spec = _smoke_spec(tmp_path)
    submitted = _submit(service, spec, "web-checkpoint-source")
    source_id = submitted["data"]["run_ids"][0]

    checkpoint_response = checkpoint_run(
        source_id,
        CheckpointCreationRequest(
            idempotency_key="web-checkpoint-request",
            reason="web acceptance checkpoint",
        ),
        service,
    )
    assert checkpoint_response.status_code == 200, checkpoint_response.body
    assert _payload(checkpoint_response)["data"]["request"]["actor"] == "web"
    assert _payload(checkpoint_response)["data"]["applied"] is True

    monkeypatch.setattr(
        agent_workload,
        "_verify_code_identity",
        lambda _expected_commit, _adapter, **_kwargs: None,
    )
    assert execute_worker(service.store, source_id) == 0
    assert service.store.run(source_id).state == RunState.resumable

    checkpoints = _payload(list_checkpoints(source_id, service))["data"]["checkpoints"]
    assert len(checkpoints) == 1
    checkpoint_id = checkpoints[0]["checkpoint_id"]

    resumed = resume_run(
        source_id,
        RunResumeRequest(
            checkpoint_id=checkpoint_id,
            idempotency_key="web-checkpoint-resume",
        ),
        service,
    )
    assert resumed.status_code == 202, resumed.body
    child_id = _payload(resumed)["data"]["child_run"]["run_id"]
    child = service.store.run(child_id)
    assert child.interface == InterfaceOrigin.web
    assert child.resumed_from_run_id == source_id
    assert child.resume_checkpoint_id == checkpoint_id
    cross_interface_replay = service.resume(
        source_id,
        checkpoint_id=checkpoint_id,
        idempotency_key="web-checkpoint-resume",
        interface=InterfaceOrigin.cli,
    )
    assert cross_interface_replay.submission.idempotent_replay is True
    assert cross_interface_replay.submission.child.interface == InterfaceOrigin.web
    assert execute_worker(service.store, child_id) == 0
    assert _payload(run_status(child_id, service))["object"]["state"] == "succeeded"

    cancel_submitted = _submit(service, _lifecycle_spec(seconds=2.0), "web-cancel")
    cancel_id = cancel_submitted["data"]["run_ids"][0]
    cancelled = cancel_run(
        cancel_id,
        RunCancellationRequest(reason="web acceptance cancellation"),
        service,
    )
    assert cancelled.status_code == 200
    assert _payload(cancelled)["data"]["applied"] is True
    assert _payload(cancelled)["object"]["state"] == "cancelling"
    cancel_request = read_model(
        service.store.cancel_request_path(cancel_id),
        CancelRequest,
    )
    assert cancel_request.actor == "web"
    assert execute_worker(service.store, cancel_id) == 17
    assert service.store.run(cancel_id).state == RunState.cancelled


def test_web_control_errors_keep_machine_contract(
    control_service: ExperimentService,
) -> None:
    missing_id = "run_" + "0" * 32
    response = run_status(missing_id, control_service)
    assert response.status_code == 404
    payload = _payload(response)
    assert payload["schema_version"] == "caribou.machine_response.v1"
    assert payload["command"] == "run.status"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "RUN_NOT_FOUND"


def test_control_access_is_default_off_and_requires_exact_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "caribou.core.control_access.ENV_FILE",
        tmp_path / "missing.env",
    )
    monkeypatch.delenv("CARIBOU_CONTROL_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as disabled:
        require_control_access(None)
    assert disabled.value.status_code == 503
    disabled_payload = disabled.value.detail
    assert disabled_payload["error"]["code"] == "CONTROL_API_DISABLED"

    monkeypatch.setenv("CARIBOU_CONTROL_API_TOKEN", "test-control-token")
    with pytest.raises(HTTPException) as wrong:
        require_control_access("Bearer wrong-token")
    assert wrong.value.status_code == 401
    assert wrong.value.detail["error"]["code"] == "CONTROL_API_UNAUTHORIZED"
    assert wrong.value.headers == {"WWW-Authenticate": "Bearer"}

    assert require_control_access("Bearer test-control-token") is None


def test_request_validation_error_response_is_sanitized_machine_contract() -> None:
    error = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "idempotency_key"),
                "msg": "Field required",
                "input": {"authorization": "Bearer must-not-leak"},
            }
        ]
    )

    response = request_validation_error_response(error)
    payload = _payload(response)
    serialized = response.body.decode("utf-8")

    assert response.status_code == 422
    assert payload["schema_version"] == "caribou.machine_response.v1"
    assert payload["command"] == "control.request"
    assert payload["error"]["code"] == "REQUEST_INVALID"
    assert payload["error"]["details"] == {
        "issues": [
            {
                "location": ["body", "idempotency_key"],
                "message": "Field required",
                "type": "missing",
            }
        ]
    }
    assert "must-not-leak" not in serialized


def test_unexpected_route_exception_is_sanitized() -> None:
    def fail() -> dict:
        raise RuntimeError("provider secret must-not-leak")

    response = _call("test.failure", fail)
    payload = _payload(response)

    assert response.status_code == 500
    assert payload["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "CARIBOU could not complete the request",
        "retryable": False,
        "details": {"exception_type": "RuntimeError"},
    }
    assert "must-not-leak" not in response.body.decode("utf-8")


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (404, "CONTROL_ROUTE_NOT_FOUND"),
        (405, "CONTROL_METHOD_NOT_ALLOWED"),
    ],
)
def test_control_routing_errors_are_sanitized_machine_responses(
    status_code: int,
    expected_code: str,
) -> None:
    response = control_http_exception_response(
        HTTPException(status_code=status_code, detail="route secret must-not-leak")
    )
    payload = _payload(response)

    assert response.status_code == status_code
    assert payload["schema_version"] == "caribou.machine_response.v1"
    assert payload["command"] == "control.request"
    assert payload["error"]["code"] == expected_code
    assert payload["error"]["details"] == {"status_code": status_code}
    assert "must-not-leak" not in response.body.decode("utf-8")


def test_frontend_control_client_keeps_token_session_scoped_and_headers_all_calls() -> (
    None
):
    repository_root = Path(__file__).resolve().parents[3]
    service_source = (
        repository_root / "frontend/src/app/core/services/experiment-control.service.ts"
    ).read_text(encoding="utf-8")
    template_source = (
        repository_root / "frontend/src/app/pages/experiments/experiments.html"
    ).read_text(encoding="utf-8")

    assert "sessionStorage.setItem" in service_source
    assert "sessionStorage.getItem" in service_source
    assert "sessionStorage.removeItem" in service_source
    assert "localStorage" not in service_source
    assert service_source.count("this.http.get") + service_source.count(
        "this.http.post"
    ) == service_source.count("this.authorizationHeaders()")
    assert "responseType: 'blob'" in service_source
    assert "artifactDownloadUrl" not in service_source
    assert "?token=" not in service_source
    assert "?access_token=" not in service_source
    assert '(click)="downloadArtifact(artifact)"' in template_source
    assert '[href]="artifactUrl(artifact)"' not in template_source
