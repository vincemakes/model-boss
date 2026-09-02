"""Host-visible model catalog: exact IDs, effort levels, prices, and mention matching.

The catalog is a dated snapshot, not live truth.  Routing still trusts only host
metadata or a live identity handshake for canonical fingerprints; the catalog
exists so that configuration can be validated (an effort level a model rejects is
a configuration error), so that dispatch estimates have per-token prices, and so
that a user's spoken model name ("opus 4.6", "让 sonnet 去开发") can be matched to
the configured routes without guessing a nearby version.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .models import EFFORT_LEVELS, Route

CATALOG_SNAPSHOT_DATE = "2026-09-02"
CATALOG_SOURCES = (
    "Claude Code model picker and model-config docs (code.claude.com/docs/en/model-config)",
    "Claude API pricing and prompt-caching docs (platform.claude.com/docs)",
)

_FIVE_LEVELS = EFFORT_LEVELS
_FOUR_LEVELS = tuple(level for level in EFFORT_LEVELS if level != "xhigh")
_NO_EFFORT: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogModel:
    """One exact model with the facts dispatch and validation need."""

    model_id: str
    provider_family: str
    display_name: str
    effort_levels: tuple[str, ...]
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    min_cache_prefix_tokens: int
    aliases: tuple[str, ...]

    def cache_write_usd_per_mtok(self, ttl: str = "1h") -> float:
        """Cache writes cost 1.25x base input for 5 minutes and 2x for one hour."""

        if ttl == "1h":
            return self.input_usd_per_mtok * 2.0
        if ttl == "5m":
            return self.input_usd_per_mtok * 1.25
        raise ValueError("ttl must be '5m' or '1h'")

    @property
    def supports_effort(self) -> bool:
        return bool(self.effort_levels)


ANTHROPIC_MODELS: tuple[CatalogModel, ...] = (
    CatalogModel(
        "claude-fable-5-1", "anthropic", "Fable 5.1", _FIVE_LEVELS,
        10.0, 50.0, 0.25, 512, ("fable", "fable 5.1", "claude-fable-5-1"),
    ),
    CatalogModel(
        "claude-fable-5", "anthropic", "Fable 5", _FIVE_LEVELS,
        10.0, 50.0, 1.0, 512, ("fable 5", "claude-fable-5"),
    ),
    CatalogModel(
        "claude-opus-5", "anthropic", "Opus 5", _FIVE_LEVELS,
        5.0, 25.0, 0.5, 512, ("opus", "opus 5", "claude-opus-5"),
    ),
    CatalogModel(
        "claude-opus-4-8", "anthropic", "Opus 4.8", _FIVE_LEVELS,
        5.0, 25.0, 0.5, 1024, ("opus 4.8", "claude-opus-4-8"),
    ),
    CatalogModel(
        "claude-opus-4-7", "anthropic", "Opus 4.7", _FIVE_LEVELS,
        5.0, 25.0, 0.5, 2048, ("opus 4.7", "claude-opus-4-7"),
    ),
    CatalogModel(
        "claude-opus-4-6", "anthropic", "Opus 4.6", _FOUR_LEVELS,
        5.0, 25.0, 0.5, 4096, ("opus 4.6", "claude-opus-4-6"),
    ),
    CatalogModel(
        "claude-sonnet-5", "anthropic", "Sonnet 5", _FIVE_LEVELS,
        2.0, 10.0, 0.2, 1024, ("sonnet", "sonnet 5", "claude-sonnet-5"),
    ),
    CatalogModel(
        "claude-sonnet-4-6", "anthropic", "Sonnet 4.6", _FOUR_LEVELS,
        3.0, 15.0, 0.3, 1024, ("sonnet 4.6", "claude-sonnet-4-6"),
    ),
    CatalogModel(
        "claude-haiku-4-5", "anthropic", "Haiku 4.5", _NO_EFFORT,
        1.0, 5.0, 0.1, 4096, ("haiku", "haiku 4.5", "claude-haiku-4-5"),
    ),
)

_BY_ID: dict[tuple[str, str], CatalogModel] = {
    (model.provider_family, model.model_id.lower()): model for model in ANTHROPIC_MODELS
}


def find_model(provider_family: str | None, model_id: str | None) -> CatalogModel | None:
    """Return the catalog entry for an exact provider/model pair, or None if unknown."""

    if not isinstance(provider_family, str) or not isinstance(model_id, str):
        return None
    return _BY_ID.get((provider_family.strip().lower(), model_id.strip().lower()))


def supported_effort_levels(
    provider_family: str | None,
    model_id: str | None,
) -> tuple[str, ...] | None:
    """Return the effort levels a known model accepts; None when the model is unknown.

    An empty tuple means the model is known and rejects the effort parameter.
    """

    model = find_model(provider_family, model_id)
    return None if model is None else model.effort_levels


def effort_is_valid_for(provider_family: str | None, model_id: str | None, effort: str | None) -> bool:
    if effort is None:
        return True
    if effort not in EFFORT_LEVELS:
        return False
    levels = supported_effort_levels(provider_family, model_id)
    return True if levels is None else effort in levels


_NORMALIZE = re.compile(r"[\W_]+")


def normalize_mention(text: str) -> str:
    """Lower-case and strip everything except letters and digits (any script)."""

    return _NORMALIZE.sub("", text.lower())


@dataclass(frozen=True)
class ModelMention:
    alias: str
    model_id: str | None
    route_ids: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class MentionReport:
    mentions: tuple[ModelMention, ...]
    unmatched: tuple[str, ...]

    @property
    def route_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for mention in self.mentions:
            for route_id in mention.route_ids:
                if route_id not in seen:
                    seen.append(route_id)
        return tuple(seen)


def _alias_table(routes: Mapping[str, Route]) -> dict[str, tuple[str, str | None, tuple[str, ...]]]:
    """Map normalized alias -> (display alias, model id, route ids) across routes."""

    table: dict[str, tuple[str, str | None, list[str]]] = {}

    def add(alias: str, model_id: str | None, route_id: str) -> None:
        normalized = normalize_mention(alias)
        if not normalized:
            return
        display, existing_model, ids = table.get(normalized, (alias, model_id, []))
        if existing_model is None:
            existing_model = model_id
        if route_id not in ids:
            ids.append(route_id)
        table[normalized] = (display, existing_model, ids)

    for route_id, route in routes.items():
        add(route_id, route.model, route_id)
        if route.model is not None:
            add(route.model, route.model, route_id)
            catalog = find_model(route.provider_family, route.model)
            if catalog is not None:
                for alias in catalog.aliases:
                    add(alias, route.model, route_id)
        for alias in route.aliases:
            add(alias, route.model, route_id)
    return {
        key: (display, model_id, tuple(sorted(ids)))
        for key, (display, model_id, ids) in table.items()
    }


def match_model_mentions(text: str, routes: Mapping[str, Route]) -> MentionReport:
    """Find configured model aliases in free text; longest match wins.

    A letters-only alias immediately followed by a digit ("fable 6") is treated
    as a versioned mention that no configured alias covers, and is reported in
    ``unmatched`` so the caller can ask instead of guessing a version.
    """

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    normalized = normalize_mention(text)
    table = _alias_table(routes)
    raw: list[tuple[int, int, str]] = []
    for key in table:
        start = normalized.find(key)
        while start != -1:
            raw.append((start, start + len(key), key))
            start = normalized.find(key, start + 1)

    def followed_by_digit(end: int) -> bool:
        return end < len(normalized) and normalized[end].isdigit()

    kept: list[tuple[int, int, str]] = []
    for start, end, key in sorted(raw, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(s <= start and end <= e and (s, e) != (start, end) for s, e, _ in raw):
            continue
        if followed_by_digit(end):
            continue
        if any(start < e and s < end for s, e, _ in kept):
            continue
        kept.append((start, end, key))

    families = sorted(
        {key for key in table if key.isalpha()},
        key=len,
        reverse=True,
    )
    unmatched: list[str] = []
    if families:
        pattern = re.compile("(" + "|".join(re.escape(f) for f in families) + r")(\d+)")
        for found in pattern.finditer(normalized):
            start, end = found.start(), found.end()
            if not any(s <= start and end <= e for s, e, _ in kept):
                token = found.group(0)
                if token not in unmatched:
                    unmatched.append(token)

    mentions = tuple(
        ModelMention(
            alias=table[key][0],
            model_id=table[key][1],
            route_ids=table[key][2],
            start=start,
            end=end,
        )
        for start, end, key in sorted(kept)
    )
    return MentionReport(mentions=mentions, unmatched=tuple(unmatched))


__all__ = (
    "ANTHROPIC_MODELS",
    "CATALOG_SNAPSHOT_DATE",
    "CATALOG_SOURCES",
    "CatalogModel",
    "MentionReport",
    "ModelMention",
    "effort_is_valid_for",
    "find_model",
    "match_model_mentions",
    "normalize_mention",
    "supported_effort_levels",
)
