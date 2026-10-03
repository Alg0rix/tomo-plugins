"""Capture pipeline: emails, receipt images, and CSV rows → inbox candidates."""

from __future__ import annotations

import base64
import csv
from datetime import date, datetime, timezone
import hashlib
from html import unescape
from io import StringIO
import re

from . import ledger

_INCOME_HINTS = re.compile(
    r"refund|cashback|cash.?back|top.?up|received|diterima|terima|"
    r"transfer.*masuk|incoming|credit(?! card)|setoran|gaji|salary|payout",
    re.IGNORECASE,
)

# Amount patterns tried in priority order — first labeled/total lines, then any
# currency figure. IDR defaults to 1:1 units; other ISO codes map through *100.
_AMOUNT_LABELED = re.compile(
    r"(?:total|amount|jumlah|tagihan|payment|charge[sd]?|debit|paid|bayar|"
    r"transaction|nominal|pembayaran|belanja|spent)\s*"
    r"(?:of|:|-)?\s*"
    r"(?:rp\.?\s?|idr\s?|usd?\s?|\$\s?|sgd\s?|myr\s?|eur\s?|€\s?|£\s?|gbp\s?|jpy\s?|¥\s?)"
    r"([\d][\d.,]*)",
    re.IGNORECASE,
)
_AMOUNT_GENERIC = re.compile(
    r"(rp\.?|idr|usd|\$|sgd|myr|eur|€|£|gbp|jpy|¥)\s*([\d][\d.,]*)",
    re.IGNORECASE,
)

_CURRENCY_WORDS = {
    "rp": "IDR", "rp.": "IDR", "idr": "IDR", "$": "USD", "usd": "USD",
    "sgd": "SGD", "myr": "MYR", "eur": "EUR", "€": "EUR", "£": "GBP",
    "gbp": "GBP", "jpy": "JPY", "¥": "JPY",
}

_MERCHANT_BLACKLIST = re.compile(
    r"no.?reply|notification|alerts?|info|mail|support|newsletter|digest|updates",
    re.IGNORECASE,
)


def _minor(amount_text: str, currency: str) -> int | None:
    """Convert a localized figure to minor units. A single trailing ',' or '.'
    followed by 1-2 digits is a decimal separator; everything else is treated
    as thousand grouping (IDR convention: dots group thousands)."""
    text = re.sub(r"[^\d.,]", "", amount_text or "")
    if not text:
        return None
    try:
        decimal_match = re.search(r"([.,])(\d{1,2})$", text)
        if decimal_match:
            head = re.sub(r"[.,]", "", text[: decimal_match.start()])
            frac = decimal_match.group(2).ljust(2, "0")
            if not head:
                return None
            return int(head) * 100 + int(frac)
        digits = re.sub(r"[.,]", "", text)
        return int(digits) * 100 if digits else None
    except ValueError:
        return None


def extract_amount(text: str) -> tuple[int, str] | None:
    match = _AMOUNT_LABELED.search(text)
    figure = match.group(1) if match else None
    currency = "IDR"
    if match:
        head = match.group(0).lower()
        for word, code in _CURRENCY_WORDS.items():
            if word in head:
                currency = code
                break
    if figure is None:
        best = None
        for generic in _AMOUNT_GENERIC.finditer(text):
            code = _CURRENCY_WORDS.get(generic.group(1).lower(), "IDR")
            minor = _minor(generic.group(2), code)
            if minor and (best is None or minor > best[0]):
                best = (minor, code)
        return best
    minor = _minor(figure, currency)
    return (minor, currency) if minor else None


def extract_merchant(sender: str, subject: str) -> str:
    sender = sender or ""
    display = re.sub(r"<.*>", "", sender).strip().strip('"')
    if display and not _MERCHANT_BLACKLIST.search(display):
        display = re.sub(r"\s+", " ", display)
        if 2 <= len(display) <= 48:
            return display
    domain = re.search(r"@([\w.-]+)", sender)
    if domain:
        host = domain.group(1).split(".")
        brand = host[-2] if len(host) >= 2 else host[0]
        if brand and not _MERCHANT_BLACKLIST.search(brand):
            return brand.replace("-", " ").title()
    subject_clean = re.sub(
        r"(?i)\b(receipt|receipts|e-?receipt|order|payment|invoice|confirmation|"
        r"transaction|alert|notif\w*|your|for|from|struk|bukti|pembayaran)\b",
        " ",
        subject or "",
    )
    subject_clean = re.sub(r"[\[\]()*_#:|-]", " ", subject_clean)
    subject_clean = re.sub(r"\s+", " ", subject_clean).strip()
    if 2 <= len(subject_clean) <= 48:
        return subject_clean.title()
    return "Unknown merchant"


