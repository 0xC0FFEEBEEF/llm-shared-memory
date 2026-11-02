"""Credential protection.

Before any message is written we scan it for obvious secrets so that passwords,
API keys, and private keys never land in the shared memory. This is a
deterministic, offline heuristic (no model, no network call):

* **Key-based** -- if a JSON payload has a field whose name looks like a secret
  (``password``, ``api_key``, ``secret``, ``token``, ``private_key`` ...), the
  whole payload is refused.
* **Value-based** -- if the text or a payload value matches a known secret
  pattern (OpenAI-style keys, AWS access-key IDs, PEM private keys, bearer
  tokens, ``password=`` assignments), it is refused.

Heuristics intentionally err on the side of refusing. They are not a complete
secret scanner; when in doubt the message is not stored.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Field names that should never appear in a stored message.
_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "secret_key",
    "access_token",
    "access-key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "token",
    "auth",
    "authorization",
    "bearer",
    "private_key",
    "privatekey",
    "client_secret",
    "credential",
    "credentials",
    "cookie",
    "session_token",
}

# Value patterns for concrete secrets.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*(PRIVATE KEY)-----"),  # PEM private key
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style API key
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),  # Anthropic-style key
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ID
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}\b"),  # bearer token
    re.compile(r"(?i)password\s*=\s*\S+"),  # password=... assignment
    re.compile(r"(?i)passwd\s*=\s*\S+"),
    re.compile(r"(?i)\b(token|secret|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9\-_\.]{12,}['\"]?"),
]

_JSON_KEY_PATTERN = None  # unused; kept for reference only


def _looks_like_secret_key(name: str) -> bool:
    return name.strip().lower() in _SECRET_KEYS


def contains_credential(
    text: str,
    payload: Any = None,
    extra_keys: set[str] | None = None,
) -> bool:
    """Return True if ``text``/``payload`` appear to contain a secret."""
    # --- Key-based check over JSON payload keys (only if a payload is present) ---
    if isinstance(payload, dict):
        payload_keys: set[str] = set(payload.keys())
        # Recurse one level into nested dicts/lists.
        for v in payload.values():
            if isinstance(v, dict):
                payload_keys.update(v.keys())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        payload_keys.update(item.keys())
        # extra_keys: reject if the payload contains any of these specific fields.
        if extra_keys and (extra_keys & payload_keys):
            return True
        if any(_looks_like_secret_key(k) for k in payload_keys):
            return True

    # --- Value-based check over the raw text ---
    if _matches_pattern(text):
        return True

    # --- Value-based check over payload string values ---
    if isinstance(payload, dict):
        for v in _iter_values(payload):
            if isinstance(v, str) and _matches_pattern(v):
                return True
    elif isinstance(payload, str):
        if _matches_pattern(payload):
            return True
    elif payload is not None:
        # Try to serialise non-dict payloads and scan the string form.
        try:
            if _matches_pattern(json.dumps(payload)):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _iter_values(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield v
    elif isinstance(obj, list):
        for v in obj:
            yield v


def _matches_pattern(text: str) -> bool:
    if not text:
        return False
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    # A bare high-entropy token on a line that names a secret.
    for line in text.splitlines():
        if re.search(r"(?i)(secret|token|api[_-]?key|password|apikey)", line) and \
           re.search(r"[A-Za-z0-9\-_\.]{32,}", line):
            return True
    return False
