from pathlib import Path

import pytest
from fastapi import HTTPException

from caribou.server.routes.datasets import _record_for_hpc_path


def test_record_for_hpc_path_accepts_readable_h5ad(tmp_path: Path):
    dataset = tmp_path / "dataset.h5ad"
    dataset.write_bytes(b"h5ad")

    record = _record_for_hpc_path(str(dataset))

    assert record.filename == "dataset.h5ad"
    assert record.path == str(dataset)
    assert record.size_bytes == 4


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative/dataset.h5ad",
        "/tmp/dataset.txt",
        "/tmp/dataset.H5AD",
        "/tmp/dataset.h5ad\x00",
    ],
)
def test_record_for_hpc_path_rejects_invalid_path_text(path: str):
    with pytest.raises(HTTPException) as exc:
        _record_for_hpc_path(path)

    assert exc.value.status_code == 400


def test_record_for_hpc_path_rejects_missing_file(tmp_path: Path):
    with pytest.raises(HTTPException) as exc:
        _record_for_hpc_path(str(tmp_path / "missing.h5ad"))

    assert exc.value.status_code == 400


def test_record_for_hpc_path_rejects_directory(tmp_path: Path):
    directory = tmp_path / "directory.h5ad"
    directory.mkdir()

    with pytest.raises(HTTPException) as exc:
        _record_for_hpc_path(str(directory))

    assert exc.value.status_code == 400
