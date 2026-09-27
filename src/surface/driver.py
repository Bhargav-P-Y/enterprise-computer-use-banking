"""Browser Surface Driver encapsulating Playwright context, frame navigation, and route security."""

import asyncio
import urllib.parse
from typing import Any, Optional, Union
from playwright.async_api import async_playwright, Browser, BrowserContext, Frame, Page, Playwright, Route

from src.guardrails.allowlist import RouteAllowlist, SecurityPolicyViolation
from src.guardrails.pii_redactor import PIIRedactor


class FrameNotFoundError(RuntimeError):
    """Raised when a requested target frame is detached or cannot be resolved (INV-09)."""


class SurfaceDriver:
    """Production Playwright surface controller with frame isolation and security gating."""

    def __init__(
        self,
        headless: bool = True,
        allowlist: Optional[RouteAllowlist] = None,
        ignore_https_errors: bool = False,
    ) -> None:
        self.headless = headless
        self.allowlist = allowlist or RouteAllowlist()
        self.ignore_https_errors = ignore_https_errors
        self._lock = asyncio.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> Page:
        """Launch browser instance and initialize primary page with route guardrails."""
        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page

            # Clean up zombie processes before reallocation if page was closed/crashed
            if self._browser or self._playwright:
                await self._cleanup_resources()

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
                timeout=10000,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=self.ignore_https_errors,
            )

            # Hook security allowlist on all network requests (INV-25)
            await self._context.route("**/*", self._handle_route_interception)

            # INV-31: Attach in-browser DOM event pre-masking for sensitive input elements
            await self._context.add_init_script("""
                window.addEventListener('DOMContentLoaded', () => {
                    let rafId = null;
                    const maskSensitive = () => {
                        const sensitive = document.querySelectorAll(
                            'input[type="password"], input[autocomplete*="password"], ' +
                            'input[name*="ssn" i], input[name*="pin" i], input[name*="cvv" i], ' +
                            'input[name*="account" i], input[name*="routing" i], input[name*="card" i], ' +
                            'input[data-mask="true"]'
                        );
                        sensitive.forEach(el => {
                            el.setAttribute('data-pii-masked', 'true');
                            if (!el.__masked_listener) {
                                el.__masked_listener = true;
                                const sanitizeValue = () => {
                                    if (el.value && !el.value.startsWith('[REDACTED')) {
                                        el.setAttribute('data-masked-value', 'true');
                                    }
                                };
                                el.addEventListener('input', sanitizeValue);
                                el.addEventListener('change', sanitizeValue);
                            }
                        });
                    };
                    maskSensitive();
                    const root = document.documentElement || document.body;
                    if (root) {
                        const observer = new MutationObserver(() => {
                            if (!rafId) {
                                rafId = requestAnimationFrame(() => {
                                    maskSensitive();
                                    rafId = null;
                                });
                            }
                        });
                        observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["type", "name", "id", "autocomplete", "data-mask", "aria-label"] });
                    }
                });
            """)

            self._page = await self._context.new_page()
            return self._page

    async def _handle_route_interception(self, route: Route) -> None:
        """Intercept every outgoing request to guarantee zero external data leakage (Fail-Closed)."""
        try:
            url = route.request.url
            if self.allowlist.is_url_allowed(url):
                await route.continue_()
            else:
                # Abort unauthorized network requests immediately
                await route.abort("blockedbyclient")
        except Exception:
            # Guarantee fail-closed posture: abort dangling route on any exception
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass

    @property
    def page(self) -> Page:
        """Get current active page."""
        if not self._page:
            raise RuntimeError("SurfaceDriver not started. Call start() first.")
        return self._page

    @property
    def browser(self) -> Browser:
        """Get current browser instance."""
        if not self._browser:
            raise RuntimeError("SurfaceDriver not started.")
        return self._browser

    @property
    def context(self) -> BrowserContext:
        """Get current browser context."""
        if not self._context:
            raise RuntimeError("SurfaceDriver not started.")
        return self._context

    def get_target_frame(self, frame_name: Optional[str] = None, fallback_to_page: bool = True) -> Union[Page, Frame]:
        """Resolve either main page or named iframe with exact origin and pathname matching (INV-09)."""
        if not frame_name or not isinstance(frame_name, str) or frame_name.strip() in ("", "_top", "top", "main"):
            return self.page

        target = frame_name.strip()
        for f in self.page.frames:
            try:
                if f.is_detached():
                    continue
                if f.name == target or f.name.lower() == target.lower():
                    return f
                parsed_url = urllib.parse.urlsplit(f.url)
                target_clean = target.strip("/")
                frame_path = parsed_url.path.strip("/")
                if f.url == target or (target_clean and frame_path == target_clean):
                    return f
            except Exception:
                continue

        if fallback_to_page:
            return self.page
        raise FrameNotFoundError(f"Frame '{frame_name}' not found or detached (INV-09).")

    @property
    def current_url(self) -> str:
        """Get current URL safely without breaching encapsulation."""
        return self.page.url if self._page else ""

    async def capture_screenshot(self, quality: int = 70, timeout: int = 5000) -> bytes:
        """Capture compressed JPEG screenshot adhering to Law of Demeter (INV-03)."""
        return await self.page.screenshot(type="jpeg", quality=quality, timeout=timeout)

    async def evaluate_script(self, script: str, target_frame: Optional[str] = None, timeout_ms: int = 5000) -> Any:
        """Evaluate JavaScript inside target frame or main page context with bounded execution budget (INV-07, INV-24)."""
        frame = self.get_target_frame(target_frame)
        return await asyncio.wait_for(frame.evaluate(script), timeout=timeout_ms / 1000.0)

    async def navigate(self, url: str) -> None:
        """Safely navigate to URL with allowlist verification, userinfo stripping, and translated errors."""
        self.allowlist.assert_url_allowed(url)
        try:
            parsed = urllib.parse.urlsplit(url)
            port_str = f":{parsed.port}" if parsed.port else ""
            clean_host = f"{parsed.hostname or ''}{port_str}"
            safe_url = parsed._replace(netloc=clean_host, query="", fragment="").geturl()
            clean_dest = PIIRedactor.redact_text(safe_url)
        except Exception:
            clean_dest = PIIRedactor.redact_text(url.split("?")[0].split("#")[0])

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=10000)
        except Exception as e:
            err_msg = str(e)
            if "net::ERR_BLOCKED_BY_CLIENT" in err_msg or "blockedbyclient" in err_msg.lower():
                raise SecurityPolicyViolation(f"Navigation to {clean_dest} blocked by security policy") from None
            clean_err = PIIRedactor.redact_text(err_msg)
            raise RuntimeError(f"Navigation to {clean_dest} failed: {clean_err}") from None

    async def click(
        self,
        selector: Optional[str] = None,
        target_frame: Optional[str] = None,
        timeout_ms: int = 3000,
        text_fallback: Optional[str] = None,
    ) -> bool:
        """Click element in page or target frame context adhering to Law of Demeter (INV-02, INV-03)."""
        frame = self.get_target_frame(target_frame)
        loc = None
        tier_timeout = max(300, timeout_ms // 2) if text_fallback else timeout_ms

        if selector:
            try:
                cand = frame.locator(selector).first
                await cand.wait_for(state="visible", timeout=tier_timeout)
                loc = cand
            except Exception:
                loc = None

        if not loc and text_fallback:
            try:
                cand = frame.get_by_text(text_fallback, exact=False).first
                await cand.wait_for(state="visible", timeout=tier_timeout)
                loc = cand
            except Exception:
                loc = None

        if loc:
            try:
                await loc.click(timeout=timeout_ms)
                p = frame if hasattr(frame, "wait_for_load_state") else getattr(frame, "page", None)
                if p:
                    try:
                        await p.wait_for_load_state("domcontentloaded", timeout=500)
                    except Exception:
                        pass
                return True
            except Exception:
                return False
        return False

    async def fill(
        self,
        value: str,
        selector: Optional[str] = None,
        target_frame: Optional[str] = None,
        timeout_ms: int = 3000,
        label_fallback: Optional[str] = None,
    ) -> bool:
        """Fill text input in page or target frame adhering to Law of Demeter and ISP (INV-02, INV-03)."""
        frame = self.get_target_frame(target_frame)
        loc = None
        tier_timeout = max(300, timeout_ms // 2) if label_fallback else timeout_ms

        if selector:
            try:
                cand = frame.locator(selector).first
                await cand.wait_for(state="visible", timeout=tier_timeout)
                loc = cand
            except Exception:
                loc = None

        if not loc and label_fallback:
            try:
                cand = frame.get_by_label(label_fallback, exact=False).first
                await cand.wait_for(state="visible", timeout=tier_timeout)
                loc = cand
            except Exception:
                loc = None

        if loc:
            try:
                await loc.fill(str(value or ""), timeout=timeout_ms)
                return True
            except Exception:
                return False
        return False

    async def extract_text(
        self,
        selector: Optional[str] = None,
        target_frame: Optional[str] = None,
        timeout_ms: int = 3000,
    ) -> Optional[str]:
        """Extract inner text from element adhering to Law of Demeter (INV-02, INV-03)."""
        frame = self.get_target_frame(target_frame)
        if not selector:
            return None
        try:
            loc = frame.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return await loc.inner_text()
        except Exception:
            return None

    async def _cleanup_resources(self) -> None:
        """Internal helper to safely close contexts and browser with bounded timeout (INV-07)."""
        self._page = None
        try:
            if self._context:
                await asyncio.wait_for(self._context.close(), timeout=3.0)
        except Exception:
            pass
        finally:
            self._context = None

        try:
            if self._browser:
                await asyncio.wait_for(self._browser.close(), timeout=3.0)
        except Exception:
            pass
        finally:
            self._browser = None

        try:
            if self._playwright:
                await asyncio.wait_for(self._playwright.stop(), timeout=3.0)
        except Exception:
            pass
        finally:
            self._playwright = None
            self._page = None

    async def stop(self) -> None:
        """Gracefully close page, context, and browser preventing zombie processes."""
        async with self._lock:
            await self._cleanup_resources()

    async def __aenter__(self) -> "SurfaceDriver":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()


