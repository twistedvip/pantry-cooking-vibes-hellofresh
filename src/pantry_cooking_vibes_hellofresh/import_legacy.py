"""Legacy direct-DB importer for HelloFresh JSON-LD JSONL.

Pre-dates the JSONL contract in ``pantry-cooking-vibes`` core. Kept for
operators who already have raw HF JSON-LD JSONL on disk; new pipelines should
convert to the contract and use ``meal-cli ingest`` instead.

Also houses ``_clean_recipe_name``, the editorial-marker stripper used by the
``HelloFreshImporter`` plugin.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from pantry_cooking_vibes.db import DB_PATH, connect
from pantry_cooking_vibes.importers.url_import import parse_recipe

from pantry_cooking_vibes_hellofresh.scraper import RAW_DIR

log = logging.getLogger(__name__)

# Mapping-queue source slice for HelloFresh ingredient strings.
MAPPING_SOURCE = "hellofresh_url"


# HelloFresh stuffs editorial markers and portion metadata into the JSON-LD
# `name` field. The cleaner below strips:
#   1. leading `[BRACKETED]` swap/variant markers (PROTEIN DOUBLE..., SWAP...)
#   2. leading regional flags ("MA/CA only", "PNW/MA/CA only", optionally
#      followed by "Compliant" — also tolerates the typo "Complaint")
#   3. "SEO/" markers anywhere in the string
#   4. trailing "| ..." suffix (Serves N, oz/serving prices, etc.)
_RE_LEADING_BRACKET = re.compile(r"^\s*\[[^\]]*\]\s*")
_RE_LEADING_REGION = re.compile(
    r"^\s*[A-Z]{2,3}(?:/[A-Z]{2,3})*\s+(?:only|Only|ONLY)\s*(?:[Cc]omp(?:liant|laint)\s+)?"
)
_RE_SEO = re.compile(r"\bSEO/\s*", re.IGNORECASE)
_RE_PIPE_SUFFIX = re.compile(r"\s*\|.*$", re.DOTALL)
_RE_WHITESPACE = re.compile(r"\s+")


def _clean_recipe_name(name: str) -> str:
    """Strip HelloFresh editorial markers and trailing portion metadata."""
    s = name
    prev = None
    while prev != s:
        prev = s
        s = _RE_LEADING_BRACKET.sub("", s)
        s = _RE_LEADING_REGION.sub("", s)
    s = _RE_SEO.sub("", s)
    s = _RE_PIPE_SUFFIX.sub("", s)
    s = _RE_WHITESPACE.sub(" ", s).strip()
    return s


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _load_canonical_map(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT source_key, proposed_canonical_id
        FROM ingredient_mapping_queue
        WHERE source = ?
          AND status IN ('approved', 'proposed')
          AND proposed_canonical_id IS NOT NULL
        """,
        (MAPPING_SOURCE,),
    ).fetchall()
    return {r["source_key"]: r["proposed_canonical_id"] for r in rows}


