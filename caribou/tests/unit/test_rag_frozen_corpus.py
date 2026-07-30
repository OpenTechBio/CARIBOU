from __future__ import annotations

import json
from pathlib import Path

import pytest

from caribou.rag.RetrievalAugmentedGeneration import (
    FROZEN_CORPUS_ENV,
    RetrievalAugmentedGeneration,
)


def _write_corpus(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "caribou.rag_corpus.v1",
                "entries": [
                    {
                        "title": "large intestine marker panel",
                        "keywords": ["cell typing", "tuft", "mast"],
                        "content": "POU2F3 supports tuft cells; TPSAB1 supports mast cells.",
                    },
                    {
                        "title": "quality control",
                        "keywords": ["normalization", "counts"],
                        "content": "Preserve counts before log normalization.",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_frozen_corpus_retrieval_is_offline_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    monkeypatch.setenv(FROZEN_CORPUS_ENV, str(corpus_path))

    retriever = RetrievalAugmentedGeneration()

    assert retriever.model is None
    assert retriever.model_loaded is False
    assert retriever.query("tuft cell typing markers") == (
        "POU2F3 supports tuft cells; TPSAB1 supports mast cells."
    )
    assert retriever.query("unrelated words") is None


def test_frozen_corpus_rejects_invalid_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_path = tmp_path / "invalid.json"
    corpus_path.write_text('{"schema_version":"wrong","entries":[]}\n')
    monkeypatch.setenv(FROZEN_CORPUS_ENV, str(corpus_path))

    with pytest.raises(ValueError, match="unsupported schema"):
        RetrievalAugmentedGeneration()
