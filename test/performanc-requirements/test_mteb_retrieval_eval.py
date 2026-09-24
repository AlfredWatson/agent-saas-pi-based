"""Checks for sample integrity and retrieval metric definitions."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_mteb  # noqa: E402
from mteb_retrieval_eval import aggregate_scores, score_ranking  # noqa: E402


class CharacterTokenizer:
    def encode(self, text: str):
        return type("Encoding", (), {"ids": list(text)})()


def test_metrics_use_all_relevant_labels_and_fixed_precision_denominator() -> None:
    scored = score_ranking(["other", "right-1", "right-2"], {"right-1", "right-2"})
    assert scored[5] == {
        "recall": 1.0,
        "precision": 2 / 5,
        "hit_rate": 1.0,
        "mrr": 1 / 2,
    }
    assert scored[20]["precision"] == 2 / 20
    assert (
        aggregate_scores([scored, score_ranking([], {"right-1"})])[5]["hit_rate"] == 0.5
    )
    with pytest.raises(ValueError, match="duplicate"):
        score_ranking(["right-1", "right-1"], {"right-1"})


def test_preparation_keeps_all_positives_and_one_chunk_per_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "EcomRetrieval"
    source.mkdir()
    tables = {
        "queries": [
            {"_id": "q1", "text": "Question one"},
            {"_id": "q2", "text": "Question two"},
        ],
        "corpus": [
            {"_id": "a", "title": "", "text": "First answer"},
            {"_id": "b", "title": "", "text": "# embedded heading\nSecond answer"},
            {"_id": "c", "title": "", "text": "Third answer"},
            {"_id": "d", "title": "", "text": "Fourth answer"},
        ],
        "data": [
            {"query-id": "q1", "corpus-id": "a", "score": 1},
            {"query-id": "q1", "corpus-id": "b", "score": 1},
            {"query-id": "q2", "corpus-id": "c", "score": 1},
        ],
    }
    monkeypatch.setattr(
        prepare_mteb, "read_parquet", lambda path, columns: tables[path.name]
    )
    output = tmp_path / "prepared"
    details = prepare_mteb.prepare_dataset(
        source,
        output,
        tokenizer=CharacterTokenizer(),
        max_tokens=100,
        query_count=2,
        corpus_count=4,
        seed=42,
        items_per_file=2,
    )
    assert details["query_count"] == 2
    assert details["corpus_count"] == 4
    with (output / "corpus_manifest.csv").open(encoding="utf-8") as handle:
        assert {row["corpus_id"] for row in csv.DictReader(handle)} == {
            "a",
            "b",
            "c",
            "d",
        }
    assert "\\# embedded heading" in (output / "corpus-0000.md").read_text(
        encoding="utf-8"
    ) or "\\# embedded heading" in (output / "corpus-0001.md").read_text(
        encoding="utf-8"
    )
