"""Pre-transform raw HelloFresh scrape JSONL into the core ingest contract.

Streams a raw scraper output file line by line through :func:`_adapter.to_contract`
and writes a contract-shaped JSONL that ``meal-cli ingest`` can read without
``--plugin hellofresh``. Useful when the plugin package is not installed in the
target environment, or when a portable contract-shaped artifact is desired.

Raw line shape: ``{"url": str, "entity": {schema.org Recipe dict}}`` — what
``scrape_recipes`` writes to ``data/raw/hellofresh/recipes.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from pantry_cooking_vibes_hellofresh._adapter import to_contract


class TransformStats(TypedDict):
    processed: int
    written: int
    dropped: int
    malformed: int


def _iter_raw(path: Path) -> Iterator[tuple[int, dict | None]]:
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError:
                yield lineno, None


def transform_jsonl(
    src: Path,
    dst: Path,
    *,
    progress_every: int = 1000,
    quiet: bool = False,
) -> TransformStats:
    """Stream-transform raw HF scrape JSONL at ``src`` into contract JSONL at ``dst``.

    Returns counts. ``dropped`` covers records the adapter rejected (no Recipe
    entity, untitled, no instructions, ≤1 ingredient, or no usable image);
    ``malformed`` covers lines that failed ``json.loads``.
    """
    if not src.exists():
        raise FileNotFoundError(src)

    dst.parent.mkdir(parents=True, exist_ok=True)
    stats: TransformStats = {
        "processed": 0,
        "written": 0,
        "dropped": 0,
        "malformed": 0,
    }

    with dst.open("w", encoding="utf-8") as out_fh:
        for _lineno, raw in _iter_raw(src):
            stats["processed"] += 1
            if raw is None:
                stats["malformed"] += 1
                continue
            adapted = to_contract(raw)
            if adapted is None:
                stats["dropped"] += 1
                continue
            out_fh.write(json.dumps(adapted, ensure_ascii=False) + "\n")
            stats["written"] += 1

            if not quiet and progress_every and stats["processed"] % progress_every == 0:
                print(
                    f"  transformed {stats['processed']} lines "
                    f"(written={stats['written']}, dropped={stats['dropped']}, "
                    f"malformed={stats['malformed']})",
                    file=sys.stderr,
                )

    return stats


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hellofresh-transform",
        description=(
            "Convert a raw HelloFresh scrape JSONL file into the "
            "pantry-cooking-vibes ingest-contract shape."
        ),
    )
    p.add_argument("src", type=Path, help="Path to raw HF scrape JSONL.")
    p.add_argument("dst", type=Path, help="Output path for contract-shaped JSONL.")
    p.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Emit progress every N lines (0 to disable). Default: 1000.",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stats = transform_jsonl(
            args.src,
            args.dst,
            progress_every=args.progress_every,
            quiet=args.quiet,
        )
    except FileNotFoundError as e:
        print(f"input not found: {e}", file=sys.stderr)
        return 1

    print("transform done:")
    print(f"  processed : {stats['processed']}")
    print(f"  written   : {stats['written']}")
    print(f"  dropped   : {stats['dropped']}")
    print(f"  malformed : {stats['malformed']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
