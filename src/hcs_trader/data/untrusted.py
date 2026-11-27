"""Sanitize and quarantine external text before any model or UI sees it.

External content is data, never instructions. This module does not decide
actions; it produces a marked envelope and strips common prompt-injection
patterns deterministically. Anything removed is recorded as metadata.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal

_MAX_CHARS = 20_000
_BIDI_AND_CONTROLS = re.compile(
    r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f"
    r"\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
_INSTRUCTION = re.compile(
    r"(ignore (all |the )?(previous|prior|above|system) "
    r"|disregard (all |the )?(previous|prior|above|system) "
    r"|system prompt|developer message|reveal (your|the) (key|seed|prompt)"
    r"|private key|seed phrase|sign (this|a) transaction"
    r"|transfer (all|the|funds)|execute (this|the) trade"
    r"|call the .*tool|new instructions follow)",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style", "noscript", "template", "svg"):
            self._skip += 1
        elif tag.lower() in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "template", "svg"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """A bounded, sanitized payload with provenance and an explicit envelope."""

    text: str
    source_id: str
    canonical_url: str
    envelope: Literal["UNTRUSTED"] = "UNTRUSTED"
    content_hash: str = ""
    retrieved_at: datetime | None = None
    removed_instructions: int = 0

    @property
    def safe_excerpt(self) -> str:
        """Short excerpt for display; never interpreted as instructions."""
        return self.text[:400]


def sanitize_html(raw: bytes, *, max_bytes: int = 1_048_576) -> str:
    """Extract visible text and drop active markup and invisible characters."""
    if len(raw) > max_bytes:
        raise ValueError("Fetched document exceeds the size cap")
    parser = _TextExtractor()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
    except ValueError as error:
        raise ValueError("Malformed fetched document") from error
    text = unicodedata.normalize("NFKC", "".join(parser.parts))
    text = _BIDI_AND_CONTROLS.sub(" ", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    if len(cleaned) > _MAX_CHARS:
        raise ValueError("Extracted text exceeds the character cap")
    return cleaned


def quarantine(text: str) -> tuple[str, int]:
    """Replace instruction-like lines; return the text and the removal count."""
    removed = 0
    kept: list[str] = []
    for line in text.splitlines():
        if _INSTRUCTION.search(line):
            removed += 1
            kept.append("[instruction-like text removed]")
        else:
            kept.append(line)
    return "\n".join(kept), removed


def as_untrusted(
    text: str,
    *,
    source_id: str,
    canonical_url: str,
    retrieved_at: datetime | None = None,
    removed_instructions: int = 0,
) -> UntrustedText:
    """Mark text as untrusted and bind it to its source and retrieval time."""
    if not source_id or not canonical_url:
        raise ValueError("Provenance is required")
    if retrieved_at is None:
        retrieved_at = datetime.now(UTC)
    elif retrieved_at.tzinfo is None:
        raise ValueError("Retrieval time must be timezone-aware")
    if len(text) > _MAX_CHARS:
        raise ValueError("Untrusted text exceeds the character cap")
    return UntrustedText(
        text=text,
        source_id=source_id,
        canonical_url=canonical_url,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        retrieved_at=retrieved_at,
        removed_instructions=removed_instructions,
    )
