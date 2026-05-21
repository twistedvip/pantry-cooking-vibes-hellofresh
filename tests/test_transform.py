"""Tests for the raw-HF-scrape -> contract-JSONL pre-transform."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantry_cooking_vibes_hellofresh.transform import main, transform_jsonl


def _hf_entity(
    name: str,
    ingredients: list[str],
    *,
    instructions: list[str] | None = None,
    image: str = "https://img.test/recipe.jpg",
) -> dict:
    return {
        "@type": "Recipe",
        "name": name,
        "totalTime": "PT30M",
        "recipeYield": 4,
        "recipeIngredient": ingredients,
        "recipeInstructions": [
            {"@type": "HowToStep", "text": s} for s in (instructions or ["Cook it."])
        ],
        "recipeCategory": "main course",
        "recipeCuisine": "italian",
        "image": image,
        "aggregateRating": {"ratingValue": "4.2", "ratingCount": "12"},
        "nutrition": {"@type": "NutritionInformation", "calories": 550},
    }


SAMPLE_RAW = [
    {
        "url": "https://hf.test/recipes/alpha",
        "entity": _hf_entity(
            "[PROTEIN DOUBLE] Salmon Bowl | 2 Servings",
            ["1 cup rice", "2 fillets salmon"],
        ),
    },
    {
        "url": "https://hf.test/recipes/beta",
        "entity": _hf_entity("MA/CA only Compliant Pork Chop", ["1 chop", "salt", "pepper"]),
    },
    # Sparse (≤1 ingredient): dropped by adapter.
    {
        "url": "https://hf.test/recipes/stub",
        "entity": _hf_entity("Stub", ["only thing"]),
    },
    # Malformed line shape (no entity): dropped by adapter.
    {"url": "https://hf.test/recipes/noentity"},
]


def _write_raw(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def raw_path(tmp_path: Path) -> Path:
    p = tmp_path / "raw.jsonl"
    _write_raw(p, SAMPLE_RAW)
    return p


def test_transform_writes_contract_jsonl(raw_path: Path, tmp_path: Path):
    dst = tmp_path / "out.jsonl"
    stats = transform_jsonl(raw_path, dst, quiet=True)

    assert stats == {"processed": 4, "written": 2, "dropped": 2, "malformed": 0}

    lines = dst.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["source_id"] == "https://hf.test/recipes/alpha"
    assert parsed[0]["name"] == "Salmon Bowl"  # editorial markers stripped
    assert parsed[0]["tags"] == ["main course", "italian"]
    assert parsed[1]["name"] == "Pork Chop"  # regional flag stripped


def test_transform_round_trips_through_ingest(raw_path: Path, tmp_path: Path, db_path):
    """Pre-transformed JSONL ingests cleanly without --plugin."""
    from pantry_cooking_vibes.db import connect
    from pantry_cooking_vibes.importers.jsonl_ingest import ingest_jsonl

    dst = tmp_path / "contract.jsonl"
    transform_jsonl(raw_path, dst, quiet=True)

    stats = ingest_jsonl(dst, source="hellofresh", db_path=db_path, quiet=True)
    assert stats["recipes"] == 2
    assert stats["skipped"] == 0

    with connect(db_path) as conn:
        names = sorted(r["name"] for r in conn.execute("SELECT name FROM recipes"))
    assert names == ["Pork Chop", "Salmon Bowl"]


def test_transform_handles_malformed_json(tmp_path: Path):
    src = tmp_path / "raw.jsonl"
    good = {"url": "https://hf.test/recipes/a", "entity": _hf_entity("Alpha", ["a", "b"])}
    src.write_text(
        json.dumps(good) + "\n" + "not-json\n" + json.dumps(good) + "\n",
        encoding="utf-8",
    )
    dst = tmp_path / "out.jsonl"
    stats = transform_jsonl(src, dst, quiet=True)
    assert stats == {"processed": 3, "written": 2, "dropped": 0, "malformed": 1}


def test_transform_missing_src_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        transform_jsonl(tmp_path / "nope.jsonl", tmp_path / "out.jsonl", quiet=True)


def test_cli_main_returns_zero(raw_path: Path, tmp_path: Path, capsys):
    dst = tmp_path / "out.jsonl"
    rc = main([str(raw_path), str(dst), "--quiet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "transform done" in out
    assert dst.exists()


def test_cli_main_missing_src_returns_one(tmp_path: Path, capsys):
    rc = main([str(tmp_path / "nope.jsonl"), str(tmp_path / "out.jsonl"), "--quiet"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "input not found" in err
