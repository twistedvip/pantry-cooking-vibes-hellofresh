"""HelloFresh post-process plugin for the pantry-cooking-vibes ingest pipeline.

Registered as ``pantry_cooking_vibes.importers`` entry-point ``hellofresh``.
Invoked by ``meal-cli ingest path/to/hf.jsonl --source hellofresh --plugin hellofresh``
before each record is validated against the JSONL contract.

The scraper writes ``{url, entity}`` lines (raw schema.org JSON-LD); this
plugin converts each line to a contract dict via :func:`_adapter.to_contract`,
strips HelloFresh editorial markers, and drops untitled / sparse recipes.
"""

from __future__ import annotations

from pantry_cooking_vibes_hellofresh import __version__
from pantry_cooking_vibes_hellofresh._adapter import to_contract


class HelloFreshImporter:
    """RecipeImporter Protocol implementation for HelloFresh."""

    name = "hellofresh"
    version = __version__

    def post_process(self, records: list[dict]) -> list[dict]:
        """Convert raw HF ``{url, entity}`` lines to JSONL-contract dicts."""
        out: list[dict] = []
        for raw in records:
            adapted = to_contract(raw)
            if adapted is not None:
                out.append(adapted)
        return out
