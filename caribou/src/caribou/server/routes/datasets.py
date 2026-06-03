from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile

from caribou.config import CARIBOU_HOME
from caribou.server.models import DatasetRecord

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

_UPLOADS_DIR = CARIBOU_HOME / "server_uploads"
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Package sample datasets bundled with CARIBOU
_PACKAGE_DATASETS_DIR = Path(__file__).resolve().parent.parent.parent / "datasets"

# User datasets downloaded via `caribou datasets`
_USER_DATASETS_DIR = CARIBOU_HOME / "datasets"


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


@router.post("", response_model=DatasetRecord, status_code=201)
async def upload_dataset(file: UploadFile) -> DatasetRecord:
    if not file.filename:
        raise HTTPException(400, "No filename provided")
    dest = _UPLOADS_DIR / file.filename
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
