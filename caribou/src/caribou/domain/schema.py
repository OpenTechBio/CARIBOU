"""JSON Schema export for versioned CARIBOU domain contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Type

from pydantic import BaseModel

from .models import (
    Aggregate,
    Artifact,
    BudgetRecord,
    Checkpoint,
    Event,
    Experiment,
    ExperimentSpec,
    ExperimentTransitionRecord,
    FailureRecord,
    MetricRecord,
    Run,
)
from .migrations import MigrationReport
from .serialization import ExperimentJournal, RunJournal

SCHEMA_MODELS: Dict[str, Type[BaseModel]] = {
    "experiment-spec": ExperimentSpec,
    "experiment": Experiment,
    "run": Run,
    "event": Event,
    "artifact": Artifact,
    "failure": FailureRecord,
    "metric": MetricRecord,
    "checkpoint": Checkpoint,
    "budget": BudgetRecord,
    "aggregate": Aggregate,
    "migration-report": MigrationReport,
    "run-journal": RunJournal,
    "experiment-transition": ExperimentTransitionRecord,
    "experiment-journal": ExperimentJournal,
}


def schema_document(name: str, model_type: Type[BaseModel]) -> dict:
    schema = model_type.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://caribou.dev/schemas/domain/v1/{name}.schema.json"
    return schema


def export_schemas(output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, model_type in SCHEMA_MODELS.items():
        document = schema_document(name, model_type)
        target = output_directory / f"{name}.schema.json"
        target.write_text(
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    export_schemas(Path(__file__).resolve().parents[3] / "schemas" / "domain" / "v1")
