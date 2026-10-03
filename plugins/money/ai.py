"""Best-effort bridges into Tomo's agent core: vision receipts and LLM extraction.

Every helper degrades gracefully — callers always have a heuristic fallback and
these functions return ``None`` instead of raising when no model is configured.
"""

from __future__ import annotations

import asyncio
import json
import re


def _run(coro):
    """Run a coroutine from a synchronous plugin context (worker thread)."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # Extremely defensive: never leak an event-loop error into a tool result.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _json_block(text: str) -> dict | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def extract_transaction(text: str, categories: list[str]) -> dict | None:
    """Extract {kind, amount, currency, category, day, merchant, note} from text."""
    if not text or not text.strip():
        return None

    async def _call():
        from app.runtime.llm import get_auxiliary_llm

        client = get_auxiliary_llm("money_extract")
        cats = ", ".join(categories[:40])
        prompt = (
            "Extract a single financial transaction from the text. Reply with JSON"
            " only: {\"kind\": \"expense\"|\"income\", \"amount\": number,"
            " \"currency\": \"IDR\"|ISO code, \"category\": one of [" + cats +
            "] or a short new name, \"day\": \"YYYY-MM-DD\" or null,"
            " \"merchant\": string or null, \"note\": short string}.\n\nText:\n"
            + text[:6000]
        )
        resp = await client.complete(
            [
                {"role": "system", "content": "You output strict JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        return resp.content

    try:
        return _json_block(_run(_call()) or "")
    except Exception:
        return None


def categorize(text: str, categories: list[str]) -> str | None:
    if not text.strip():
        return None

    async def _call():
        from app.runtime.llm import get_auxiliary_llm

        client = get_auxiliary_llm("money_categorize")
        resp = await client.complete(
            [
                {
                    "role": "system",
                    "content": "Reply with exactly one category name, nothing else.",
                },
                {
                    "role": "user",
                    "content": "Pick the best spending category for this "
                    f"transaction, choosing from: {', '.join(categories[:40])}. "
                    "If nothing fits, reply Uncategorized.\n\n" + text[:1000],
                },
            ]
        )
        return (resp.content or "").strip().strip('"').strip()

    try:
        value = _run(_call())
    except Exception:
        return None
    if not value or len(value) > 80:
        return None
    lowered = {c.lower(): c for c in categories}
    return lowered.get(value.lower(), value)


def describe_receipt(data_url: str, categories: list[str]) -> dict | None:
    """Vision-parse a receipt image into transaction fields."""
    async def _call():
        from app.runtime.llm.vision import analyze_image_data_url

        cats = ", ".join(categories[:40])
        prompt = (
            "This is a receipt or payment screenshot. Extract the transaction and"
            " reply with JSON only: {\"merchant\": string, \"amount\": number"
            " (the grand total paid), \"currency\": \"IDR\"|ISO code,"
            " \"day\": \"YYYY-MM-DD\" or null, \"category\": one of [" + cats +
            "] or a short new name, \"items\": [short item strings up to 6]}."
        )
        return await analyze_image_data_url(None, data_url, prompt)

    try:
        return _json_block(_run(_call()) or "")
    except Exception:
        return None
