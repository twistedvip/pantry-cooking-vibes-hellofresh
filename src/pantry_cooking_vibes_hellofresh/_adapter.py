"""Adapt HelloFresh scrape JSONL lines to the JSONL ingest contract.

Input: dict with ``url`` (str) and ``entity`` (schema.org Recipe dict) — the
shape ``scrape_recipes`` writes to ``data/raw/hellofresh/recipes.jsonl``.

Output: dict matching ``pantry_cooking_vibes.models.RecipeRecord`` so that
``meal-cli ingest --plugin hellofresh`` can validate and UPSERT.
"""

from __future__ import annotations

import json

from pantry_cooking_vibes.importers.url_import import parse_recipe

from pantry_cooking_vibes_hellofresh.import_legacy import _clean_recipe_name


def to_contract(raw: dict) -> dict | None:
    """Map an HF scrape line to a JSONL contract dict.

    Mirrors the legacy importer's filters: drops records with no Recipe
    entity, ``(untitled)`` names, no instructions, ≤1 ingredient, or no
    usable image URL (HF often serves placeholder/truncated CDN paths
    that don't render). Returns ``None`` so the caller can skip them.
    """
    url = raw.get("url")
    entity = raw.get("entity")
    if not isinstance(url, str) or not isinstance(entity, dict):
        return None

    rec = parse_recipe(entity, url)

    name = rec["name"]
    if name != "(untitled)":
        cleaned = _clean_recipe_name(name)
        if cleaned:
            name = cleaned
    if name == "(untitled)" or not rec["instructions_md"]:
        return None
    if len(rec["ingredients"]) <= 1:
        return None

    image_url = rec["image_url"]
    if isinstance(image_url, str) and image_url.endswith("/"):
        image_url = None
    if not image_url or not image_url.strip():
        return None

    nutrition_str = rec["nutrition_json"]
    nutrition_dict: dict | None = json.loads(nutrition_str) if nutrition_str else None
    if nutrition_dict == {}:
        nutrition_dict = None

    return {
        "source_id": url,
        "name": name,
        "cooking_time_min": rec["cooking_time_min"],
        "servings": rec["servings"],
        "instructions_md": rec["instructions_md"],
        "image_url": image_url,
        "rating": rec["rating"],
        "rating_count": rec["rating_count"],
        "nutrition_json": nutrition_dict,
        "tags": rec["tags"],
        "ingredients": [{"original_text": s} for s in rec["ingredients"]],
    }
