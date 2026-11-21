"""Price weights for token usage: reads mastack's models.toml when present, else built-in defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any

from .schema import RequestRecord, as_dict


@dataclass(frozen=True)
class PriceWeights:
    input: float = 1.0
    output: float = 5.0
    cache_read: float = 0.1
    cache_write_short: float = 1.25
    cache_write_long: float = 2.0
    cache_write_default: float = 1.0
    tier_multiplier: float = 1.0


DEFAULT_WEIGHTS = PriceWeights()

# Relative tier multipliers when models.toml does not supply explicit per-model weights.
TIER_MULTIPLIERS: dict[str, float] = {
    "frontier": 1.0,
    "strong": 1.0,
    "routine": 0.2,
}


def _empty_str_set() -> set[str]:
    return set()


@dataclass
class PricingTable:
    default: PriceWeights
    by_model: dict[str, PriceWeights]
    source: str
    aliases: set[str] = field(default_factory=_empty_str_set)

    def for_model(self, model: str) -> PriceWeights:
        if not model:
            return self.default
        if model in self.by_model:
            return self.by_model[model]
        lower = model.lower()
        for key, weights in self.by_model.items():
            if key.lower() == lower:
                return weights
        # Prefix-match longest non-alias keys first so short aliases never shadow specific model IDs.
        candidates = sorted(
            ((k, w) for k, w in self.by_model.items() if k not in self.aliases),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for key, weights in candidates:
            if lower.startswith(key.lower()):
                return weights
        return self.default

    def weighted_components(self, record: RequestRecord) -> dict[str, float]:
        weights = self.for_model(record.model)
        mult = weights.tier_multiplier
        inp = record.input_tokens * weights.input * mult
        out = record.output_tokens * weights.output * mult
        cread = record.cache_read_tokens * weights.cache_read * mult
        cwrite = 0.0
        if record.cache_write_ttl_buckets:
            for bucket, tokens in record.cache_write_ttl_buckets.items():
                if bucket == "short":
                    cwrite += tokens * weights.cache_write_short * mult
                elif bucket == "long":
                    cwrite += tokens * weights.cache_write_long * mult
                else:
                    cwrite += tokens * weights.cache_write_default * mult
        elif record.cache_write_tokens > 0:
            rate = weights.cache_write_short if record.agent_role == "subagent" else weights.cache_write_long
            cwrite = record.cache_write_tokens * rate * mult
        return {
            "input": inp,
            "output": out,
            "cache_read": cread,
            "cache_write": cwrite,
            "total": inp + out + cread + cwrite,
        }


def _float(mapping: dict[str, Any], key: str, fallback: float) -> float:
    val = mapping.get(key)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return fallback


def default_models_path() -> Path:
    configured = os.environ.get("TOKENFATHER_MODELS") or os.environ.get("MASTACK_MODELS")
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / "mastack" / "models.toml"


def load_pricing(models_path: Path | None = None) -> PricingTable:
    """Load price weights from mastack's models.toml when present, else built-in defaults."""
    path = models_path or default_models_path()
    by_model: dict[str, PriceWeights] = {}
    default = DEFAULT_WEIGHTS
    if not path.is_file():
        return PricingTable(default=default, by_model=by_model, source="built-in")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return PricingTable(default=default, by_model=by_model, source="built-in")

    pricing_section = as_dict(data.get("pricing"))
    if pricing_section is not None:
        raw_default = as_dict(pricing_section.get("default"))
        if raw_default is not None:
            default = PriceWeights(
                input=_float(raw_default, "input", default.input),
                output=_float(raw_default, "output", default.output),
                cache_read=_float(raw_default, "cache_read", default.cache_read),
                cache_write_short=_float(raw_default, "cache_write_short", default.cache_write_short),
                cache_write_long=_float(raw_default, "cache_write_long", default.cache_write_long),
                cache_write_default=_float(raw_default, "cache_write_default", default.cache_write_default),
                tier_multiplier=_float(raw_default, "tier_multiplier", 1.0),
            )

    aliases: set[str] = set()
    models_section = as_dict(data.get("models"))
    if models_section is not None:
        for alias, raw_entry in models_section.items():
            entry = as_dict(raw_entry)
            if entry is None:
                continue
            model_id = str(entry.get("id") or alias)
            tier = str(entry.get("tier") or "")
            raw_w: dict[str, Any] = dict(as_dict(entry.get("weights")) or {})
            if pricing_section is not None:
                alias_override = as_dict(pricing_section.get(alias))
                if alias_override is not None:
                    raw_w.update(alias_override)
                id_override = as_dict(pricing_section.get(model_id))
                if id_override is not None:
                    raw_w.update(id_override)
            weights = PriceWeights(
                input=_float(raw_w, "input", default.input),
                output=_float(raw_w, "output", default.output),
                cache_read=_float(raw_w, "cache_read", default.cache_read),
                cache_write_short=_float(raw_w, "cache_write_short", default.cache_write_short),
                cache_write_long=_float(raw_w, "cache_write_long", default.cache_write_long),
                cache_write_default=_float(raw_w, "cache_write_default", default.cache_write_default),
                tier_multiplier=_float(raw_w, "tier_multiplier", TIER_MULTIPLIERS.get(tier, 1.0)),
            )
            by_model[model_id] = weights
            if alias != model_id:
                by_model[alias] = weights
                aliases.add(alias)

    # Load standalone [pricing.<model>] entries even when not declared under [models] (e.g. Antigravity/Gemini).
    if pricing_section is not None:
        for key, raw_entry in pricing_section.items():
            if key == "default" or key in by_model:
                continue
            entry = as_dict(raw_entry)
            if entry is None:
                continue
            tier = str(entry.get("tier") or "")
            by_model[key] = PriceWeights(
                input=_float(entry, "input", default.input),
                output=_float(entry, "output", default.output),
                cache_read=_float(entry, "cache_read", default.cache_read),
                cache_write_short=_float(entry, "cache_write_short", default.cache_write_short),
                cache_write_long=_float(entry, "cache_write_long", default.cache_write_long),
                cache_write_default=_float(entry, "cache_write_default", default.cache_write_default),
                tier_multiplier=_float(entry, "tier_multiplier", TIER_MULTIPLIERS.get(tier, 1.0)),
            )

    return PricingTable(default=default, by_model=by_model, source=str(path), aliases=aliases)
