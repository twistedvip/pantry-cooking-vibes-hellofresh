"""Tests for the HelloFresh sitemap scraper and JSONL importer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pantry_cooking_vibes.db import connect

from pantry_cooking_vibes_hellofresh.import_legacy import (
    _clean_recipe_name,
    clean_existing_names,
    import_recipes,
)
from pantry_cooking_vibes_hellofresh.scraper import (
    _parse_sitemap,
    discover_urls,
    scrape_recipes,
)

SAMPLE_SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.hellofresh.com/recipes/aaa-1</loc></url>
  <url><loc>https://www.hellofresh.com/recipes/bbb-2</loc></url>
  <url><loc>https://www.hellofresh.com/recipes/ccc-3</loc></url>
</urlset>
"""


def _recipe_html(name: str, ingredients: list[str], steps: list[str]) -> str:
    """Build a minimal page with a single Recipe JSON-LD block."""
    entity = {
        "@context": "https://schema.org/",
        "@type": "Recipe",
        "name": name,
        "totalTime": "PT30M",
        "recipeYield": 2,
        "recipeIngredient": ingredients,
        "recipeInstructions": [{"@type": "HowToStep", "text": s} for s in steps],
        "recipeCategory": "main course",
        "recipeCuisine": "italian",
        "image": f"https://img.example.com/{name}.jpg",
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.2",
            "ratingCount": "12",
        },
        "nutrition": {"@type": "NutritionInformation", "calories": "550 kcal"},
    }
    return (
        "<!doctype html><html><body>"
        '<script type="application/ld+json">' + json.dumps(entity) + "</script></body></html>"
    )


# ---------- sitemap parsing ----------


def test_parse_sitemap_extracts_locs_in_order():
    urls = _parse_sitemap(SAMPLE_SITEMAP)
    assert urls == [
        "https://www.hellofresh.com/recipes/aaa-1",
        "https://www.hellofresh.com/recipes/bbb-2",
        "https://www.hellofresh.com/recipes/ccc-3",
    ]


def test_parse_sitemap_handles_extra_whitespace():
    xml = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>  https://x.test/a  </loc></url>"
        "</urlset>"
    )
    assert _parse_sitemap(xml) == ["https://x.test/a"]


def test_discover_urls_uses_session():
    session = MagicMock()
    session.get.return_value = MagicMock(text=SAMPLE_SITEMAP, raise_for_status=lambda: None)
    urls = discover_urls("https://x.test/sitemap.xml", session=session)
    assert len(urls) == 3
    session.get.assert_called_once()


# ---------- scrape loop ----------


class _FakeSession:
    """Minimal session double: maps url -> (status, text). Skips real HTTP."""

    def __init__(self, pages: dict[str, tuple[int, str]]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: int = 0) -> MagicMock:
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"unexpected URL: {url}")
        status, text = self.pages[url]
        resp = MagicMock()
        resp.text = text
        if 200 <= status < 300:
            resp.raise_for_status = lambda: None
        else:
            err = Exception(f"HTTP {status}")
            resp.raise_for_status = MagicMock(side_effect=err)
        return resp


def test_scrape_recipes_writes_jsonl_and_state(tmp_path: Path):
    urls = ["https://hf.test/recipes/a", "https://hf.test/recipes/b"]
    pages = {
        urls[0]: (200, _recipe_html("Alpha", ["1 cup flour"], ["Mix."])),
        urls[1]: (200, _recipe_html("Beta", ["2 eggs"], ["Whisk."])),
    }
    out = tmp_path / "recipes.jsonl"
    state = tmp_path / "state.json"

    written = scrape_recipes(
        out_path=out,
        state_path=state,
        sleep=0,
        urls=urls,
        session=_FakeSession(pages),
        verbose=False,
    )

    assert written == 2
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["url"] == urls[0]
    assert first["entity"]["name"] == "Alpha"
    assert first["entity"]["recipeIngredient"] == ["1 cup flour"]

    saved = json.loads(state.read_text())
    assert saved["next_index"] == 2
    assert saved["total_written"] == 2


def test_scrape_recipes_skips_pages_without_jsonld(tmp_path: Path):
    urls = ["https://hf.test/recipes/has-recipe", "https://hf.test/recipes/blank"]
    pages = {
        urls[0]: (200, _recipe_html("Alpha", ["1 cup flour"], ["Mix."])),
        urls[1]: (200, "<html><body>no recipe here</body></html>"),
    }
    out = tmp_path / "recipes.jsonl"
    state = tmp_path / "state.json"

    written = scrape_recipes(
        out_path=out,
        state_path=state,
        sleep=0,
        urls=urls,
        session=_FakeSession(pages),
        verbose=False,
    )

    assert written == 1
    saved = json.loads(state.read_text())
    assert saved["next_index"] == 2


