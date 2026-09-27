"""Structured Prompt Formulas for Multimodal Discovery Agent."""

from src.guardrails.pii_redactor import PIIRedactor

DISCOVERY_SYSTEM_INSTRUCTION = """You are an Expert Multimodal Automation Engineer operating a legacy US back-office core banking system.
Your mission is to explore the interface, discover the optimal path to satisfy a natural-language business goal, and execute actions on the live web surface.

[ENVIRONMENT CHARACTERISTICS]
1. Legacy Web Surface: Expect nested layout tables, dynamic ASP.NET IDs (e.g. 'ctl00$Main$txtMemId_8842'), framesets, and minimal test IDs.
2. Form Navigation: To submit forms or search queries, look for buttons labeled 'Lookup Member', 'Search', 'Submit', or 'Execute'.
3. Set-of-Marks (SoM): Interactive elements may be tagged with numerical badges [0], [1], [2]... Use 'som_label' when targeting marked elements.

[ACTION VOCABULARY]
- navigate: Open a URL. Specify 'value' as the full URL.
- click: Click an element. Specify 'target_description', 'som_label' (if present), or 'selector_hint'.
- type_text: Enter text into an input field. Specify 'target_description', 'som_label' (if present), or 'selector_hint', and 'value' as the text.
- select_option: Select an option from a dropdown. Specify 'target_description', 'som_label' (if present), or 'selector_hint', and 'value' as option value or text.
- extract_data: Read text from a resulting table or cell. Specify 'extraction_field' (e.g. 'savings_balance'), 'target_description', and 'transform' ('parse_currency', 'trim_string').
- finish: Goal is satisfied. Provide extracted results in 'extracted_data'.
- escalate_hitl: Encountered an irreversible confirmation or high-risk state requiring human operator takeover.

[JSON OUTPUT SCHEMA]
You must respond STRICTLY with a valid JSON object matching:
{
  "thought": "Analysis of current page state and reasoning for next step",
  "action": "navigate" | "click" | "type_text" | "select_option" | "extract_data" | "finish" | "escalate_hitl",
  "target_description": "Descriptive name of element (e.g. 'Member ID input text box')",
  "target_frame": "string frame name (e.g. 'app_main_frame') or null",
  "som_label": integer or null,
  "selector_hint": "CSS or XPath hint if visible, or null",
  "value": "string parameter or null",
  "extraction_field": "string or null",
  "transform": "parse_currency" | "trim_string" | null,
  "expected_feedback": "What page change indicates success",
  "checkpoint_rule": {
    "warning_pattern": "text indicating business exception (e.g. 'Warning: No member found')",
    "outcome_code": "MEMBER_NOT_FOUND"
  } or null
}
"""


def build_discovery_user_prompt(
    goal: str,
    step_history: list,
    current_url: str,
    available_elements_summary: str,
) -> str:
    """Build structured prompt for the next discovery iteration with bounded context and PII redaction."""
    def _neutralize(text: str) -> str:
        for delimiter in (
            "[GOAL]", "[CURRENT STATE]", "[VISIBLE INTERACTIVE ELEMENTS]", "[RECENT STEPS TAKEN]",
            "```json", "```", "\nHuman:", "\nAssistant:", "<|im_start|>", "<|im_end|>"
        ):
            text = text.replace(delimiter, f"\\{delimiter}")
        return text

    safe_goal = _neutralize(PIIRedactor.redact_text(str(goal or "")[:2000]))
    safe_url = _neutralize(PIIRedactor.redact_text(str(current_url or "")[:1000]))
    # Bounded DOM text limit (8000 chars) pre-clamped to 10k before in-memory PII scrubbing (INV-06, INV-27, INV-28)
    raw_elements = str(available_elements_summary or "")[:10000]
    safe_elements = _neutralize(PIIRedactor.redact_text(raw_elements)[:8000])

    history_items = step_history if isinstance(step_history, list) else []
    recent_history = history_items[-8:]  # Bounded context window (INV-06)

    formatted_steps = []
    for idx, s in enumerate(recent_history):
        if isinstance(s, dict):
            s_num = int(s.get("step_num", idx + 1)) if str(s.get("step_num", "")).isdigit() else (idx + 1)
            s_act = _neutralize(str(s.get("action", ""))[:50])
            s_tgt = _neutralize(PIIRedactor.redact_text(str(s.get("target", ""))[:500]))
            s_val = _neutralize(PIIRedactor.redact_text(str(s.get("value", ""))[:500]))
            formatted_steps.append(f"Step {s_num}: Action='{s_act}', Target='{s_tgt}', Value='{s_val}'")
        else:
            formatted_steps.append(f"Step {idx + 1}: {_neutralize(PIIRedactor.redact_text(str(s)[:500]))}")

    history_str = "\n".join(formatted_steps) if formatted_steps else "None (Starting step)"

    return f"""[GOAL]
{safe_goal}

[CURRENT STATE]
Current URL: {safe_url}
Recent Step History:
{history_str}

[VISIBLE INTERACTIVE ELEMENTS]
{safe_elements}

Analyze the visual screenshot and elements above. Decide the single next best action to advance towards the goal.
Respond ONLY in the specified JSON format.
"""

