"""Transient glitch and recoverable condition handler for deterministic replay."""

import asyncio
from typing import Union
from playwright.async_api import Frame, Page


class TransientRecoverySentinel:
    """Detects and resolves temporary UI blockers (loading spinners, interstitial notices)."""

    @classmethod
    async def handle_transient_states(
        cls, target: Union[Page, Frame], spinner_timeout_ms: int = 1200
    ) -> bool:
        """
        Check for and resolve recoverable transient states.
        Returns True if a transient state was resolved, False otherwise.
        """
        if (hasattr(target, "is_detached") and target.is_detached()) or (
            hasattr(target, "is_closed") and target.is_closed()
        ):
            return False

        resolved = False
        t_start = asyncio.get_event_loop().time()
        deadline = t_start + 1.2
        # Clamp spinner timeout to bounded range [100ms, 1200ms] (INV-07, INV-24)
        bounded_timeout = max(100, min(int(spinner_timeout_ms), 1200))

        # 1. Check for visible loading indicators
        try:
            spinner = target.locator("#loading_indicator:visible, .loading-spinner:visible, [data-spinner]:visible")
            if await spinner.count() > 0 and await spinner.first.is_visible():
                await spinner.first.wait_for(state="hidden", timeout=bounded_timeout)
                resolved = True
        except Exception:
            pass

        # 2. Check for unexpected dismissable notification banners or modal dialogs with remaining budget (INV-24)
        remaining_sec = max(0.1, deadline - asyncio.get_event_loop().time())
        dismiss_timeout = min(800, int(remaining_sec * 1000))
        try:
            dismiss_btn = target.locator(
                "#btn_error_dismiss:visible, "
                "[role='dialog']:not([data-risk]):not([data-outcome]) button:has-text('OK'):visible, "
                "[role='dialog']:not([data-risk]):not([data-outcome]) button:has-text('Dismiss'):visible, "
                "[role='dialog']:not([data-risk]):not([data-outcome]) button:has-text('Acknowledge'):visible, "
                "[role='dialog']:not([data-risk]):not([data-outcome]) button:has-text('Close'):visible, "
                ".modal:not([data-risk]):not([data-outcome]) button:has-text('OK'):visible, "
                ".modal:not([data-risk]):not([data-outcome]) button:has-text('Dismiss'):visible, "
                ".alert button:has-text('Dismiss'):visible, .banner button:has-text('Dismiss'):visible"
            )
            if await dismiss_btn.count() > 0 and await dismiss_btn.first.is_visible():
                await dismiss_btn.first.click(timeout=dismiss_timeout)
                await asyncio.sleep(0.05)
                resolved = True
        except Exception:
            pass

        return resolved

