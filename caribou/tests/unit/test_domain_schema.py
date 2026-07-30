"""Ensure checked-in consumer schemas cannot drift from canonical models."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from caribou.domain.schema import SCHEMA_MODELS, schema_document

from .test_domain_models import make_spec, top_level_records


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "domain" / "v1"


def test_all_domain_schemas_are_checked_in_and_current() -> None:
    expected_names = {f"{name}.schema.json" for name in SCHEMA_MODELS}
    assert {path.name for path in SCHEMA_ROOT.glob("*.schema.json")} == expected_names
    for name, model_type in SCHEMA_MODELS.items():
        path = SCHEMA_ROOT / f"{name}.schema.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored == schema_document(name, model_type), f"schema drift: {path}"
        assert stored["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert stored["additionalProperties"] is False


def test_json_schema_and_python_reject_the_same_boundary_errors() -> None:
    schema = schema_document("experiment-spec", SCHEMA_MODELS["experiment-spec"])
    validator = jsonschema.Draft202012Validator(schema)
    valid = make_spec().model_dump(mode="json")
    validator.validate(valid)
    invalid_documents = []
    future = json.loads(json.dumps(valid))
    future["schema_version"] = "caribou.experiment_spec.v99"
    invalid_documents.append(future)
    extra = json.loads(json.dumps(valid))
    extra["conditions"][0]["unknown"] = True
    invalid_documents.append(extra)
    coercion = json.loads(json.dumps(valid))
    coercion["repetitions"] = "5"
    invalid_documents.append(coercion)
    for document in invalid_documents:
        assert list(validator.iter_errors(document))


def test_every_top_level_example_validates_with_its_consumer_json_schema() -> None:
    records = top_level_records()
    assert set(records) == set(SCHEMA_MODELS)
    for name, record in records.items():
        schema = schema_document(name, SCHEMA_MODELS[name])
        validator = jsonschema.Draft202012Validator(schema)
        document = record.model_dump(mode="json")
        validator.validate(document)
        document["schema_version"] = "caribou.unsupported.v99"
        document["unknown_field"] = True
        errors = list(validator.iter_errors(document))
        assert errors, f"schema accepted future/unknown fields for {name}"
