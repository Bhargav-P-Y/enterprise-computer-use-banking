"""Set-of-Marks (SoM) visual annotator for grounding multimodal LLM decisions."""

import asyncio
import logging
from typing import Any, Dict, List, Union
from playwright.async_api import Frame, Page

logger = logging.getLogger(__name__)

SOM_INJECTION_JS = """
(() => {
    // 1. Remove existing SoM container and reset stale data-som-id attributes (Clean-slate re-entrancy)
    const existing = document.getElementById('__som_overlay_container__');
    if (existing) existing.remove();
    document.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));

    const parent = document.documentElement || document.body;
    if (!parent) return [];

    const container = document.createElement('div');
    container.id = '__som_overlay_container__';
    container.style.position = 'fixed';
    container.style.top = '0';
    container.style.left = '0';
    container.style.width = '100vw';
    container.style.height = '100vh';
    container.style.pointerEvents = 'none';
    container.style.zIndex = '9999999';
    container.style.setProperty('transform', 'none', 'important');
    container.style.setProperty('margin', '0', 'important');

    // In-browser client-side regex scrubber for SSNs, PANs, banking accounts, emails, and DOBs (INV-27, INV-31)
    const scrubPII = (txt) => {
        if (!txt) return '';
        return String(txt)
            .replace(/\\b\\d{3}-\\d{2}-\\d{4}\\b/g, '[REDACTED_SSN]')
            .replace(/\\b(?:\\d[ -]?){13,19}\\b/g, '[REDACTED_PAN]')
            .replace(/\\b\\d{8,12}\\b/g, '[REDACTED_ACCOUNT]')
            .replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/g, '[REDACTED_EMAIL]')
            .replace(/\\b(?:0[1-9]|1[0-2])[\\/-](?:0[1-9]|[12]\\d|3[01])[\\/-](?:19|20)\\d{2}\\b/g, '[REDACTED_DOB]');
    };

    const rawCandidates = Array.from(document.querySelectorAll(
        'button:not(:disabled), a[href], input:not([type="hidden"]):not(:disabled), select:not(:disabled), textarea:not(:disabled), [role="button"]:not([aria-disabled="true"]), [role="link"], [onclick]'
    ));

    // Pre-filter visible, on-screen elements including computed visibility (INV-06)
    const visibleCandidates = [];
    for (const el of rawCandidates) {
        if (visibleCandidates.length >= 100) break;
        const rect = el.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5) continue;
        if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
        
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) continue;
        visibleCandidates.push(el);
    }

    const fragment = document.createDocumentFragment();
    const marks = [];
    let idx = 1;

    visibleCandidates.forEach((el) => {
        const rect = el.getBoundingClientRect();

        const badge = document.createElement('div');
        badge.innerText = idx.toString();
        badge.style.position = 'absolute';
        badge.style.left = Math.round(rect.left) + 'px';
        badge.style.top = Math.round(rect.top) + 'px';
        badge.style.background = '#dc2626';
        badge.style.color = '#ffffff';
        badge.style.fontSize = '10px';
        badge.style.fontWeight = 'bold';
        badge.style.padding = '1px 4px';
        badge.style.borderRadius = '3px';
        badge.style.boxShadow = '0 1px 3px rgba(0,0,0,0.5)';
        badge.style.border = '1px solid #ffffff';
        badge.style.pointerEvents = 'none';
        fragment.appendChild(badge);

        // Highlight element border
        el.setAttribute('data-som-id', idx.toString());

        // Extract label/context text with password and PII redaction (INV-27, INV-31)
        let labelText = '';
        const secretAttr = [el.name || '', el.id || '', el.placeholder || '', el.getAttribute('aria-label') || '', el.getAttribute('autocomplete') || ''].join(' ');
        const isSecret = el.type === 'password' || /password|pin|cvv|cvc|ssn|secret|token/i.test(secretAttr);
        if (isSecret) {
            labelText = '[REDACTED_PASSWORD]';
        } else {
            labelText = el.innerText || el.value || el.getAttribute('aria-label') || el.name || '';
            if (!labelText && el.closest('tr')) {
                labelText = el.closest('tr').innerText.replace(/\\s+/g, ' ').trim();
            }
            labelText = scrubPII(labelText.substring(0, 150));
        }

        marks.push({
            id: idx,
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || el.type || el.tagName.toLowerCase(),
            text: labelText.substring(0, 60),
            bbox: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)]
        });

        idx++;
    });

    container.appendChild(fragment);
    parent.appendChild(container);

    return marks;
})()
"""

SOM_REMOVAL_JS = """
(() => {
    const container = document.getElementById('__som_overlay_container__');
    if (container) {
        container.remove();
    }
    document.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));
})()
"""


class SoMAnnotator:
    """Injects and clears Set-of-Marks visual markers for VLM grounding."""

    @classmethod
    async def inject_marks(cls, target: Union[Page, Frame]) -> List[Dict[str, Any]]:
        """Inject numbered bounding badges onto interactive controls with bounded timeout."""
        try:
            marks = await asyncio.wait_for(target.evaluate(SOM_INJECTION_JS), timeout=2.0)
            return marks or []
        except Exception as e:
            logger.debug("SoM injection failed or timed out: %s", e)
            return []

    @classmethod
    async def clear_marks(cls, target: Union[Page, Frame]) -> None:
        """Remove Set-of-Marks visual overlay."""
        try:
            await asyncio.wait_for(target.evaluate(SOM_REMOVAL_JS), timeout=1.5)
        except Exception as e:
            logger.debug("SoM removal failed or timed out: %s", e)