def parse_email_candidate(
    sender: str, subject: str, body: str, when: date | None = None
) -> dict | None:
    """Heuristic parse of a payment/receipt email into a proposed transaction."""
    text = f"{subject or ''}\n{body or ''}"
    found = extract_amount(text)
    if not found:
        return None
    amount_minor, currency = found
    if amount_minor <= 0 or amount_minor > 10**12:
        return None
    kind = "income" if _INCOME_HINTS.search(subject or "") else "expense"
    merchant = extract_merchant(sender, subject)
    day = (when or date.today()).isoformat()
    note = (subject or "").strip()[:120]
    return {
        "kind": kind,
        "amount": f"{amount_minor / 100:.2f}",
        "amount_minor": amount_minor,
        "currency": currency,
        "category": "Uncategorized",
        "merchant": merchant,
        "note": note,
        "day": day,
    }


def html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        return unescape(BeautifulSoup(html, "html.parser").get_text("\n"))
    except Exception:
        return unescape(re.sub(r"<[^>]+>", " ", html or ""))


def gmail_message_content(message: dict) -> dict:
    """Decode a Gmail API message resource into readable parts."""
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (message.get("payload", {}).get("headers") or [])
    }
    body_parts: list[str] = []

    def walk(part):
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                decoded = base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", "replace"
                )
            except Exception:
                decoded = ""
            if decoded:
                body_parts.append(
                    decoded if mime == "text/plain" else html_to_text(decoded)
                )
        for child in part.get("parts") or []:
            walk(child)

    walk(message.get("payload") or {})
    when = None
    internal = message.get("internalDate")
    if internal:
        try:
            when = datetime.fromtimestamp(
                int(internal) / 1000, tz=timezone.utc
            ).date()
        except (ValueError, OSError):
            when = None
    return {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", "") or (message.get("snippet") or ""),
        "snippet": message.get("snippet") or "",
        "body": "\n".join(body_parts),
        "when": when,
    }


def llm_email_candidate(content: dict, categories: list[str]) -> dict | None:
    """Use the configured LLM to extract a transaction from an email."""
    from . import ai

    text = (
        f"From: {content['from']}\nSubject: {content['subject']}\n\n"
        + content["body"]
    )
    fields = ai.extract_transaction(text, categories)
    if not fields:
        return None
    kind = str(fields.get("kind") or "expense").lower()
    if kind not in ("income", "expense"):
        kind = "expense"
    try:
        minor = ledger.parse_amount_minor(fields.get("amount"))
    except ledger.ValidationError:
        return None
    if minor <= 0 or minor > 10**12:
        return None
    when = content.get("when") or date.today()
    try:
        day = ledger.parse_day(fields.get("day"))
    except ledger.ValidationError:
        day = when.isoformat()
    merchant = str(fields.get("merchant") or "")[:80] or extract_merchant(
        content["from"], content["subject"]
    )
    note = str(fields.get("note") or content["subject"] or "")[:120] or merchant
    return {
        "kind": kind,
        "amount": f"{minor / 100:.2f}",
        "amount_minor": minor,
        "currency": str(fields.get("currency") or "IDR")[:8].upper(),
        "category": str(fields.get("category") or "Uncategorized")[:80],
        "merchant": merchant,
        "note": note,
        "day": day,
    }


def parse_gmail_message(
    message: dict, categories: list[str] | None = None
) -> dict | None:
    """Extract a candidate transaction from a Gmail API message resource.

    Prefers the configured LLM when categories are supplied; falls back to
    amount/merchant heuristics when no model is available.
    """
    content = gmail_message_content(message)
    candidate = None
    if categories:
        candidate = llm_email_candidate(content, categories)
    if candidate is None:
        candidate = parse_email_candidate(
            content["from"], content["subject"], content["body"], content["when"]
        )
    if candidate is None:
        return None
    provenance = {
        "external_key": f"gmail:{content['id']}",
        "gmail_thread": content["thread_id"],
        "from": content["from"][:200],
        "subject": content["subject"][:200],
        "snippet": content["snippet"][:240],
    }
    return {"payload": candidate, "provenance": provenance}


# --- Receipt images -----------------------------------------------------------

def receipt_to_candidate(data_url: str, categories: list[str]) -> dict | None:
    from . import ai

    fields = ai.describe_receipt(data_url, categories)
    if not fields:
        return None
    amount = fields.get("amount")
    try:
        minor = ledger.parse_amount_minor(amount)
    except ledger.ValidationError:
        return None
    day = fields.get("day") or date.today().isoformat()
    try:
        day = ledger.parse_day(day)
    except ledger.ValidationError:
        day = date.today().isoformat()
    items = [str(item)[:60] for item in (fields.get("items") or [])][:6]
    merchant = str(fields.get("merchant") or "Receipt")[:80]
    return {
        "kind": "expense",
        "amount": f"{minor / 100:.2f}",
        "amount_minor": minor,
        "currency": str(fields.get("currency") or "IDR")[:8].upper(),
        "category": str(fields.get("category") or "Uncategorized")[:80],
        "merchant": merchant,
        "note": ", ".join(items)[:500] if items else merchant,
        "day": day,
    }


