"""Domain and route allowlist enforcement for safety policy."""

import urllib.parse
from typing import List, Optional, Set
from src.guardrails.pii_redactor import PIIRedactor


class SecurityPolicyViolation(Exception):
    """Raised when an action or navigation violates security boundaries."""


class RouteAllowlist:
    """Strict domain and route validator."""

    def __init__(self, allowed_domains: Optional[List[str]] = None) -> None:
        raw_domains = allowed_domains if allowed_domains is not None else ["localhost:8000", "127.0.0.1:8000"]
        self.allowed_domains: Set[str] = set()
        for d in raw_domains:
            clean = str(d).lower().strip().rstrip(".")
            clean_target = f"http://{clean}" if not clean.startswith(("http://", "https://")) else clean
            try:
                parts = urllib.parse.urlsplit(clean_target)
                host = (parts.hostname or "").rstrip(".").replace("[", "").replace("]", "")
                try:
                    host = host.encode("idna").decode("ascii")
                except Exception:
                    pass
                port = parts.port
                if host:
                    self.allowed_domains.add(f"{host}:{port}" if port else host)
            except Exception:
                fallback = clean.split("/")[0].split("?")[0].split("#")[0].replace("[", "").replace("]", "")
                fallback = fallback.split("@")[-1]
                if fallback:
                    self.allowed_domains.add(fallback)

    def is_url_allowed(self, url: str) -> bool:
        """Check if a URL belongs to the strictly permitted domain allowlist."""
        if not isinstance(url, str) or not url.strip():
            return False
        url = url.strip()

        # Reject whitespace/control characters or null bytes (INV-06, INV-25)
        if any(c in url for c in ("\0", "\r", "\n", "\t", "\v", "\f")):
            return False

        # Reject backslash relative path bypasses (e.g. '/\\evil.com') (INV-25)
        if "\\" in url:
            return False

        # Allow safe relative internal paths
        if url.startswith("/") and not url.startswith("//"):
            return True

        # Special safe schemes (case-insensitive exact match or permitted query/hash suffix)
        url_lower = url.lower()
        if url_lower == "about:blank" or url_lower.startswith(("about:blank?", "about:blank#")):
            return True

        try:
            parsed = urllib.parse.urlparse(url)
            # Enforce allowed schemes
            if parsed.scheme not in ("http", "https"):
                return False

            raw_host = (parsed.hostname or "").lower().rstrip(".")
            hostname = raw_host.replace("[", "").replace("]", "")
            try:
                hostname = hostname.encode("idna").decode("ascii")
            except Exception:
                pass
            if not hostname:
                return False

            port = parsed.port
            default_port = 80 if parsed.scheme == "http" else 443
            effective_port = port if port is not None else default_port

            host_with_port = f"{hostname}:{effective_port}"
            # Strict port checking to prevent SSRF against alternate daemon ports (INV-25)
            return (
                host_with_port in self.allowed_domains
                or (port in (None, default_port) and hostname in self.allowed_domains)
            )
        except (ValueError, Exception):
            return False

    def assert_url_allowed(self, url: str) -> None:
        """Raise SecurityPolicyViolation if URL is not permitted with redacted error text (INV-28)."""
        if not self.is_url_allowed(url):
            # Strip userinfo (credentials), query string, and sensitive tokens (INV-28)
            try:
                split_url = urllib.parse.urlsplit(url.strip())
                raw_h = (split_url.hostname or "").rstrip(".")
                host_str = f"[{raw_h}]" if ":" in raw_h else raw_h
                sanitized_netloc = host_str
                if split_url.port:
                    sanitized_netloc += f":{split_url.port}"
                # Construct clean URL via standard urlunsplit without accessing private namedtuple methods (INV-03)
                sanitized_dest = urllib.parse.urlunsplit(
                    (split_url.scheme, sanitized_netloc, split_url.path, "", "")
                )
            except Exception:
                sanitized_dest = "[unparseable_destination]"
            clean_dest = PIIRedactor.redact_text(sanitized_dest)

            # Do not dump internal network topologies into error messages to prevent leakage (INV-28)
            raise SecurityPolicyViolation(
                f"Navigation to unauthorized destination '{clean_dest}' blocked by security allowlist policy."
            )
