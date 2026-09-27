"""In-memory PII and sensitive data sanitizer for logs, traces, and artifacts."""

import dataclasses
import re
from typing import Any, Dict, Optional, Set


# Compiled regex patterns for fast in-memory execution (INV-06, INV-27)
SSN_REGEX = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b|\b\d{9}\b")
# Supports Visa, MC, Discover, and Amex 15-digit 4-6-5 format (INV-27)
CARD_REGEX = re.compile(
    r"\b(?:3[47]\d{2}[- ]?\d{6}[- ]?\d{5}|(?:4\d{3}|5[1-5]\d{2}|222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720|6011)[- ]?\d{4}[- ]?\d{4}[- ]?\d{1,4})\b"
)
ACCOUNT_REGEX = re.compile(r"\b(?:SAV|CHK|ACC)-\d{4}-\d{2}\b")
PHONE_REGEX = re.compile(r"(?:\b|\+?1[-. ]?)(?:\([0-9]{3}\)|[0-9]{3})[-. ]?[0-9]{3}[-. ]?[0-9]{4}\b")
# Requires keyword/symbol prefix to avoid collaterally corrupting 10-digit Unix epoch timestamps (INV-06, INV-27)
GENERIC_ACCT_REGEX = re.compile(r"(?i)(?:\b(?:ACCOUNT|ACCT|ACC|A/C|NO)|#)\s*[:#-]?\s*\b\d{8,16}\b")
EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Supports JSON quoted keys as well as bare words and unquoted parameter formats, without catastrophic backtracking
INLINE_CRED_QUOTED_REGEX = re.compile(
    r'(?i)(["\']?)\b(password|passwd|pwd|token|secret|pin|cvv|cvc|bearer|api[-_]?key)\b\1(\s*[:=]\s*)(["\'])([^"\'\\]*(?:\\.[^"\'\\]*)*)\4'
)
INLINE_CRED_UNQUOTED_REGEX = re.compile(
    r'(?i)(["\']?)\b(password|passwd|pwd|token|secret|pin|cvv|cvc|api[-_]?key)\b\1(\s*[:=]\s*)([^"\'\s,;&]+)'
)
# Matches space-delimited standard HTTP Authorization: Bearer/Basic/Token <token>
BEARER_AUTH_REGEX = re.compile(r'(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=-]+)')

DIGIT_SEARCH_REGEX = re.compile(r"\d")
# Word-bounded tokens prevent false positive matches on 'passed', 'passenger', 'keyword', 'keyboard'
HAS_CRED_TOKENS_REGEX = re.compile(
    r"(?i)\b(?:pass(?:word|phrase|wd)?|pwd|token|secret|pin|cvv|cvc|bearer|basic|api[-_]?key|cred(?:ential)?|auth(?:oriz(?:ation)?)?|ssn)\b"
)

# Supports snake_case, camelCase, and bare words without matching 'shipping', 'passenger', or 'author'
SENSITIVE_KEY_REGEX = re.compile(
    r"(?:^|[._\-])(?:password|passwd|pass|pwd|ssn|tax_?id|secret|token|api_?key|override_?code|credential|auth|bearer|cvv|cvc|account_?number|card_?number|dob|pin)(?:$|[._\-])|"
    r"(?:user|account|member|master)?(?:Password|Passwd|Secret|Token|ApiKey|OverrideCode|Credential|Ssn|TaxId|Cvv|Cvc|CardNumber|AccountNumber|Pin|Pwd)|"
    r"\b(?:password|passwd|secret|token|credential|bearer|pin|pwd)\b",
    re.IGNORECASE,
)


def _deep_freeze(val: Any) -> Any:
    """Recursively freeze unhashable dicts and lists into hashable tuples for set insertion, handling heterogeneous key types."""
    if isinstance(val, dict):
        return tuple((_deep_freeze(k), _deep_freeze(v)) for k, v in sorted(val.items(), key=lambda item: str(item[0])))
    elif isinstance(val, (list, set, tuple)):
        return tuple(_deep_freeze(x) for x in val)
    return val