def test_scrape_recipes_resumes_from_state(tmp_path: Path):
    urls = ["https://hf.test/recipes/a", "https://hf.test/recipes/b"]
    pages = {
        urls[0]: (200, _recipe_html("Alpha", ["x"], ["s"])),
        urls[1]: (200, _recipe_html("Beta", ["y"], ["t"])),
    }
    out = tmp_path / "recipes.jsonl"
    state = tmp_path / "state.json"
    out.write_text(
        json.dumps({"url": urls[0], "entity": {"name": "Alpha"}}) + "\n", encoding="utf-8"
    )
    state.write_text(json.dumps({"next_index": 1, "total_written": 1}), encoding="utf-8")

    session = _FakeSession(pages)
    written = scrape_recipes(
        out_path=out,
        state_path=state,
        sleep=0,
        urls=urls,
        session=session,
        verbose=False,
    )

    assert written == 1
    assert session.calls == [urls[1]]
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_scrape_recipes_max_recipes_limit(tmp_path: Path):
    urls = ["https://hf.test/recipes/a", "https://hf.test/recipes/b", "https://hf.test/recipes/c"]
    pages = {u: (200, _recipe_html(u[-1], ["x"], ["s"])) for u in urls}

    written = scrape_recipes(
        out_path=tmp_path / "out.jsonl",
        state_path=tmp_path / "state.json",
        sleep=0,
        max_recipes=2,
        urls=urls,
        session=_FakeSession(pages),
        verbose=False,
    )

    assert written == 2
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["next_index"] == 2


def test_scrape_recipes_advances_past_http_errors(tmp_path: Path):
    urls = ["https://hf.test/recipes/a", "https://hf.test/recipes/b"]
    pages = {
        urls[0]: (404, ""),
        urls[1]: (200, _recipe_html("Beta", ["y"], ["t"])),
    }
    out = tmp_path / "out.jsonl"
    state = tmp_path / "state.json"

    written = scrape_recipes(
        out_path=out,
        state_path=state,
        sleep=0,
        urls=urls,
        session=_FakeSession(pages),
        verbose=False,
    )

    assert written == 1
    saved = json.loads(state.read_text())
    assert saved["next_index"] == 2


# ---------- import_recipes ----------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_import_recipes_basic(db_path: Path, tmp_path: Path):
    url = "https://hf.test/recipes/alpha"
    entity = {
        "@type": "Recipe",
        "name": "Alpha Risotto",
        "totalTime": "PT45M",
        "recipeYield": 4,
        "recipeIngredient": ["1 cup arborio rice", "2 cups stock"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Stir."}],
        "recipeCategory": "main course",
        "recipeCuisine": "italian",
        "image": "https://img.test/alpha.jpg",
        "aggregateRating": {"ratingValue": "4.2", "ratingCount": "12"},
        "nutrition": {"@type": "NutritionInformation", "calories": "500"},
    }
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [{"url": url, "entity": entity}])

    stats = import_recipes(jsonl_path=jsonl, db_path=db_path, quiet=True)

    assert stats["processed"] == 1
    assert stats["recipes"] == 1
    assert stats["ingredients"] == 2
    assert stats["tags"] == 2
    assert stats["skipped"] == 0

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT source, source_id, name, servings, rating FROM recipes"
        ).fetchone()
        assert row["source"] == "hellofresh"
        assert row["source_id"] == url
        assert row["name"] == "Alpha Risotto"
        assert row["servings"] == 4
        assert row["rating"] == 4.2

        tags = sorted(t["tag"] for t in conn.execute("SELECT tag FROM recipe_tags").fetchall())
        assert tags == ["italian", "main course"]


def test_import_recipes_idempotent(db_path: Path, tmp_path: Path):
    url = "https://hf.test/recipes/alpha"
    entity = {
        "@type": "Recipe",
        "name": "Alpha",
        "recipeIngredient": ["1 cup flour", "2 eggs"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Mix."}],
    }
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [{"url": url, "entity": entity}])

    import_recipes(jsonl_path=jsonl, db_path=db_path, quiet=True)
    import_recipes(jsonl_path=jsonl, db_path=db_path, quiet=True)

    with connect(db_path) as conn:
        recipes = conn.execute("SELECT COUNT(*) FROM recipes WHERE source='hellofresh'").fetchone()[
            0
        ]
        ings = conn.execute("SELECT COUNT(*) FROM recipe_ingredients").fetchone()[0]
    assert recipes == 1
    assert ings == 2