def _upsert_recipe(conn: sqlite3.Connection, rec: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO recipes
            (source, source_id, name, cooking_time_min, servings,
             instructions_md, nutrition_json, image_url, rating, rating_count)
        VALUES ('hellofresh', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            name             = excluded.name,
            cooking_time_min = excluded.cooking_time_min,
            servings         = excluded.servings,
            instructions_md  = excluded.instructions_md,
            nutrition_json   = excluded.nutrition_json,
            image_url        = excluded.image_url,
            rating           = excluded.rating,
            rating_count     = excluded.rating_count
        RETURNING id
        """,
        (
            rec["source_id"],
            rec["name"],
            rec["cooking_time_min"],
            rec["servings"],
            rec["instructions_md"],
            rec["nutrition_json"],
            rec["image_url"],
            rec["rating"],
            rec["rating_count"],
        ),
    )
    return cur.fetchone()["id"]


def _replace_tags(conn: sqlite3.Connection, recipe_id: int, tags: list[str]) -> int:
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    inserted = 0
    for tag in tags:
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag) VALUES (?, ?)",
            (recipe_id, tag),
        )
        inserted += 1
    return inserted


def _replace_ingredients(
    conn: sqlite3.Connection,
    recipe_id: int,
    ingredients: list[str],
    canonical_map: dict[str, int],
) -> int:
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    for text in ingredients:
        conn.execute(
            """
            INSERT INTO recipe_ingredients
                (recipe_id, canonical_id, original_text, quantity, unit, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (recipe_id, canonical_map.get(text), text, None, None, None),
        )
    return len(ingredients)


def import_recipes(
    jsonl_path: Path | None = None,
    *,
    db_path: Path | None = None,
    batch_size: int = 200,
    limit: int = 0,
    quiet: bool = False,
) -> dict:
    """Import HelloFresh recipes from a scraped JSONL.

    Idempotent: re-running upserts on (source='hellofresh', source_id=url);
    tags and ingredients are replaced wholesale per recipe.
    """
    path = jsonl_path or (RAW_DIR / "recipes.jsonl")
    db = db_path or DB_PATH
    if not path.exists():
        raise FileNotFoundError(path)

    stats = {"processed": 0, "recipes": 0, "ingredients": 0, "tags": 0, "skipped": 0}

    with connect(db) as conn:
        canonical_map = _load_canonical_map(conn)
        for line in _iter_jsonl(path):
            stats["processed"] += 1
            url = line.get("url")
            entity = line.get("entity")
            if not isinstance(url, str) or not isinstance(entity, dict):
                stats["skipped"] += 1
                continue

            rec = parse_recipe(entity, url)
            if rec["name"] != "(untitled)":
                rec["name"] = _clean_recipe_name(rec["name"]) or "(untitled)"
            if rec["name"] == "(untitled)" or not rec["instructions_md"]:
                stats["skipped"] += 1
                continue
            if len(rec["ingredients"]) <= 1:
                stats["skipped"] += 1
                continue
            if rec["image_url"] and rec["image_url"].endswith("/"):
                rec["image_url"] = None

            recipe_id = _upsert_recipe(conn, rec)
            stats["tags"] += _replace_tags(conn, recipe_id, rec["tags"])
            stats["ingredients"] += _replace_ingredients(
                conn, recipe_id, rec["ingredients"], canonical_map
            )
            stats["recipes"] += 1

            if stats["recipes"] % batch_size == 0:
                conn.commit()
                if not quiet:
                    print(f"  imported {stats['recipes']} recipes...")
            if limit and stats["recipes"] >= limit:
                break

    return stats


def delete_sparse_recipes(
    *, db_path: Path | None = None, min_ingredients: int = 2, quiet: bool = False
) -> dict:
    """Delete HelloFresh recipes with fewer than ``min_ingredients`` ingredients."""
    db = db_path or DB_PATH
    stats = {"scanned": 0, "deleted": 0, "kept": 0}

    with connect(db) as conn:
        rows = conn.execute(
            """
            SELECT r.id, COUNT(ri.id) AS ing_count
            FROM recipes r
            LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE r.source = 'hellofresh'
            GROUP BY r.id
            """
        ).fetchall()
        for row in rows:
            stats["scanned"] += 1
            if row["ing_count"] < min_ingredients:
                conn.execute("DELETE FROM recipes WHERE id = ?", (row["id"],))
                stats["deleted"] += 1
                if not quiet and stats["deleted"] % 500 == 0:
                    print(f"  deleted {stats['deleted']} recipes...")
            else:
                stats["kept"] += 1

    return stats


def clean_existing_names(*, db_path: Path | None = None, quiet: bool = False) -> dict:
    """Apply ``_clean_recipe_name`` to every existing HelloFresh recipe row."""
    db = db_path or DB_PATH
    stats = {"scanned": 0, "updated": 0, "unchanged": 0}

    with connect(db) as conn:
        rows = conn.execute("SELECT id, name FROM recipes WHERE source = 'hellofresh'").fetchall()
        for row in rows:
            stats["scanned"] += 1
            current = row["name"]
            cleaned = _clean_recipe_name(current) if current else current
            if not cleaned or cleaned == current:
                stats["unchanged"] += 1
                continue
            conn.execute("UPDATE recipes SET name = ? WHERE id = ?", (cleaned, row["id"]))
            stats["updated"] += 1
            if not quiet and stats["updated"] % 500 == 0:
                print(f"  cleaned {stats['updated']} names...")

    return stats