def image_data_url(raw: bytes, mime: str) -> str | None:
    from app.runtime.llm.vision_image import encode_image_data_url, guess_image_mime

    if not raw:
        return None
    mime = mime if mime.startswith("image/") else guess_image_mime("receipt.jpg", raw)
    try:
        data_url, _ = encode_image_data_url(raw, mime)
    except ValueError:
        return None
    return data_url


# --- CSV import ---------------------------------------------------------------

_CSV_ALIASES = {
    "day": ("day", "date", "tanggal", "tgl", "time", "posted"),
    "kind": ("kind", "type", "tipe", "direction", "flow"),
    "amount": ("amount", "nominal", "jumlah", "total", "value", "idr", "rp"),
    "category": ("category", "kategori", "cat", "label"),
    "note": ("note", "notes", "description", "desc", "keterangan", "memo", "merchant", "payee"),
    "source": ("source", "account", "wallet", "akun", "rekening", "bank"),
    "currency": ("currency", "curr", "mata uang"),
}


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (value or "").strip().lower())


def _map_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map CSV headers to canonical fields; returns {} when unreadable."""
    normalized = {name: _norm_header(name) for name in fieldnames or []}
    mapping: dict[str, str] = {}
    for canonical, aliases in _CSV_ALIASES.items():
        for original, norm in normalized.items():
            if norm in aliases or any(norm.startswith(a) for a in aliases):
                mapping[canonical] = original
                break
    return mapping if "amount" in mapping else {}


def _csv_kind(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"income", "in", "credit", "cr", "masuk", "pemasukan", "debit_in"}:
        return "income"
    if text in {"expense", "out", "debit", "db", "keluar", "pengeluaran", "spent"}:
        return "expense"
    return ""


def _flex_day(value: str) -> str:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[:24], fmt).date().isoformat()
        except ValueError:
            continue
    return ledger.parse_day(text)


def import_csv(conn, text: str) -> dict:
    """Bulk-import CSV rows directly into the ledger. Returns a report dict."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    mapping = _map_columns(reader.fieldnames or [])
    if not mapping:
        raise ledger.ValidationError(
            "Could not detect an amount column — expect headers like "
            "date, amount, type, category, note"
        )
    imported = skipped = duplicates = 0
    errors: list[str] = []
    source_ids = {s["name"].lower(): s["id"] for s in ledger.list_sources(conn)}
    for index, row in enumerate(reader, start=2):
        if index > 5000:
            break
        raw_amount = (row.get(mapping["amount"]) or "").strip()
        if not raw_amount:
            skipped += 1
            continue
        kind = _csv_kind(row.get(mapping.get("kind") or "", ""))
        negative = raw_amount.startswith("-") or raw_amount.startswith("(")
        if not kind:
            kind = "income" if negative else "expense"
        cleaned = raw_amount.strip("()").replace("-", "")
        fields = {
            "kind": kind,
            "amount": cleaned,
            "category": (row.get(mapping.get("category") or "", "") or "").strip()
            or "Uncategorized",
            "note": (row.get(mapping.get("note") or "", "") or "").strip()[:500],
            "day": row.get(mapping.get("day") or "", "") or "",
            "currency": (row.get(mapping.get("currency") or "", "") or "IDR")
            .strip()[:8]
            or "IDR",
        }
        source_name = (row.get(mapping.get("source") or "", "") or "").strip().lower()
        if source_name and source_name in source_ids:
            fields["source_id"] = source_ids[source_name]
        try:
            fields["day"] = _flex_day(fields["day"])
        except ledger.ValidationError:
            errors.append(f"row {index}: bad date {fields['day']!r}")
            skipped += 1
            continue
        digest = hashlib.sha256(
            "|".join(
                str(fields.get(k) or "") for k in ("kind", "amount", "day", "note")
            ).encode()
        ).hexdigest()[:24]
        fields["external_key"] = f"csv:{digest}"
        try:
            result = ledger.add_transaction(conn, fields)
            if result.get("duplicate"):
                duplicates += 1
            else:
                imported += 1
        except ledger.ValidationError as exc:
            errors.append(f"row {index}: {exc}")
            skipped += 1
    return {
        "imported": imported,
        "skipped": skipped,
        "duplicates": duplicates,
        "errors": errors[:8],
        "columns": mapping,
    }