def test_import_recipes_missing_file_raises(db_path: Path, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        import_recipes(jsonl_path=tmp_path / "nope.jsonl", db_path=db_path, quiet=True)


# ---------- name cleanup ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Veggie Caprese Sandwich | 2 Servings", "Veggie Caprese Sandwich"),
        ("Sliced Cheddar Cheese | 8 oz (11 slices)", "Sliced Cheddar Cheese"),
        ("Cheesy Bacon Strata | Serves 4 ($8.75/serving)", "Cheesy Bacon Strata"),
        ("[PROTEIN DOUBLE CHICKEN] Stir Fry", "Stir Fry"),
        ("[SWAP PORK CHOPS TO CHICKEN CUTLETS] Schnitzel", "Schnitzel"),
        ("MA/CA only Steakhouse Pork Chop", "Steakhouse Pork Chop"),
        ("PNW/MA/CA only Pan Seared Pork Chops", "Pan Seared Pork Chops"),
        ("MA/CA Only Compliant Chicken With Rice", "Chicken With Rice"),
        ("MA/CA only Complaint Lemon Garlic Pork Chop", "Lemon Garlic Pork Chop"),
        (
            "[SIDE VEG DOUBLE BROCCOLI] MA/CA only Compliant Chicken with Sauce | 2 Servings",
            "Chicken with Sauce",
        ),
        (
            "SEO/ Crispy Maple Mustard Chicken W18 SEO/ with Carrots",
            "Crispy Maple Mustard Chicken W18 with Carrots",
        ),
        ("Sheet Pan Chicken Fajitas", "Sheet Pan Chicken Fajitas"),
        ("[X]   Foo   |   Bar", "Foo"),
    ],
)
def test_clean_recipe_name(raw, expected):
    assert _clean_recipe_name(raw) == expected


def test_clean_existing_names_updates_only_dirty_rows_and_keeps_fts_synced(db_path: Path):
    with connect(db_path) as conn:
        for src_id, name in [
            ("hf://1", "[PROTEIN DOUBLE] Salmon Bowl | 2 Servings"),
            ("hf://2", "Sheet Pan Chicken Fajitas"),
        ]:
            conn.execute(
                "INSERT INTO recipes (source, source_id, name) VALUES ('hellofresh', ?, ?)",
                (src_id, name),
            )

    stats = clean_existing_names(db_path=db_path, quiet=True)
    assert stats == {"scanned": 2, "updated": 1, "unchanged": 1}

    with connect(db_path) as conn:
        cleaned = {
            r["source_id"]: r["name"]
            for r in conn.execute("SELECT source_id, name FROM recipes WHERE source='hellofresh'")
        }
        assert cleaned == {
            "hf://1": "Salmon Bowl",
            "hf://2": "Sheet Pan Chicken Fajitas",
        }
        rows = conn.execute(
            "SELECT r.name FROM recipes_fts f JOIN recipes r ON r.id = f.rowid "
            "WHERE recipes_fts MATCH 'salmon'"
        ).fetchall()
        assert [r["name"] for r in rows] == ["Salmon Bowl"]
        stale = conn.execute(
            "SELECT 1 FROM recipes_fts WHERE recipes_fts MATCH 'PROTEIN'"
        ).fetchall()
        assert stale == []


# ---------- adapter (raw {url, entity} -> JSONL contract) ----------


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
        "nutrition": {"@type": "NutritionInformation", "calories": 550, "proteinContent": "30 g"},
    }


def test_to_contract_basic_record_passes_validation():
    from pantry_cooking_vibes.models import RecipeRecord

    from pantry_cooking_vibes_hellofresh._adapter import to_contract

    url = "https://hf.test/recipes/alpha"
    raw = {
        "url": url,
        "entity": _hf_entity(
            "[PROTEIN DOUBLE] Salmon Bowl | 2 Servings",
            ["1 cup rice", "2 fillets salmon", "olive oil"],
        ),
    }
    contract = to_contract(raw)
    assert contract is not None
    rec = RecipeRecord.model_validate(contract)

    assert rec.source_id == url
    assert rec.name == "Salmon Bowl"  # editorial markers stripped
    assert rec.cooking_time_min == 30
    assert rec.servings == 4
    assert rec.rating == 4.2
    assert rec.rating_count == 12
    assert rec.tags == ["main course", "italian"]
    assert [i.original_text for i in rec.ingredients] == [
        "1 cup rice",
        "2 fillets salmon",
        "olive oil",
    ]
    assert rec.nutrition_json is not None
    assert rec.nutrition_json.get("calories") == 550


def test_to_contract_drops_malformed():
    from pantry_cooking_vibes_hellofresh._adapter import to_contract

    assert to_contract({"url": "https://x"}) is None  # no entity
    assert to_contract({"entity": {"@type": "Recipe"}}) is None  # no url
    assert to_contract({"url": 123, "entity": {}}) is None  # url not str


