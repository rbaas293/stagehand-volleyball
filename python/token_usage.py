"""Accumulate real prompt/completion/total tokens from xAI Responses API usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Published xAI rates for grok-4-fast-* (<128k): $0.20 / 1M input, $0.50 / 1M output.
# (docs.x.ai / x.ai/news/grok-4-fast). Other models fall back to these conservative rates
# unless listed below. Retirement (May 2026) may redirect fast slugs to grok-4.3 pricing.
_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "grok-4-fast-reasoning": (0.20, 0.50),
    "grok-4-fast-non-reasoning": (0.20, 0.50),
    "grok-4-1-fast-reasoning": (0.20, 0.50),
    "grok-4-1-fast-non-reasoning": (0.20, 0.50),
    "grok-4.3": (1.25, 2.50),
}


@dataclass
class TokenUsage:
    model: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    # Raw sum of usage.cost_in_usd_ticks when the API sends it (scale undocumented).
    cost_in_usd_ticks: int = 0
    per_call: list[dict[str, Any]] = field(default_factory=list)

    def record(self, usage: Any, *, model: str | None = None) -> None:
        """Record one Responses API `usage` object (or dict)."""
        if model:
            self.model = model
        if usage is None:
            self.calls += 1
            self.per_call.append({"prompt": 0, "completion": 0, "total": 0, "missing": True})
            return

        if hasattr(usage, "model_dump"):
            data = usage.model_dump()
        elif isinstance(usage, dict):
            data = usage
        else:
            data = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "cost_in_usd_ticks": getattr(usage, "cost_in_usd_ticks", None),
                "input_tokens_details": getattr(usage, "input_tokens_details", None),
                "output_tokens_details": getattr(usage, "output_tokens_details", None),
            }

        prompt = int(data.get("input_tokens") or data.get("prompt_tokens") or 0)
        completion = int(data.get("output_tokens") or data.get("completion_tokens") or 0)
        total = int(data.get("total_tokens") or (prompt + completion))

        cached = 0
        details = data.get("input_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        elif details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)

        reasoning = 0
        out_details = data.get("output_tokens_details") or {}
        if isinstance(out_details, dict):
            reasoning = int(out_details.get("reasoning_tokens") or 0)
        elif out_details is not None:
            reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0)

        ticks = int(data.get("cost_in_usd_ticks") or 0)

        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.reasoning_tokens += reasoning
        self.cached_tokens += cached
        self.cost_in_usd_ticks += ticks
        self.per_call.append(
            {
                "prompt": prompt,
                "completion": completion,
                "total": total,
                "reasoning": reasoning,
                "cached": cached,
                "cost_in_usd_ticks": ticks,
            }
        )

    def estimate_cost_usd(self) -> float | None:
        """Estimate USD from published per-1M rates for the active model."""
        if self.calls == 0 and self.total_tokens == 0:
            return 0.0
        key = (self.model or "").strip().lower()
        rates = _PRICE_PER_M.get(key)
        if rates is None:
            # Prefer fast-tier rates when the slug looks like a fast model.
            if "fast" in key:
                rates = (0.20, 0.50)
            elif key:
                rates = (1.25, 2.50)  # grok-4.3-ish default
            else:
                return 0.0 if self.total_tokens == 0 else None
        inp, out = rates
        return (self.prompt_tokens / 1_000_000.0) * inp + (
            self.completion_tokens / 1_000_000.0
        ) * out

    def as_dict(self) -> dict[str, Any]:
        cost = self.estimate_cost_usd()
        return {
            "model": self.model or None,
            "calls": self.calls,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "cachedTokens": self.cached_tokens,
            "estimatedCostUsd": round(cost, 6) if cost is not None else None,
            "costInUsdTicksSum": self.cost_in_usd_ticks or None,
            "pricingNote": (
                "estimatedCostUsd uses published xAI rates for grok-4-fast-* "
                "($0.20/1M input, $0.50/1M output) or grok-4.3 ($1.25/$2.50) by model id"
            ),
        }

    def print_summary(self, log_fn) -> None:
        d = self.as_dict()
        cost = d["estimatedCostUsd"]
        cost_s = f"${cost:.6f}" if cost is not None else "n/a"
        log_fn(
            f"LLM usage: calls={d['calls']} prompt={d['promptTokens']} "
            f"completion={d['completionTokens']} total={d['totalTokens']} "
            f"reasoning={d['reasoningTokens']} cached={d['cachedTokens']} "
            f"est_cost={cost_s} model={d['model']!r}"
        )


# Process-wide accumulator so the BYO callback and scrape() share one bucket.
USAGE = TokenUsage()


def reset_usage(model: str = "") -> TokenUsage:
    global USAGE
    USAGE = TokenUsage(model=model)
    return USAGE
