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
        return asyncio.run(asyncio.wait_for(coro, timeout=20))
    except RuntimeError:
        # Extremely defensive: never leak an event-loop error into a tool result.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=20))
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


async def _complete(client, messages):
    try:
        return await client.complete(messages)
    finally:
        await client.aclose()


def _hint_block(hints) -> str:
    lines = []
    for item in (hints or [])[:20]:
        if not isinstance(item, dict):
            continue
        merchant = str(item.get("merchant") or "").strip()
        category = str(item.get("category") or "").strip()
        if not merchant or not category or len(merchant) > 80 or len(category) > 80:
            continue
        lines.append(f"- {merchant} → {category}")
    if not lines:
        return ""
    return (
        "Known merchant categories from this user's ledger. Prefer them when the"
        " merchant matches:\n" + "\n".join(lines) + "\n\n"
    )


def extract_transaction(
    text: str, categories: list[str], hints: list[dict] | None = None
) -> dict | None:
    """Extract {kind, amount, currency, category, day, merchant, note} from text."""
    if not text or not text.strip():
        return None

    async def _call():
        from app.runtime.llm import get_auxiliary_llm

        client = get_auxiliary_llm("money_extract")
        cats = ", ".join(categories[:40])
        prompt = (
            "Extract a single financial transaction from the text. Reply with JSON"
            ' only: {"kind": "expense"|"income", "amount": number,'
            ' "currency": "IDR"|ISO code, "category": one of ['
            + cats
            + '] or a short new name, "day": "YYYY-MM-DD" or null,'
            ' "merchant": string or null, "note": short string,'
            ' "method": short lowercase payment rail or null'
            " (e.g. qris, cash, debit, credit, transfer, e-wallet)}.\n\n"
            + _hint_block(hints)
            + "Text:\n"
            + text[:6000]
        )
        resp = await _complete(
            client,
            [
                {"role": "system", "content": "You output strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.content

    try:
        return _json_block(_run(_call()) or "")
    except Exception:
        return None


_REVIEW_FIELDS = (
    "kind",
    "amount",
    "currency",
    "category",
    "merchant",
    "note",
    "day",
    "method",
)


def review_candidate(
    payload: dict, categories: list[str], hints: list[dict] | None = None
) -> dict | None:
    """One triage call: approve, edit, or dismiss. None means leave it pending."""
    if not isinstance(payload, dict):
        return None

    async def _call():
        from app.runtime.llm import get_auxiliary_llm

        client = get_auxiliary_llm("money_review")
        capture = {key: payload.get(key) for key in _REVIEW_FIELDS}
        prompt = (
            "Decide what to do with this parsed payment capture. Reply with JSON"
            ' only: {"action": "approve"|"edit"|"dismiss"}. approve = the fields'
            " are a real payment and look right. edit = fix fields that disagree"
            " with the categories or the user's corrections; include only the"
            " fields you change (kind, amount, currency, category, merchant, note,"
            " day, method). dismiss = not a payment (newsletter, balance, marketing).\n\n"
            f"Categories: {', '.join(categories[:40])}\n"
            + _hint_block(hints)
            + "Capture:\n"
            + json.dumps(capture, default=str)[:4000]
        )
        resp = await _complete(
            client,
            [
                {"role": "system", "content": "You output strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.content

    try:
        parsed = _json_block(_run(_call()) or "")
    except Exception:
        return None
    if not parsed:
        return None
    action = parsed.get("action")
    if action not in ("approve", "edit", "dismiss"):
        return None
    fields = {
        key: parsed[key]
        for key in _REVIEW_FIELDS
        if parsed.get(key) not in (None, "")
    }
    if action == "edit" and not fields:
        action = "approve"
    return {"action": action, **fields}


def categorize(text: str, categories: list[str]) -> str | None:
    if not text.strip():
        return None

    async def _call():
        from app.runtime.llm import get_auxiliary_llm

        client = get_auxiliary_llm("money_categorize")
        resp = await _complete(
            client,
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
            ],
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
            ' reply with JSON only: {"merchant": string, "amount": number'
            ' (the grand total paid), "currency": "IDR"|ISO code,'
            ' "day": "YYYY-MM-DD" or null, "category": one of ['
            + cats
            + '] or a short new name, "items": [short item strings up to 6],'
            ' "method": short lowercase payment rail or null'
            " (e.g. qris, cash, debit, credit)}."
        )
        return await analyze_image_data_url(None, data_url, prompt)

    try:
        return _json_block(_run(_call()) or "")
    except Exception:
        return None
