/**
 * In-browser Event Recorder & Visual Dock Overlay for Interface.ai HITL.
 * Injected into the active page when human intervention is triggered.
 *
 * Invariants:
 * 1. Zero plaintext PII egress: sensitive inputs (password, pin, ssn) masked in-memory.
 * 2. Non-destructive visual dock overlay attached to document root.
 * 3. Dispatches structured events to window.__interface_ai_record_event__.
 */
(() => {
    window.__interface_ai_recorder_active = true;
    if (window.__interface_ai_recorder_installed) {
        if (!document.getElementById('__interface_ai_hitl_dock__')) {
            // Re-mount dock if DOM reloaded
            _mountDock();
        }
        return;
    }
    window.__interface_ai_recorder_installed = true;

    function _mountDock() {
        if (document.getElementById('__interface_ai_hitl_dock__')) return;
        const dock = document.createElement('div');
        dock.id = '__interface_ai_hitl_dock__';
        dock.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 2147483647;
            background: linear-gradient(90deg, #b91c1c, #991b1b);
            color: #ffffff;
            padding: 10px 20px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 13px;
            font-weight: 600;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            border-bottom: 2px solid #f87171;
        `;

        dock.innerHTML = `
            <div style="display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 18px;">⏸️</span>
                <span>AUTOMATION PAUSED &mdash; HUMAN OPERATOR IN CONTROL</span>
                <span style="background: rgba(255,255,255,0.2); padding: 2px 8px; border-radius: 4px; font-size: 11px; font-family: monospace;">DUAL-STREAM RECORDING ACTIVE</span>
            </div>
            <button id="__interface_ai_resume_btn__" style="
                background: #ffffff;
                color: #991b1b;
                border: none;
                padding: 6px 16px;
                font-size: 12px;
                font-weight: bold;
                border-radius: 4px;
                cursor: pointer;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
                transition: transform 0.1s ease;
            ">Resume Automation &rarr;</button>
        `;

        document.documentElement.appendChild(dock);

        const resumeBtn = document.getElementById('__interface_ai_resume_btn__');
        if (resumeBtn) {
            resumeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (window.__interface_ai_resume_signal__) {
                    window.__interface_ai_resume_signal__();
                }
            });
        }
    }

    _mountDock();

    // 2. Sensitive Field Detector for In-Browser PII Pre-Masking
    function isSensitiveInput(target) {
        if (!target) return false;
        const type = (target.getAttribute('type') || '').toLowerCase();
        if (type === 'password') return true;
        const name = (target.getAttribute('name') || '').toLowerCase();
        const id = (target.getAttribute('id') || '').toLowerCase();
        const label = (target.getAttribute('aria-label') || '').toLowerCase();
        const sensitiveTokens = ['ssn', 'pass', 'pin', 'cvv', 'card', 'secret', 'token', 'auth'];
        return sensitiveTokens.some(token => name.includes(token) || id.includes(token) || label.includes(token));
    }

    // 3. Compute CSS Selector Path
    function getSelector(el) {
        if (!(el instanceof Element)) return '';
        if (el.id) return `#${el.id}`;
        let path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            let selector = el.nodeName.toLowerCase();
            if (el.className && typeof el.className === 'string') {
                const classes = el.className.trim().split(/\s+/).filter(c => !c.startsWith('__interface_ai'));
                if (classes.length) selector += '.' + classes.join('.');
            }
            path.unshift(selector);
            el = el.parentElement;
            if (path.length > 5) break;
        }
        return path.join(' > ');
    }

    // 4. Capture Click Events (Only when recorder active - INV-32)
    document.addEventListener('click', (e) => {
        if (!window.__interface_ai_recorder_active) return;
        if (e.target && e.target.closest('#__interface_ai_hitl_dock__')) return;
        const target = e.target;
        const eventData = {
            type: 'click',
            timestamp: Date.now(),
            target_tag: target.tagName ? target.tagName.toLowerCase() : '',
            target_id: target.id || null,
            target_text: (target.innerText || target.value || '').trim().substring(0, 100),
            selector: getSelector(target),
            coordinates: { x: e.clientX, y: e.clientY }
        };
        if (window.__interface_ai_record_event__) {
            window.__interface_ai_record_event__(JSON.stringify(eventData));
        }
    }, true);

    // 5. Capture Input & Change Events with Pre-Masking (Only when recorder active - INV-32)
    document.addEventListener('change', (e) => {
        if (!window.__interface_ai_recorder_active) return;
        if (e.target && e.target.closest('#__interface_ai_hitl_dock__')) return;
        const target = e.target;
        const sensitive = isSensitiveInput(target);
        const rawVal = target.value || '';
        const safeVal = sensitive ? '[REDACTED_SENSITIVE_INPUT]' : rawVal;

        const eventData = {
            type: 'change',
            timestamp: Date.now(),
            target_tag: target.tagName ? target.tagName.toLowerCase() : '',
            target_id: target.id || null,
            target_name: target.getAttribute('name') || null,
            value: safeVal,
            is_sensitive: sensitive,
            selector: getSelector(target)
        };
        if (window.__interface_ai_record_event__) {
            window.__interface_ai_record_event__(JSON.stringify(eventData));
        }
    }, true);
})();
