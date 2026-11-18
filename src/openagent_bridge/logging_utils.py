"""Logging utilities with secret redaction for OpenAgent Bridge.

Provides a ``SecretRedactor`` that scrubs sensitive values from log output,
and a ``setup_logging`` helper that wires up file + console handlers with
automatic redaction.

.. warning::

    Redaction is a *defence-in-depth* measure.  It is **not** an absolute
    security guarantee — novel secret formats, non-UTF-8 encodings, or
    split-line secrets can bypass the patterns below.  Never treat redacted
    logs as safe for public sharing without manual review.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Each entry is (compiled regex, replacement).  Order matters only when
# patterns overlap — more specific patterns go first.
_DEFAULT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- API keys by prefix -------------------------------------------------
    # OpenAI keys: sk-... (48-64 chars)
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # Anthropic keys: sk-ant-...
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_ prefixed
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}"), "[REDACTED]"),

    # --- Environment variable assignments ------------------------------------
    # OPENAI_API_KEY=…, ANTHROPIC_API_KEY=…, GITHUB_TOKEN=…
    (re.compile(
        r"((?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN)"
        r"\s*=\s*)[^\s\"']+"),
     r"\1[REDACTED]"),
    # AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, etc.
    (re.compile(r"(AWS_[A-Z_]*(?:KEY|TOKEN|SECRET)\s*=\s*)[^\s\"']+"),
     r"\1[REDACTED]"),
    # AZURE_* secrets
    (re.compile(r"(AZURE_[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|CONNECTION_STRING)\s*=\s*)[^\s\"']+"),
     r"\1[REDACTED]"),

    # --- HTTP headers --------------------------------------------------------
    # Authorization: Bearer <token>
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE),
     r"\1[REDACTED]"),

    # --- Password fields (JSON / YAML / query-string style) ------------------
    (re.compile(
        r"([\"']?password[\"']?\s*[:=]\s*)[\"']?[^\s,;\"'}{]+[\"']?",
        re.IGNORECASE),
     r"\1[REDACTED]"),

    # --- PEM private key blocks ---------------------------------------------
    (re.compile(
        r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
        r"[\s\S]*?"
        r"-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"),
     "[REDACTED-PEM-KEY]"),

    # --- Cookie values -------------------------------------------------------
    # Cookie: name=value or Set-Cookie: name=value; …
    (re.compile(r"((?:Set-)?Cookie:\s*)(\S+)", re.IGNORECASE),
     r"\1[REDACTED]"),
]

_PLACEHOLDER = "[REDACTED]"


class SecretRedactor:
    """Apply regex-based redaction to arbitrary text.

    Parameters
    ----------
    extra_patterns:
        Additional ``(compiled_regex, replacement)`` pairs appended *after*
        the built-in list.
    """

    def __init__(
        self,
        extra_patterns: Sequence[tuple[re.Pattern[str], str]] | None = None,
    ) -> None:
        self._patterns: list[tuple[re.Pattern[str], str]] = list(
            _DEFAULT_PATTERNS
        )
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def redact(self, text: str) -> str:
        """Return *text* with all matched secret patterns replaced."""
        for pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class _RedactingFormatter(logging.Formatter):
    """Formatter that passes each formatted record through a redactor."""

    def __init__(
        self,
        redactor: SecretRedactor,
        fmt: str | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return self._redactor.redact(original)


def setup_logging(
    log_dir: Path | str,
    *,
    level: int = logging.DEBUG,
    console_level: int = logging.INFO,
    redactor: SecretRedactor | None = None,
) -> logging.Logger:
    """Configure the ``openagent_bridge`` logger with file + console output.

    Parameters
    ----------
    log_dir:
        Directory where ``bridge.log`` will be created (parents are made
        automatically).
    level:
        Root level for the file handler.
    console_level:
        Level for the console (stderr) handler.
    redactor:
        Custom :class:`SecretRedactor`; a default one is created if *None*.

    Returns
    -------
    logging.Logger
        The configured ``openagent_bridge`` logger.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    if redactor is None:
        redactor = SecretRedactor()

    logger = logging.getLogger("openagent_bridge")
    logger.setLevel(level)
    # Avoid duplicate handlers on repeated calls.
    logger.handlers.clear()

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # File handler — full detail
    file_handler = logging.FileHandler(
        log_dir / "bridge.log", encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(_RedactingFormatter(redactor, fmt=fmt))
    logger.addHandler(file_handler)

    # Console handler — summary level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(_RedactingFormatter(redactor, fmt=fmt))
    logger.addHandler(console_handler)

    return logger
