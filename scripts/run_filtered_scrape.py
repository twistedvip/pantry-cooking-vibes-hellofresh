"""Filter HelloFresh sitemap by ObjectId timestamp, then scrape.

The sitemap_recipe_pages.xml lists 15k+ URLs but HelloFresh strips JSON-LD
(empty recipeIngredient + recipeInstructions) on URLs whose mongo ObjectId
timestamp predates 2016. Spot-check confirmed clean yields >=2016, ~0%
yield in 2013-14. Filtering before scrape skips ~550 doomed fetches.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pantry_cooking_vibes_hellofresh.scraper import RAW_DIR, discover_urls, scrape_recipes

# Years before 2016 yield no recipe data, so skip them.
CUTOFF_YEAR = 2016
CUTOFF_TS = datetime(CUTOFF_YEAR, 1, 1, tzinfo=timezone.utc).timestamp()
OID_RE = re.compile(r"-([0-9a-f]{24})/?$")


def filter_by_objectid(urls: list[str]) -> list[str]:
    keep: list[str] = []
    for u in urls:
        m = OID_RE.search(u.rstrip("/"))
        if not m:
            continue
        ts = int(m.group(1)[:8], 16)
        if ts >= CUTOFF_TS:
            keep.append(u)
    return keep


def reset_state(raw_dir: Path) -> None:
    for name in ("recipes.jsonl", "recipes_state.json"):
        p = raw_dir / name
        if p.exists():
            p.unlink()


def main() -> int:
    print("[hf-filter] discovering sitemap URLs", file=sys.stderr)
    urls = discover_urls()
    print(f"[hf-filter] sitemap total={len(urls)}", file=sys.stderr)

    keep = filter_by_objectid(urls)
    print(
        f"[hf-filter] keep>={CUTOFF_YEAR}: {len(keep)}  drop: {len(urls) - len(keep)}",
        file=sys.stderr,
    )

    reset_state(RAW_DIR)
    print(f"[hf-filter] cleared state under {RAW_DIR}", file=sys.stderr)

    max_recipes = int(os.environ.get("HF_MAX_RECIPES", "0"))
    sleep = float(os.environ.get("HF_SLEEP", "1.5"))
    if max_recipes:
        print(f"[hf-filter] cap: max_recipes={max_recipes} sleep={sleep}", file=sys.stderr)

    scrape_recipes(
        urls=keep,
        resume=False,
        verbose=True,
        sleep=sleep,
        max_recipes=max_recipes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