class PIIRedactor:
    """Zero-persistence PII scrubber removing financial and personal data from runtime text."""

    @classmethod
    def redact_text(cls, text: str) -> str:
        """Sanitize raw string replacing PII with standardized tokens."""
        if not text or not isinstance(text, str):
            return text

        # Fast C-level pre-filter (INV-06)
        has_digits = bool(DIGIT_SEARCH_REGEX.search(text))
        has_at = "@" in text
        has_cred_tokens = bool(HAS_CRED_TOKENS_REGEX.search(text))

        if not has_digits and not has_at and not has_cred_tokens:
            return text

        redacted = text
        if has_cred_tokens:
            # Scrub space-delimited bearer auth and quoted/unquoted credentials
            redacted = BEARER_AUTH_REGEX.sub(r"\1 [REDACTED_CREDENTIAL]", redacted)
            redacted = INLINE_CRED_QUOTED_REGEX.sub(r"\1\2\1\3\4[REDACTED_CREDENTIAL]\4", redacted)
            redacted = INLINE_CRED_UNQUOTED_REGEX.sub(r"\1\2\1\3[REDACTED_CREDENTIAL]", redacted)

        if has_digits:
            # Contextual accounts precede bare numeric sequences to prevent SSN collisions (INV-27)
            redacted = ACCOUNT_REGEX.sub("[REDACTED_ACCT]", redacted)
            redacted = GENERIC_ACCT_REGEX.sub("[REDACTED_ACCT]", redacted)
            redacted = CARD_REGEX.sub("[REDACTED_CARD]", redacted)
            redacted = PHONE_REGEX.sub("[REDACTED_PHONE]", redacted)
            redacted = SSN_REGEX.sub("[REDACTED_SSN]", redacted)

        if has_at:
            redacted = EMAIL_REGEX.sub("[REDACTED_EMAIL]", redacted)

        return redacted

    @classmethod
    def redact_object(cls, obj: Any, visited: Optional[Set[int]] = None) -> Any:
        """Recursively redact sensitive values with branch-isolated cycle detection."""
        if visited is None:
            visited = set()

        if isinstance(obj, type):
            return obj

        obj_id = id(obj)
        if obj_id in visited:
            return "[CIRCULAR_REF]"

        # Register visited id BEFORE dumping Pydantic models or dataclasses to prevent infinite recursion
        branch_visited = visited | {obj_id}

        # Handle Python BaseException objects to scrub args and prevent PII leak (INV-28)
        if isinstance(obj, BaseException):
            clean_args = [cls.redact_object(arg, branch_visited) for arg in obj.args]
            return f"{type(obj).__name__}({', '.join(repr(a) for a in clean_args)})"

        # Handle binary byte payloads (INV-28)
        if isinstance(obj, (bytes, bytearray)):
            try:
                decoded = obj.decode("utf-8")
                redacted_str = cls.redact_text(decoded)
                return redacted_str.encode("utf-8") if isinstance(obj, bytes) else bytearray(redacted_str.encode("utf-8"))
            except UnicodeDecodeError:
                return b"[BINARY_DATA]" if isinstance(obj, bytes) else bytearray(b"[BINARY_DATA]")

        if hasattr(obj, "model_dump") and callable(obj.model_dump) and not isinstance(obj, type):
            try:
                obj = obj.model_dump(mode="json")
            except Exception:
                obj = vars(obj) if hasattr(obj, "__dict__") else str(obj)
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            try:
                obj = dataclasses.asdict(obj)
            except Exception:
                obj = vars(obj) if hasattr(obj, "__dict__") else str(obj)

        if isinstance(obj, dict):
            clean_dict: Dict[str, Any] = {}
            for k, v in obj.items():
                k_clean = cls.redact_text(str(k)) if isinstance(k, str) else k
                k_str = str(k)
                if SENSITIVE_KEY_REGEX.search(k_str):
                    clean_dict[k_clean] = "[REDACTED_CREDENTIAL]"
                else:
                    clean_dict[k_clean] = cls.redact_object(v, branch_visited)
            return clean_dict

        if isinstance(obj, (list, tuple, set)):
            redacted_seq = [cls.redact_object(item, branch_visited) for item in obj]
            if isinstance(obj, tuple):
                return tuple(redacted_seq)
            if isinstance(obj, set):
                try:
                    return set(redacted_seq)
                except TypeError:
                    # Recursively freeze unhashable items into tuples to preserve set type fidelity
                    return {_deep_freeze(item) for item in redacted_seq}
            return redacted_seq

        if isinstance(obj, str):
            return cls.redact_text(obj)

        if hasattr(obj, "__dict__") and not isinstance(obj, type):
            try:
                return cls.redact_object(vars(obj), branch_visited)
            except Exception:
                return obj

        if not isinstance(obj, type):
            all_slots = set()
            for cls_ in getattr(type(obj), "__mro__", ()):
                raw_slots = getattr(cls_, "__slots__", None)
                if isinstance(raw_slots, str):
                    all_slots.add(raw_slots)
                elif isinstance(raw_slots, (tuple, list, set)):
                    all_slots.update(raw_slots)
            if all_slots:
                try:
                    slots_dict = {s: getattr(obj, s, None) for s in all_slots if hasattr(obj, s)}
                    return cls.redact_object(slots_dict, branch_visited)
                except Exception:
                    return obj

        return obj

    @classmethod
    def redact_dict(cls, d: Any) -> Any:
        """Convenience alias for redact_object on dictionaries and key-value mappings."""
        return cls.redact_object(d)