def test_to_contract_drops_sparse_recipes():
    from pantry_cooking_vibes_hellofresh._adapter import to_contract

    # Only one ingredient — legacy importer rejected these as protein-swap stubs.
    sparse = {
        "url": "https://hf.test/recipes/stub",
        "entity": _hf_entity("Stub", ["1 thing"]),
    }
    assert to_contract(sparse) is None

    # Missing instructions -> drop.
    no_steps = {
        "url": "https://hf.test/recipes/nosteps",
        "entity": {
            "@type": "Recipe",
            "name": "No Steps",
            "recipeIngredient": ["a", "b"],
        },
    }
    assert to_contract(no_steps) is None

    # Untitled (parse_recipe falls back to "(untitled)") -> drop.
    untitled = {
        "url": "https://hf.test/recipes/untitled",
        "entity": {
            "@type": "Recipe",
            "recipeIngredient": ["a", "b"],
            "recipeInstructions": [{"@type": "HowToStep", "text": "Do it."}],
        },
    }
    assert to_contract(untitled) is None


def test_to_contract_drops_truncated_image_urls():
    """Truncated CDN paths (`.../w_1200/`) don't render — drop the record."""
    from pantry_cooking_vibes_hellofresh._adapter import to_contract

    raw = {
        "url": "https://hf.test/recipes/x",
        "entity": _hf_entity("Plain", ["a", "b"], image="https://img.hellofresh.com/w_1200/"),
    }
    assert to_contract(raw) is None


def test_to_contract_drops_missing_image():
    from pantry_cooking_vibes_hellofresh._adapter import to_contract

    no_image = _hf_entity("Plain", ["a", "b"])
    no_image.pop("image", None)
    assert to_contract({"url": "https://hf.test/recipes/noimg", "entity": no_image}) is None

    blank = _hf_entity("Plain", ["a", "b"], image="")
    assert to_contract({"url": "https://hf.test/recipes/blank", "entity": blank}) is None


# ---------- plugin (post_process + ingest_jsonl end-to-end) ----------


def test_plugin_post_process_filters_and_adapts():
    from pantry_cooking_vibes_hellofresh.plugin import HelloFreshImporter

    raw_records = [
        {
            "url": "https://hf.test/recipes/alpha",
            "entity": _hf_entity(
                "[PROTEIN DOUBLE] Salmon Bowl | 2 Servings",
                ["1 cup rice", "2 fillets salmon"],
            ),
        },
        # Sparse: dropped by adapter.
        {
            "url": "https://hf.test/recipes/stub",
            "entity": _hf_entity("Stub", ["only thing"]),
        },
        # Malformed: dropped by adapter.
        {"oops": "no url"},
    ]
    out = HelloFreshImporter().post_process(raw_records)
    assert len(out) == 1
    assert out[0]["name"] == "Salmon Bowl"
    assert out[0]["source_id"] == "https://hf.test/recipes/alpha"


def test_plugin_via_ingest_jsonl_end_to_end(db_path: Path, tmp_path: Path):
    """Raw HF {url, entity} JSONL -> ingest_jsonl(plugin='hellofresh') -> DB rows."""
    from pantry_cooking_vibes.db import connect
    from pantry_cooking_vibes.importers.jsonl_ingest import ingest_jsonl

    raw_lines = [
        {
            "url": "https://hf.test/recipes/alpha",
            "entity": _hf_entity(
                "[PROTEIN DOUBLE] Salmon Bowl | 2 Servings",
                ["1 cup rice", "2 fillets salmon"],
            ),
        },
        {
            "url": "https://hf.test/recipes/beta",
            "entity": _hf_entity(
                "MA/CA only Compliant Pork Chop",
                ["1 chop", "salt", "pepper"],
            ),
        },
        # Sparse — plugin should drop before validation.
        {
            "url": "https://hf.test/recipes/stub",
            "entity": _hf_entity("Stub", ["only thing"]),
        },
    ]
    jsonl = tmp_path / "raw.jsonl"
    _write_jsonl(jsonl, raw_lines)

    stats = ingest_jsonl(
        jsonl,
        source="hellofresh",
        db_path=db_path,
        plugin="hellofresh",
        quiet=True,
    )

    assert stats["recipes"] == 2  # stub filtered out
    assert stats["skipped"] == 0  # plugin removed before validation, not "skipped"

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source, source_id, name FROM recipes ORDER BY source_id"
        ).fetchall()
        assert [r["source"] for r in rows] == ["hellofresh", "hellofresh"]
        names = [r["name"] for r in rows]
        assert "Salmon Bowl" in names  # editorial markers stripped
        assert "Pork Chop" in names  # regional flag stripped
