import re
import time
from typing import Optional, Tuple, Union
from playwright.async_api import Frame, Locator, Page

from src.schemas.artifact import LocatorCascade, LocatorStrategyType, LocatorTier
from src.guardrails.pii_redactor import PIIRedactor

STRATEGY_PRIORITY = {
    LocatorStrategyType.ROLE_AND_NAME: 1,
    LocatorStrategyType.LABEL_PROXIMITY: 2,
    LocatorStrategyType.STRUCTURAL_XPATH: 3,
    LocatorStrategyType.COORDINATE_RATIO: 4,
}


def _xpath_literal(s: str) -> str:
    """Safely format an XPath string literal, handling single and double quotes cleanly."""
    s = s.replace("\u00a0", " ")
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    args = []
    for i, part in enumerate(parts):
        if part:
            args.append(f"'{part}'")
        if i < len(parts) - 1:
            args.append('"\'"')
    if len(args) <= 1:
        args.append("''")
    return f"concat({', '.join(args)})"


class LocatorResolutionError(Exception):
    """Raised when critical surface failure occurs during cascade evaluation (INV-09)."""


class LocatorEngine:
    """High-speed cascade resolver with microsecond fast-paths and spatial fallbacks."""

    @classmethod
    async def resolve(
        cls,
        target: Union[Page, Frame],
        cascade: LocatorCascade,
        timeout_ms: int = 3000,
    ) -> Tuple[Optional[Locator], Optional[LocatorTier], int]:
        """
        Evaluate locator tiers sequentially in strict order of preference (INV-10).
        Returns: (matched_locator, tier_used, duration_ms)
        """
        # Validate frame detachment and page closure status (INV-08, INV-09)
        if hasattr(target, "is_closed") and target.is_closed():
            raise LocatorResolutionError("Target page has been closed")
        if hasattr(target, "is_detached") and target.is_detached():
            raise LocatorResolutionError("Target frame has been detached from the DOM")

        t0 = time.perf_counter()

        # Enforce strict Tier 1 -> Tier 2 -> Tier 3 -> Tier 4 ordering
        sorted_tiers = sorted(
            cascade.tiers,
            key=lambda t: STRATEGY_PRIORITY.get(t.strategy, 99),
        )

        for i, tier in enumerate(sorted_tiers):
            elapsed_so_far = int((time.perf_counter() - t0) * 1000)
            remaining_ms = max(0, timeout_ms - elapsed_so_far)
            if remaining_ms <= 0:
                break

            try:
                locator = cls._evaluate_tier(target, tier)
                if locator is not None:
                    # Dynamically allocate budget among remaining tiers to allow dynamic controls to hydrate (INV-08, INV-24)
                    remaining_tiers = max(1, len(sorted_tiers) - i)
                    tier_budget = max(100, remaining_ms // remaining_tiers)
                    is_last_tier = (i == len(sorted_tiers) - 1) or all(t.strategy == LocatorStrategyType.COORDINATE_RATIO for t in sorted_tiers[i+1:])
                    probe_timeout = max(1, remaining_ms) if is_last_tier else max(1, min(remaining_ms, min(500, tier_budget)))

                    # Fast-path check: if first candidate is already visible, return immediately with zero delay (INV-24)
                    first = locator.first
                    try:
                        if await first.is_visible():
                            total_elapsed = int((time.perf_counter() - t0) * 1000)
                            return first, tier, total_elapsed
                    except Exception:
                        pass

                    # Bounded auto-waiting for dynamic elements hydrating or mounting (INV-08, INV-24)
                    await first.wait_for(state="visible", timeout=probe_timeout)
                    total_elapsed = int((time.perf_counter() - t0) * 1000)
                    return first, tier, total_elapsed
            except Exception as e:
                err_msg = str(e).lower()
                # Anchored target/frame closure check avoids false positives on selector names containing words like 'closed'
                is_target_dead = (
                    (hasattr(target, "is_closed") and target.is_closed()) or
                    (hasattr(target, "is_detached") and target.is_detached()) or
                    bool(re.search(r"\b(?:target|page|context|frame|browser)\b.*\b(?:closed|detached|destroyed|crashed)\b", err_msg))
                )
                if is_target_dead:
                    clean_err = PIIRedactor.redact_text(str(e))
                    raise LocatorResolutionError(f"Target frame or page was detached/closed: {clean_err}")
                # Fall through to next tier in cascade
                continue

        # If DOM locators fail, check if cascade contains a coordinate ratio fallback (INV-10)
        coord_tier = next((t for t in sorted_tiers if t.strategy == LocatorStrategyType.COORDINATE_RATIO), None)
        total_elapsed = int((time.perf_counter() - t0) * 1000)
        return None, coord_tier, total_elapsed

    @classmethod
    def _evaluate_tier(
        cls, target: Union[Page, Frame], tier: LocatorTier
    ) -> Optional[Locator]:
        """Evaluate a single tier strategy against the target frame/page (synchronous, zero overhead)."""
        strategy = tier.strategy

        if strategy == LocatorStrategyType.ROLE_AND_NAME:
            try:
                # Normalize role to lowercase to prevent casing mismatches across compilers (INV-08, INV-10)
                role_str = tier.role.lower().strip() if tier.role else None
                if role_str and tier.name:
                    return target.get_by_role(role_str, name=tier.name, exact=False)
                elif role_str:
                    return target.get_by_role(role_str)
                elif tier.name:
                    return target.get_by_text(tier.name, exact=False)
            except (ValueError, Exception):
                if tier.name:
                    return target.get_by_text(tier.name, exact=False)
                return None

        elif strategy == LocatorStrategyType.LABEL_PROXIMITY:
            clean_label = tier.label_text.replace("\u00a0", " ").rstrip(":").strip() if tier.label_text else ""
            if not clean_label:
                return None
            lit = _xpath_literal(clean_label)
            # Prioritize dedicated labels, filter disabled/hidden controls, and search proximal inputs (INV-06, INV-08, INV-10)
            xpath_expr = (
                f"//label[contains(normalize-space(.), {lit})]//input[not(@type='hidden') and not(@disabled) and not(@aria-hidden='true')] | "
                f"//label[contains(normalize-space(.), {lit})]/following::input[not(@type='hidden') and not(@disabled) and not(@aria-hidden='true')][1] | "
                f"//label[contains(normalize-space(.), {lit})]/following::select[not(@disabled) and not(@aria-hidden='true')][1] | "
                f"//label[contains(normalize-space(.), {lit})]/following::textarea[not(@disabled) and not(@aria-hidden='true')][1] | "
                f"//button[contains(normalize-space(.), {lit}) and not(@disabled) and not(@aria-hidden='true')] | "
                f"//input[(@type='submit' or @type='button') and contains(@value, {lit}) and not(@disabled)] | "
                f"//*[not(*) and contains(normalize-space(.), {lit})]/following::input[not(@type='hidden') and not(@disabled)][1] | "
                f"//*[not(*) and contains(normalize-space(.), {lit})]/following::select[not(@disabled)][1] | "
                f"//*[not(*) and contains(normalize-space(.), {lit})]/following::textarea[not(@disabled)][1] | "
                f"//*[not(*) and contains(normalize-space(.), {lit})]/ancestor::tr//input[not(@type='hidden') and not(@disabled)][1] | "
                f"//*[not(*) and contains(normalize-space(.), {lit})]/ancestor::*[@role='row']//input[not(@type='hidden') and not(@disabled)][1]"
            )
            return target.locator(f"xpath={xpath_expr}")

        elif strategy == LocatorStrategyType.STRUCTURAL_XPATH:
            if not tier.xpath or not tier.xpath.strip():
                return None
            xpath_raw = tier.xpath.strip()
            # Deduplicate 'xpath=' prefix to prevent malformed selector errors (INV-11)
            xpath_arg = xpath_raw if xpath_raw.startswith("xpath=") else f"xpath={xpath_raw}"
            return target.locator(xpath_arg)

        elif strategy == LocatorStrategyType.COORDINATE_RATIO:
            # Coordinates are handled at action execution time if no locator matches
            return None

        return None
