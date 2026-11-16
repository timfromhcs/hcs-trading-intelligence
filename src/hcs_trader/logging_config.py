"""Structured JSON logging with secret redaction and correlation IDs."""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id: ContextVar[str | None] = ContextVar("hcs_correlation_id", default=None)

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = (
    r"api[_-]?key|api[_-]?secret|secret|passphrase|password|passwd|token|"
    r"authorization|private[_-]?key|seed|mnemonic|x-hcs-token|api-sign|api-key"
)
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer xxx / Basic xxx
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}"),
    # key=value, key: value, "key": "value" for sensitive key names
    re.compile(
        rf"(?i)([\"']?(?:[a-z0-9_\-]*(?:{_SENSITIVE_KEYS})[a-z0-9_\-]*)[\"']?"
        r"\s*[:=]\s*)([\"']?)[^\s\"',;&}]+\2"
    ),
    # api_key in query strings is covered above; also long base64-like secrets
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)


def redact(text: str) -> str:
    """Return ``text`` with credential-looking material replaced."""
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _PATTERNS[2].sub(REDACTED, text)


def redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: REDACTED
            if re.search(_SENSITIVE_KEYS, str(k), re.IGNORECASE)
            else redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()
        return True


class RedactingFilter(logging.Filter):
    """Redacts the rendered message, args and exception text of each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", None)
            or correlation_id.get(),
        }
        if record.exc_text or record.exc_info:
            exc = record.exc_text or self.formatException(record.exc_info)  # type: ignore[arg-type]
            payload["exception"] = redact(exc)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(level: int = logging.INFO) -> logging.Handler:
    """Install a JSON, redacting, correlation-aware handler on the root logger."""
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_hcs", False)]
    handler._hcs = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return handler
