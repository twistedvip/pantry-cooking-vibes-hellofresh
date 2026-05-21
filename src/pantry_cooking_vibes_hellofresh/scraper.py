"""HelloFresh recipe scraper.

robots.txt allows /recipes/* (only /recipes/search/?q* is blocked) and
publishes a dedicated sitemap_recipe_pages.xml. This module discovers URLs
from that sitemap and persists each page's schema.org Recipe JSON-LD entity
to a resumable JSONL file. Verified 2026-04-25.

JSONL line shape: ``{"url": str, "entity": {schema.org Recipe dict}}``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from pantry_cooking_vibes.importers.url_import import _build_session, extract_recipe_jsonld

log = logging.getLogger(__name__)

SITEMAP_URL = "https://www.hellofresh.com/sitemap_recipe_pages.xml"
REQUEST_TIMEOUT = 30  # recipe pages are 4-5 MB

_REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = _REPO_ROOT / "data" / "raw" / "hellofresh"


def _build_hf_session() -> requests.Session:
    """Chrome-fingerprint session, but no brotli — urllib3 can't decode `br`
    without the optional brotli package, and HF's CDN happily serves it when
    advertised, leaving us with garbage bytes."""
    s = _build_session()
    s.headers["Accept-Encoding"] = "gzip, deflate"
    return s


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _parse_sitemap(xml_text: str) -> list[str]:
    """Parse a <urlset> sitemap, returning <loc> URLs in document order."""
    # Sitemap is fetched from controlled HelloFresh endpoint; not user input.
    root = ET.fromstring(xml_text)  # noqa: S314
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text.strip() for loc in root.findall(".//sm:loc", ns) if loc.text]


def discover_urls(
    sitemap_url: str = SITEMAP_URL,
    *,
    session: requests.Session | None = None,
) -> list[str]:
    """Fetch sitemap_recipe_pages.xml and return all recipe URLs."""
    s = session or _build_hf_session()
    r = s.get(sitemap_url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return _parse_sitemap(r.text)


def scrape_recipes(
    out_path: Path | None = None,
    state_path: Path | None = None,
    *,
    sleep: float = 1.5,
    max_recipes: int = 0,
    resume: bool = True,
    verbose: bool = True,
    sitemap_url: str = SITEMAP_URL,
    urls: list[str] | None = None,
    session: requests.Session | None = None,
) -> int:
    """Iterate sitemap URLs, extract Recipe JSON-LD per page, append to JSONL.

    State file: ``{"next_index": N, "total_written": N}`` so re-runs resume
    after the last fully-processed URL. Returns the number of recipes written
    this run.
    """
    out = out_path or (RAW_DIR / "recipes.jsonl")
    state = state_path or (RAW_DIR / "recipes_state.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)

    s = session or _build_hf_session()

    next_index = 0
    total_written = 0
    if resume and state.exists():
        try:
            d = json.loads(state.read_text())
            next_index = int(d.get("next_index", 0))
            total_written = int(d.get("total_written", 0))
            if verbose and next_index:
                print(
                    f"[hellofresh] resuming from index={next_index} "
                    f"(already written: {total_written})",
                    file=sys.stderr,
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    if urls is None:
        urls = discover_urls(sitemap_url, session=s)
        if verbose:
            print(
                f"[hellofresh] sitemap reports {len(urls)} recipe URLs",
                file=sys.stderr,
            )

    mode = "a" if (resume and next_index > 0) else "w"
    written_this_run = 0
    skipped_this_run = 0
    attempts = 0

    with out.open(mode, encoding="utf-8") as fh:
        for idx in range(next_index, len(urls)):
            url = urls[idx]
            attempts += 1
            try:
                resp = s.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                entity = extract_recipe_jsonld(resp.text)
            except Exception as exc:
                log.warning("[hellofresh] fetch failed for %s: %s", url, exc)
                entity = None

            if entity is None:
                skipped_this_run += 1
            else:
                fh.write(json.dumps({"url": url, "entity": entity}, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
                total_written += 1
                written_this_run += 1

            next_index = idx + 1
            _atomic_write_text(
                state,
                json.dumps({"next_index": next_index, "total_written": total_written}),
            )

            if verbose and attempts % 25 == 0:
                print(
                    f"[hellofresh] index={next_index}/{len(urls)} "
                    f"written={written_this_run} skipped={skipped_this_run}",
                    file=sys.stderr,
                )

            if max_recipes and attempts >= max_recipes:
                break
            time.sleep(sleep)

    if verbose:
        print(
            f"[hellofresh] done: written={written_this_run} skipped={skipped_this_run} "
            f"index={next_index}/{len(urls)}",
            file=sys.stderr,
        )

    return written_this_run
