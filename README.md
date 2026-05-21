# pantry-cooking-vibes-hellofresh

HelloFresh recipe scraper + post-process plugin for the
[`pantry-cooking-vibes`](../../) core.

## Source

`https://www.hellofresh.com/sitemap_recipe_pages.xml` lists every recipe
URL. Each page embeds a schema.org `Recipe` JSON-LD block.

`robots.txt` allows `/recipes/*` (only `/recipes/search/?q*` is blocked).
Verified 2026-04-25.

## Layout

```
src/pantry_cooking_vibes_hellofresh/
  scraper.py         # sitemap -> {url, entity} JSONL
  _adapter.py        # {url, entity} -> JSONL contract dict
  plugin.py          # RecipeImporter entry-point: filters + adapts each line
  import_legacy.py   # direct-DB importer (pre-JSONL contract); houses _clean_recipe_name
  _utils.py          # HTML/coercion helpers (copied to keep repo standalone)
tests/
  test_scraper.py    # sitemap, scrape loop, adapter, plugin, ingest_jsonl coverage
```

## Pipeline

1. `scrape_recipes()` walks the sitemap, extracts each page's schema.org
   Recipe JSON-LD, and writes `{url, entity}` lines to
   `data/raw/hellofresh/recipes.jsonl`.
2. `meal-cli ingest data/raw/hellofresh/recipes.jsonl --source hellofresh --plugin hellofresh`
   loads `HelloFreshImporter`, runs `_adapter.to_contract` on each line, and
   UPSERTs into core's `recipes` / `recipe_tags` / `recipe_ingredients`.

The adapter calls core's `parse_recipe(entity, url)` to map JSON-LD to a
recipes-row dict, then:

- runs `_clean_recipe_name` to strip editorial markers (see below)
- drops records that are untitled, missing instructions, or have ≤1 ingredient
  (legacy importer's protein-swap-stub filter)
- nulls truncated CDN image URLs (`.../w_1200/`)
- decodes `nutrition_json` back to a dict for the contract

## Editorial-marker stripping

HelloFresh stuffs editorial markers and portion metadata into the
JSON-LD `name` field. `import_legacy._clean_recipe_name` strips:

1. leading `[BRACKETED]` swap/variant markers (`[PROTEIN DOUBLE...]`, `[SWAP...]`)
2. leading regional flags (`MA/CA only`, `PNW/MA/CA only Compliant`,
   tolerating the typo `Complaint`)
3. `SEO/` markers anywhere in the string
4. trailing `| ...` suffix (Serves N, oz/serving, etc.)

## Status

`import_legacy.py` is retained for operators with raw HF JSONL captured under
the old direct-DB importer. New pipelines should use `meal-cli ingest`.

## Local install (dev)

```bash
uv pip install -e ../../        # install core
uv pip install -e .             # install this plugin (registers entry-point)
```

Then in core:

```bash
meal-cli ingest path/to/hellofresh.jsonl --source hellofresh --plugin hellofresh
```
