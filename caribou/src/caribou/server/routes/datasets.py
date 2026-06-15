from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile

from caribou.config import CARIBOU_HOME
from caribou.server.models import DatasetPathValidationRequest, DatasetRecord

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

_UPLOADS_DIR = CARIBOU_HOME / "server_uploads"
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Package sample datasets bundled with CARIBOU
_PACKAGE_DATASETS_DIR = Path(__file__).resolve().parent.parent.parent / "datasets"

# User datasets downloaded via `caribou datasets`
_USER_DATASETS_DIR = CARIBOU_HOME / "datasets"

_PATH_VALIDATION_ERROR = "Dataset path must be an absolute readable .h5ad file."
_UPLOAD_VALIDATION_ERROR = "Uploaded dataset must be a .h5ad file."


def _scan_dir(directory: Path, deletable: bool) -> List[DatasetRecord]:
    records = []
    if not directory.exists():
        return records
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.suffix == ".h5ad":
            records.append(DatasetRecord(
                filename=f.name,
                path=str(f),
                size_bytes=f.stat().st_size,
                uploaded_at=datetime.fromtimestamp(f.stat().st_mtime),
            ))
    return records


def _record_for_hpc_path(raw_path: str) -> DatasetRecord:
    path_text = raw_path.strip()
    if not path_text or "\x00" in path_text:
        raise HTTPException(400, _PATH_VALIDATION_ERROR)

    path = Path(path_text)
    if not path.is_absolute() or path.suffix != ".h5ad":
        raise HTTPException(400, _PATH_VALIDATION_ERROR)

    try:
        file_stat = path.stat()
    except (OSError, ValueError):
        raise HTTPException(400, _PATH_VALIDATION_ERROR)

    if not stat.S_ISREG(file_stat.st_mode) or not os.access(path, os.R_OK):
        raise HTTPException(400, _PATH_VALIDATION_ERROR)

    try:
        with path.open("rb"):
            pass
    except OSError:
        raise HTTPException(400, _PATH_VALIDATION_ERROR)

    return DatasetRecord(
        filename=path.name,
        path=str(path),
        size_bytes=file_stat.st_size,
        uploaded_at=datetime.fromtimestamp(file_stat.st_mtime),
    )


@router.post("", response_model=DatasetRecord, status_code=201)
async def upload_dataset(file: UploadFile) -> DatasetRecord:
    if not file.filename:
        raise HTTPException(400, "No filename provided")
    safe_name = Path(file.filename).name
    if safe_name != file.filename or safe_name in ("", ".", "..") or Path(safe_name).suffix != ".h5ad":
        raise HTTPException(400, _UPLOAD_VALIDATION_ERROR)
    dest = _UPLOADS_DIR / safe_name
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception as exc:
        raise HTTPException(500, f"Upload failed: {exc}")
    return DatasetRecord(
        filename=dest.name,
        path=str(dest),
        size_bytes=dest.stat().st_size,
        uploaded_at=datetime.utcnow(),
    )


@router.post("/validate-path", response_model=DatasetRecord)
async def validate_dataset_path(body: DatasetPathValidationRequest) -> DatasetRecord:
    return _record_for_hpc_path(body.path)


@router.get("", response_model=List[DatasetRecord])
async def list_datasets() -> List[DatasetRecord]:
    # Package samples first, then user-downloaded, then uploaded — dedup by path
    seen: set[str] = set()
    records: List[DatasetRecord] = []
    for record in (
        _scan_dir(_PACKAGE_DATASETS_DIR, deletable=False)
        + _scan_dir(_USER_DATASETS_DIR, deletable=False)
        + _scan_dir(_UPLOADS_DIR, deletable=True)
    ):
        if record.path not in seen:
            seen.add(record.path)
            records.append(record)
    return records


@router.delete("/{filename}", status_code=204)
async def delete_dataset(filename: str) -> None:
    # Only allow deleting from uploads dir, not package or user-downloaded datasets
    target = _UPLOADS_DIR / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Dataset not found in uploads — package and downloaded datasets cannot be deleted via the API")
    target.unlink()
