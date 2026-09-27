"""Multimodal Gemini 3.8 Client for Discovery Agent.

Interacts with Google Gemini 3.8 (via direct REST with key pool rotation from .env)
to provide fast, reliable observe-decide-act multimodal computer use capabilities.
"""

import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

from src.guardrails.pii_redactor import PIIRedactor

load_dotenv(Path(__file__).parent.parent.parent / ".env")

GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PRIMARY_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-3.7-flash"
TERTIARY_MODEL = "gemini-3.6-flash"


def _extract_retry_delay(resp: requests.Response) -> Optional[float]:
    """Extract server-recommended retry delay in seconds from 429 response."""
    try:
        data = resp.json()
        error = data.get("error", {})
        for item in error.get("details", []):
            if "retryDelay" in item:
                delay_str = str(item["retryDelay"]).rstrip("s")
                return float(delay_str)
        msg = error.get("message", "")
        if "Please retry in " in msg:
            part = msg.split("Please retry in ")[1].split("s")[0].strip()
            return float(part)
    except Exception:
        pass
    return None


class DiscoveryGeminiClient:
    """Multimodal client calling Gemini 3.8 REST endpoint with key pool rotation and bounded concurrency (INV-07, INV-14)."""

    def __init__(self) -> None:
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.keys: List[str] = []
        all_found = []
        for i in range(1, 21):
            k = os.getenv(f"GEMINI_API_KEY_{i}")
            if k:
                all_found.append((i, k))

        all_found.sort(key=lambda x: x[0])
        self.keys = [k for _, k in all_found]

        if not self.keys:
            fallback = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if fallback:
                self.keys.append(fallback)

        if not self.keys:
            raise ValueError("No Gemini API keys found in .env.")

        self.current_key_idx = 0

    def close(self) -> None:
        """Close underlying HTTP session and release connection pool resources."""
        self.session.close()

    def __enter__(self) -> "DiscoveryGeminiClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_key(self) -> str:
        return self.keys[self.current_key_idx]

    def _rotate_key(self) -> str:
        if len(self.keys) > 1:
            self.current_key_idx = (self.current_key_idx + 1) % len(self.keys)
        return self._get_key()

    def generate_decision(
        self,
        system_instruction: str,
        user_prompt: str,
        screenshot_png_bytes: Optional[bytes] = None,
        timeout_budget_sec: float = 120.0,
    ) -> Dict[str, Any]:
        """Request next action decision from Gemini 3.8 using visual and text context with bounded budget (INV-07, INV-13, INV-14, INV-27)."""
        t0 = time.monotonic()

        # Sanitize prompt before outbound transmission (INV-27)
        clean_user_prompt = PIIRedactor.redact_text(user_prompt)
        parts: List[Dict[str, Any]] = [{"text": clean_user_prompt}]

        if screenshot_png_bytes and len(screenshot_png_bytes) >= 8:
            mime = "image/png" if screenshot_png_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            b64_img = base64.b64encode(screenshot_png_bytes).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": mime,
                    "data": b64_img,
                }
            })

        payload = {
            "contents": [{"parts": parts}],
            "systemInstruction": {"parts": [{"text": PIIRedactor.redact_text(system_instruction)}]},
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }

        models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL, TERTIARY_MODEL]
        max_rotation_rounds = 5
        rejected_keys = set()

        for round_num in range(max_rotation_rounds):
            last_retry_delay: Optional[float] = None

            for _ in range(len(self.keys)):
                elapsed = time.monotonic() - t0
                if elapsed >= timeout_budget_sec:
                    raise TimeoutError(f"Cumulative Gemini discovery inference exceeded budget of {timeout_budget_sec}s (INV-07).")

                key = self._get_key()
                if key in rejected_keys:
                    self._rotate_key()
                    continue

                for model in models_to_try:
                    remaining = timeout_budget_sec - (time.monotonic() - t0)
                    if remaining <= 0:
                        raise TimeoutError(f"Cumulative Gemini discovery inference exceeded budget of {timeout_budget_sec}s (INV-07).")
                    call_timeout = min(30.0, remaining)

                    url = GEMINI_REST_URL.format(model=model)
                    headers = {
                        "Content-Type": "application/json",
                        "x-goog-api-key": key,
                    }
                    try:
                        resp = self.session.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=call_timeout,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            candidates = data.get("candidates", [])
                            if not candidates:
                                continue
                            candidate = candidates[0]
                            finish_reason = candidate.get("finishReason")
                            if finish_reason == "SAFETY":
                                raise RuntimeError("Gemini content blocked by safety policy")
                            elif finish_reason == "MAX_TOKENS":
                                raise RuntimeError("Gemini output truncated: maximum output tokens exceeded")

                            parts_out = candidate.get("content", {}).get("parts", [])
                            if not parts_out:
                                continue
                            raw_text = parts_out[0].get("text", "").strip()

                            # Resilient fenced & bracket-aware JSON extraction
                            clean_text = raw_text.strip()
                            if clean_text.startswith("```json"):
                                clean_text = clean_text[7:]
                            elif clean_text.startswith("```"):
                                clean_text = clean_text[3:]
                            if clean_text.endswith("```"):
                                clean_text = clean_text[:-3]
                            clean_text = clean_text.strip()

                            match = re.search(r"(\{.*\})", clean_text, re.DOTALL)
                            clean_json = match.group(1) if match else clean_text
                            parsed = json.loads(clean_json)
                            parsed["_model_used"] = model
                            self._rotate_key()
                            return parsed

                        elif resp.status_code in (401, 403):
                            # Evict key from active rotation rather than crashing the whole pool (INV-14)
                            rejected_keys.add(key)
                            if len(rejected_keys) >= len(self.keys):
                                raise PermissionError(f"All {len(self.keys)} Gemini API keys rejected with authorization failure (HTTP {resp.status_code}).")
                            break

                        elif resp.status_code == 429:
                            delay = _extract_retry_delay(resp)
                            if delay and (last_retry_delay is None or delay > last_retry_delay):
                                last_retry_delay = delay
                            # Rate limit applies to the key/project, so advance to next key immediately
                            break

                        elif 400 <= resp.status_code < 500 and resp.status_code not in (401, 403, 429):
                            sanitized_err = PIIRedactor.redact_text(resp.text[:200])
                            raise ValueError(f"Fatal Gemini client error HTTP {resp.status_code}: {sanitized_err}")

                        elif resp.status_code in (500, 502, 503, 504):
                            # Full-jitter exponential backoff on cluster load shedding and gateway errors (INV-14)
                            backoff = min(6.0, 0.5 * (2 ** round_num)) + random.uniform(0.1, 0.4)
                            cur_remaining = max(0.0, timeout_budget_sec - (time.monotonic() - t0))
                            time.sleep(min(backoff, cur_remaining))
                            continue

                    except (RuntimeError, PermissionError, ValueError):
                        # Re-raise safety, truncation, auth, and fatal client errors immediately (fail-closed INV-26, INV-28)
                        raise
                    except (requests.RequestException, json.JSONDecodeError):
                        continue

                self._rotate_key()

            # All keys in pool returned 429; wait for the short rate limit window to reset with budget clamp (INV-07, INV-14)
            if round_num < max_rotation_rounds - 1:
                wait_time = int(last_retry_delay + 2) if last_retry_delay else min(45, int(10 * (1.5 ** round_num)))
                cur_remaining = max(0.0, timeout_budget_sec - (time.monotonic() - t0))
                time.sleep(min(wait_time, cur_remaining))

        raise RuntimeError("Gemini API key pool exhausted after retry backoff. Ensure API key quotas are available.")
