"""Opt-in integration for real AnnData checkpoint capture and restore in the SIF."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from caribou.control.agent_workload import (
    SANDBOX_DATA_PATH,
    _CHECKPOINT_DATASET_CAPTURE_CODE,
    _CHECKPOINT_DATASET_FILENAME,
    _CHECKPOINT_DATASET_RESTORE_CODE,
    _real_sandbox,
)


_CONTAINER = os.environ.get("CARIBOU_CHECKPOINT_CONTAINER_PATH")
_CONTAINER_HASH = os.environ.get("CARIBOU_CHECKPOINT_CONTAINER_HASH")
_DATASET = os.environ.get("CARIBOU_CHECKPOINT_DATASET_PATH")

pytestmark = pytest.mark.skipif(
    not (_CONTAINER and _CONTAINER_HASH and _DATASET),
    reason="real checkpoint SIF, hash, and AnnData path were not supplied",
)


def _sandbox(container: Path, content_hash: str):
    return _real_sandbox(
        container,
        content_hash,
        Console(file=StringIO(), force_terminal=False),
        gpu_enabled=False,
    )


def test_mutated_anndata_is_captured_and_restored_in_fresh_repl(
    tmp_path: Path,
) -> None:
    assert _CONTAINER is not None
    assert _CONTAINER_HASH is not None
    assert _DATASET is not None
    container = Path(_CONTAINER).resolve()
    dataset = Path(_DATASET).resolve()
    assert container.is_file()
    assert dataset.is_file()

    capture_output = tmp_path / "capture"
    capture = _sandbox(container, _CONTAINER_HASH)
    capture.set_data([(dataset, SANDBOX_DATA_PATH)], capture_output)
    try:
        assert capture.start_container() is True
        mutation = capture.exec_code(
            """
import anndata as _caribou_anndata
adata = _caribou_anndata.read_h5ad('/workspace/dataset.h5ad')
adata.obs['caribou_checkpoint_probe'] = 'preserved-after-resume'
print(f'MUTATED {adata.n_obs} {adata.n_vars}')
""",
            timeout=600,
        )
        assert mutation["status"] == "ok", mutation
        captured = capture.exec_code(_CHECKPOINT_DATASET_CAPTURE_CODE, timeout=600)
        assert captured["status"] == "ok", captured
    finally:
        capture.stop_container()

    checkpoint_dataset = capture_output / f".{_CHECKPOINT_DATASET_FILENAME}"
    assert checkpoint_dataset.is_file()
    assert checkpoint_dataset.stat().st_size > 0

    restore_output = tmp_path / "restore"
    restore = _sandbox(container, _CONTAINER_HASH)
    restore.set_data([(checkpoint_dataset, SANDBOX_DATA_PATH)], restore_output)
    try:
        assert restore.start_container() is True
        restored = restore.exec_code(_CHECKPOINT_DATASET_RESTORE_CODE, timeout=600)
        assert restored["status"] == "ok", restored
        verified = restore.exec_code(
            """
import json as _caribou_json
from pathlib import Path as _CaribouPath
_caribou_values = sorted(set(adata.obs['caribou_checkpoint_probe'].tolist()))
_CaribouPath('/workspace/outputs/restored-state.json').write_text(
    _caribou_json.dumps({
        'n_obs': int(adata.n_obs),
        'n_vars': int(adata.n_vars),
        'probe_values': _caribou_values,
    }, sort_keys=True),
    encoding='utf-8',
)
print('RESTORE_VERIFIED')
""",
            timeout=600,
        )
        assert verified["status"] == "ok", verified
    finally:
        restore.stop_container()

    payload = json.loads((restore_output / "restored-state.json").read_text())
    assert payload["n_obs"] > 0
    assert payload["n_vars"] > 0
    assert payload["probe_values"] == ["preserved-after-resume"]
