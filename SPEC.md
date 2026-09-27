# SPEC.md: Enterprise Computer-Use Automation System
## Formal Engineering Specification & Operational Contract

> **Target System:** Bank & Credit Union Back-Office Legacy Core Automation  
> **Host Environment:** Legacy Hostile Web (Deeply Nested Tables, Framesets/iframes, No Test IDs, Dynamic Server-Rendered State)  
> **Core Architecture:** Goal-Driven Multimodal Discovery $\rightarrow$ Typed Capability Synthesis $\rightarrow$ Zero-LLM Deterministic Replay  
> **Status:** Binding Technical Contract  

---

## 1. System Overview & Core Philosophy

### 1.1 The Operational Context
US banks and credit unions operate hundreds of core banking platforms, account servicing consoles, and back-office tools that **expose zero APIs**. Human operators spend thousands of hours driving these interfaces through repetitive data entry, member lookups, and transaction reconciliations.

This system provides the **backend integration layer** that gives AI agents reliable "hands" to operate legacy bank applications.

### 1.2 The Core Through-Line
> *"The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production."*

1. **Stage 1 (Discovery):** A multimodal LLM agent operates a real, live browser session under strict guardrails to discover how to satisfy a natural-language goal.
2. **Stage 2 (Compilation):** The verified exploratory run is compiled into an immutable, versioned, typed **Capability Artifact** defining inputs, outputs, multi-tiered locators, and state checkpoints.
3. **Stage 3 (Deterministic Replay):** Production invocations execute the saved artifact **with zero LLM in the decision loop**, achieving sub-second latency, deterministic predictability, and zero token costs.
4. **Cross-Cutting Invariants (Omnipresent):**
   - **Safety & Policy Citadel:** Enforced allowlists, safe vs. risky action gates, and strict in-memory PII redaction active across all stages.
   - **Human-in-the-Loop (HITL) Dual-Stream Seam:** In-situ control handoff on the **exact same live session**, capturing operator actions while preserving browser state.
   - **Unified Observability:** Structured audit logs and forensic failure evidence persisted to `/evidence/`.

---

## 2. Core Engineering Principles

Every component in this codebase strictly adheres to the following non-negotiable software engineering laws:

1. **KISS & YAGNI (Keep It Simple / You Aren't Gonna Need It):**
   - Strictly forbid distributed message queues (Kafka, Celery, RabbitMQ), microservice clusters, or heavy orchestration frameworks.
   - The entire system operates as a cohesive, single-process CLI application with modular component boundaries.
2. **Interface Segregation Principle (ISP):**
   - Clients must never be forced to depend on interfaces they do not use.
   - Discrete interfaces govern `SurfaceDriver` (browser interaction), `PerceptionEngine` (element resolution), `PolicyEnforcer` (guardrails), and `CapabilityExecutor` (replay).
3. **Law of Demeter (Principle of Least Knowledge):**
   - Objects talk only to immediate collaborators. A step execution engine queries the `LocatorResolver` for an element handle; it does not dig into raw Playwright internals or traverse private browser state.
4. **Composition over Inheritance:**
   - Automation behaviors are composed of pluggable strategies (e.g., locator cascades, checkpoint evaluators, action handlers) rather than deep inheritance hierarchies.
5. **Separation of Concerns & Deterministic Code Firewall:**
   - **The Firewall:** LLM client libraries (`google-genai`) are strictly forbidden from being imported or called inside the `replay/` execution engine. The replay engine must function completely offline without internet or API keys.
6. **Advanced High-Performance Computational Strategies:**
   - **In-Memory Hashing & Indexing:** Pre-index DOM accessibility nodes by role and normalized text label into hash tables ($O(1)$ lookup) before falling back to structural tree scans ($O(N)$).
   - **No Slow Iterations:** Strictly forbid slow row-by-row scans (such as Pandas `iterrows()`); use dictionary lookups, tuple iteration, or vectorized set comparisons.
   - **Cheap Pre-Filters:** Check fast $O(1)$ string and URL bounds before executing expensive regex or DOM evaluations.
   - **Bounded Concurrency & Fault Isolation:** Async browser operations must execute within bounded timeouts with explicit cancellation tokens; single-step failures must never corrupt global runtime state.

---

## 3. Executive Architecture Map

The end-to-end dataflow and control boundaries across the system:

```
====================================================================================================
                                      EXECUTIVE ARCHITECTURE MAP
====================================================================================================

      [ NATURAL LANGUAGE GOAL ] + [ ENTRY URL ] (e.g., "Lookup member 1042 savings balance")
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: GOAL-DRIVEN DISCOVERY ENGINE (LLM-IN-THE-LOOP)                                          │
│                                                                                                  │
│   ┌───────────────────────────┐      Observation (SoM Image + AX Tree)     ┌─────────────────┐   │
│   │   Browser Surface Driver  │ ─────────────────────────────────────────> │ Gemini 3.8 VLM  │   │
│   │   (Live Chromium Session) │ <───────────────────────────────────────── │ Multi-Key Pool  │   │
│   └─────────────┬─────────────┘         Structured Action Tool Call        └─────────────────┘   │
│                 │                               (Plan-Act-Verify)                                │
│                 ▼                                                                                │
│   ┌───────────────────────────┐                                                                  │
│   │  Raw Exploratory Trace    │ (Actions, Observations, DOM Snapshots, Screen Deltas)            │
│   └─────────────┬─────────────┘                                                                  │
└─────────────────┼────────────────────────────────────────────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: CAPABILITY COMPILER & SCHEMA SYNTHESIS                                                  │
│                                                                                                  │
│   ┌───────────────────────────┐                                                                  │
│   │ DAG Trace Pruning Pass    │ ──► Strip cyclic backtracks, misclicks, and dead-ends            │
│   │ Parameterization Engine   │ ──► Literal "1042" -> $input.member_id                           │
│   │ Multi-Tier Locator Gen    │ ──► Synthesize: AX Role+Name -> Text Proximity -> XPath -> Coords│
│   │ Checkpoint Synthesizer    │ ──► Assert pre/post conditions and business outcome branches     │
│   └─────────────┬─────────────┘                                                                  │
│                 ▼                                                                                │
│   ┌───────────────────────────┐                                                                  │
│   │ Capability Artifact JSON  │ (Pydantic v2 Contract: Typed Inputs, Outputs, Steps, Checkpoints)│
│   └─────────────┬─────────────┘                                                                  │
└─────────────────┼────────────────────────────────────────────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: DETERMINISTIC REPLAY ENGINE (ZERO-LLM IN PRODUCTION)                                    │
│                                                                                                  │
│   [ Input Params: member_id = "2088" ]                                                           │
│                 │                                                                                │
│                 ▼                                                                                │
│   ┌───────────────────────────┐       Fast Multi-Tier Match (<20ms)       ┌──────────────────┐   │
│   │ Replay Step Dispatcher    │ ────────────────────────────────────────> │ Hostile Bank DOM │   │
│   │ (Zero LLM Dependencies)   │ <──────────────────────────────────────── │ (Tables/iframes) │   │
│   └─────────────┬─────────────┘               DOM State Delta             └──────────────────┘   │
│                 │                                                                                │
│                 ▼                                                                                │
│   ┌───────────────────────────┐                                                                  │
│   │ Tri-Partite Classifier    │                                                                  │
│   └──────┬──────────────┬─────┘                                                                  │
│          │              │                                                                        │
│          ▼              ▼                                              ▼                         │
│   [Business Outcome] [Recoverable Glitch]                      [Hard Failure]                    │
│   ("Member Not Found" (Auto-dismiss modal,                      (Element Missing                  │
│    -> Return 200 OK)   retry slow query)                         -> Halt & Dump)                 │
└────────────────────────────────────────────────────────────────────────┬─────────────────────────┘
                                                                         │
═════════════════════════════════════════════════════════════════════════╪═════════════════════════
CROSS-CUTTING INVARIANTS (ACTIVE ACROSS ALL STAGES)                      │
                                                                         │
 1. SAFETY CITADEL: Domain allowlist gate, risky action confirmations,   │
    in-memory PII masking.                                               │
                                                                         ▼
 2. HUMAN-IN-THE-LOOP (HITL) SEAM: ──────────────────────────────► [Trigger: Stuck/Fatal/Risk]
    - Pause automation on SAME live session (Browser PID preserved).     │
    - Stream 1: In-browser event listener records human clicks/types.    │
    - Stream 2: Before & after DOM/visual snapshot diff.                 ▼
    - Resume: Re-ground automation; write /evidence/human_intervention.json
                                                                         │
 3. OBSERVABILITY: Write JSONL audit logs, replay traces, and screenshots▼to /evidence/
====================================================================================================
```

---

## 4. Repository Manifest & Directory Layout

The project structure enforces clean boundaries and zero circular dependencies:

```
c:\Users\yella\interface.ai_build\
├── .env                                # Multi-key configuration (GEMINI_API_KEY_1..7)
├── README.md                           # Quickstart, setup, and exact demo commands
├── REPORT.md                           # Required 7-heading technical design write-up
├── SPEC.md                             # This binding contract
├── CRITICAL_SPECIFICATION_ANALYSIS.md  # Domain requirements analysis
│
├── evidence/                           # Submission deliverables & audit logs
│   ├── capability_artifact.json        # Compiled production capability artifact
│   ├── discovery_run.log               # Full transcript of genuine LLM discovery
│   ├── replay_success.log              # Log of deterministic replay (happy path)
│   ├── replay_business_outcome.log     # Log of replay handling "Member Not Found"
│   ├── replay_failure.log              # Log of replay catching hard injected fault
│   ├── human_intervention.json         # Dual-stream recorded human handoff trace
│   └── screenshots/                    # Failure and milestone screenshots (.png)
│
├── mock_bank/                          # Target Application (The Stand-In)
│   ├── __init__.py
│   ├── server.py                       # Self-contained Starlette/Uvicorn server
│   ├── database.py                     # In-memory bank database (Members, Accounts, Balances)
│   ├── templates/                      # Hostile legacy templates (Nested tables, iframes)
│   │   ├── base_frameset.html          # Legacy HTML frameset / iframe navigation
│   │   ├── member_search.html          # Unlabelled inputs, dynamic IDs, table search
│   │   ├── member_detail.html          # Deeply nested table layout with account details
│   │   ├── transfer_wizard.html        # Multi-step transfer with confirmation dialogs
│   │   └── error_pages.html            # Session timeout modal & 500 error templates
│   └── static/
│       └── legacy.css                  # Minimal, non-modern styling (raw HTML look)
│
├── src/                                # Core System Package
│   ├── __init__.py
│   ├── cli.py                          # Typer CLI entrypoint (`discover`, `replay`, `server`)
│   │
│   ├── common/                         # Shared utilities & interfaces
│   │   ├── __init__.py
│   │   ├── config.py                   # Environment loader & key pool settings
│   │   ├── logger.py                   # Structured JSONL + Rich console logger
│   │   └── types.py                    # Shared Enums (RiskLevel, OutcomeType, ActionType)
│   │
│   ├── schemas/                        # Pydantic v2 Contracts
│   │   ├── __init__.py
│   │   ├── artifact.py                 # CapabilityArtifact, Step, Locator, Checkpoint
│   │   ├── execution.py                # ReplayInput, ReplayResult, BusinessOutcome
│   │   └── hitl.py                     # HumanInterventionRecord, RecordedAction
│   │
│   ├── surface/                        # Perception & Browser Automation Driver
│   │   ├── __init__.py
│   │   ├── driver.py                   # Playwright wrapper (headed/headless, frame mgmt)
│   │   ├── locator_engine.py           # Multi-tiered cascade resolution (<20ms)
│   │   └── som_annotator.py            # Set-of-Marks visual bbox + index injector
│   │
│   ├── discovery/                      # Stage 1: LLM-Driven Exploration
│   │   ├── __init__.py
│   │   ├── agent.py                    # Plan-Act-Verify (PAV) state machine
│   │   ├── gemini_client.py            # Google Gemini 3.8 wrapper with 7-key rotation
│   │   ├── prompts.py                  # Strict structured prompt templates
│   │   └── compiler.py                 # Stage 2: DAG trace pruner & schema synthesizer
│   │
│   ├── replay/                         # Stage 3: Zero-LLM Deterministic Engine
│   │   ├── __init__.py
│   │   ├── executor.py                 # Deterministic step runner & value binder
│   │   ├── classifier.py               # Tri-partite outcome evaluator
│   │   └── recovery.py                 # Transient glitch self-healer (modal dismiss)
│   │
│   ├── guardrails/                     # Cross-Cutting: Safety & Policies
│   │   ├── __init__.py
│   │   ├── allowlist.py                # Domain and route pattern interceptor
│   │   ├── risk_governor.py            # Safe vs. Risky action barrier
│   │   └── pii_redactor.py             # In-memory regex + entropy PII sanitizer
│   │
│   └── hitl/                           # Cross-Cutting: Human Escalation Seam
│       ├── __init__.py
│       ├── handoff.py                  # Live-session pause/resume controller
│       ├── event_recorder.js           # Injected DOM script for Stream 1 capture
│       └── diff_engine.py              # Macro state delta comparator for Stream 2
│
└── tests/                              # Automated Verification Suite
    ├── __init__.py
    ├── test_mock_bank.py               # Verify stand-in application behavior
    ├── test_locators.py                # Verify multi-tier fallback resolution
    ├── test_classifier.py              # Verify tri-partite taxonomy differentiation
    ├── test_guardrails.py              # Verify domain blocking and PII scrubbing
    └── test_end_to_end.py              # Full loop: Discover -> Compile -> Replay
```

---

## 5. Target Application Specification (`mock_bank`)

The stand-in application must faithfully recreate the architectural characteristics of legacy core banking software (e.g., FIS, Fiserv, Jack Henry) rather than a clean modern React app.

### 5.1 Hostile DOM Constraints (Deliberately Non-Semantic)
1. **Layout Primitive:** Built exclusively with nested HTML tables (`<table><tr><td>...</td></tr></table>`) 3–5 levels deep.
2. **Zero Test Attributes:** No `data-testid`, `data-cy`, or automated testing selectors anywhere in the markup.
3. **Dynamic Generated IDs:** Form elements use ASP.NET WebForms style dynamic IDs:
   - `<input name="ctl00$Main$txtMemId_8842" id="ctl00_Main_txtMemId_8842" type="text" />`
4. **iFrame / Frameset Isolation:** The primary navigation menu resides in a header frame; the workspace loads inside an `<iframe>` named `app_main_frame`.
5. **Non-Standard Controls:** Buttons rendered as styled `<a>` tags with `href="javascript:__doPostBack(...)"` or unstyled `<input type="button">`.

### 5.2 Banking Workflows & State Matrix

| Workflow | Path / Steps | Happy Path State | Exceptional / Error State |
| :--- | :--- | :--- | :--- |
| **1. Member Lookup** | `/servicing/lookup` $\rightarrow$ search form $\rightarrow$ results | Displays table: Member Name, DOB, Active Accounts, Savings Balance. | **Business Outcome:** *"No member found matching ID {id}"* (HTTP 200). |
| **2. Balance Inquiry** | Member Details $\rightarrow$ click Account tab $\rightarrow$ read ledger | Extracts Available Balance and Pending Balance. | **Recoverable:** Loading indicator spinner (`#loading_indicator`) active for 1.5s. |
| **3. Sub-Account Open / Transfer** | Form with Amount, Source, Dest $\rightarrow$ Review Dialog $\rightarrow$ Submit | Confirmation number generated (`CONF-77291`). | **Risky Action:** Requires confirmation modal. **Exceptional:** *"Manager Override Code Required"*. |
| **4. Session Security** | Triggered on `/servicing/secure_vault` or inactivity | Reaches vault data. | **Session Expiry:** Modal overlay *"Session Expired - Re-authenticate"*. |

### 5.3 Deterministic Seeding (`mock_bank/database.py`)
The in-memory database is pre-seeded with predictable entities for automated testing:
- `Member 1042`: Jane Doe, Regular Savings: `$4,850.25`, Status: `ACTIVE`
- `Member 2088`: Robert Smith, Regular Savings: `$12,450.00`, Status: `ACTIVE`
- `Member 3011`: Maria Garcia, Regular Savings: `$150.00`, Status: `FROZEN`
- `Member 9999`: Non-existent (triggers clean `MEMBER_NOT_FOUND` business outcome)

---

## 6. Capability Artifact Schema Specification (`schemas/artifact.py`)

The Capability Artifact is the core contract. It must be 100% typed, serializable to JSON/YAML, human-reviewable, and agent-invocable.

### 6.1 Formal Pydantic v2 Schema Definitions

```python
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, HttpUrl

class RiskLevel(str, Enum):
    SAFE_READ = "safe_read_only"
    SAFE_IDEMPOTENT_WRITE = "safe_idempotent_write"
    RISKY_IRREVERSIBLE = "risky_irreversible"

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    EXTRACT_DATA = "extract_data"
    WAIT_FOR_STATE = "wait_for_state"

class LocatorStrategyType(str, Enum):
    ROLE_AND_NAME = "role_and_name"          # Tier 1: Accessibility tree
    LABEL_PROXIMITY = "label_proximity"      # Tier 2: Spatial label relation
    STRUCTURAL_XPATH = "structural_xpath"    # Tier 3: Invariant DOM structure
    COORDINATE_RATIO = "coordinate_ratio"    # Tier 4: Relative bbox fallback

class LocatorTier(BaseModel):
    strategy: LocatorStrategyType
    role: Optional[str] = None
    name: Optional[str] = None
    label_text: Optional[str] = None
    direction: Optional[Literal["right", "below", "inside"]] = "right"
    xpath: Optional[str] = None
    coords_ratio: Optional[List[float]] = None  # [x_ratio, y_ratio] (0.0 to 1.0)

class LocatorCascade(BaseModel):
    tiers: List[LocatorTier] = Field(..., min_items=1)
    robustness_rationale: str
    target_frame: Optional[str] = None  # if located inside an iframe

class CheckpointBranch(BaseModel):
    condition_type: Literal["text_visible", "element_visible", "url_matches"]
    pattern: str
    outcome_category: Literal["continue", "business_outcome", "hard_failure"]
    outcome_code: Optional[str] = None  # e.g., "MEMBER_NOT_FOUND"
    message: Optional[str] = None

class StepCheckpoint(BaseModel):
    wait_condition: Literal["dom_content_loaded", "network_idle", "element_attached"]
    timeout_ms: int = 5000
    assertions: Optional[List[str]] = None
    branches: Optional[List[CheckpointBranch]] = None

class StepExtraction(BaseModel):
    output_field: str
    locator: LocatorCascade
    transform: Literal["raw_text", "parse_currency", "trim_string", "extract_digits"]

class CapabilityStep(BaseModel):
    step_id: str
    description: str
    action: ActionType
    locator: Optional[LocatorCascade] = None
    value_binding: Optional[str] = None  # e.g. "$input.member_id" or raw string
    is_sensitive: bool = False
    extractions: Optional[List[StepExtraction]] = None
    checkpoint: Optional[StepCheckpoint] = None

class ParameterProperty(BaseModel):
    type: Literal["string", "number", "integer", "boolean"]
    description: str
    pattern: Optional[str] = None
    default: Optional[Any] = None

class CapabilityInterface(BaseModel):
    inputs: Dict[str, ParameterProperty]
    required_inputs: List[str]
    outputs: Dict[str, ParameterProperty]
    required_outputs: List[str]

class CapabilityMetadata(BaseModel):
    capability_id: str
    version: str = "1.0.0"
    description: str
    target_app: str
    base_url_pattern: str
    surface_type: Literal["legacy_web", "modern_web", "desktop"]
    risk_level: RiskLevel
    created_at_utc: str

class CapabilityArtifact(BaseModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    metadata: CapabilityMetadata
    interface: CapabilityInterface
    guardrails_ref: Dict[str, Any]
    steps: List[CapabilityStep]
```

---

## 7. Stage 1: Goal-Driven Discovery Engine Specification

### 7.1 Multi-Key Rotation Pool (`discovery/gemini_client.py`)
To ensure zero failure from API rate limits during genuine discovery runs:
1. Load keys `GEMINI_API_KEY_1` through `GEMINI_API_KEY_7` from `.env`.
2. Maintain an in-memory key state manager tracking request counts and rate-limit backoffs.
3. On HTTP 429 (Too Many Requests) or ResourceExhausted:
   - Mark the current key as cooling down for 60 seconds.
   - Instantly switch to the next key in the pool ($O(1)$ round-robin).
   - Retry the discovery step with zero data loss.

### 7.2 Structured Prompt Formula (`discovery/prompts.py`)
Every prompt sent to Gemini 3.8 must strictly adhere to the formula:
`[PERSONA] + [RELEVANT CONTEXT] + [STEPS TO DO] + [OUTPUT SCHEMA & ALLOWED CATEGORIES]`

```
================================ DISCOVERY PROMPT TEMPLATE ================================
[PERSONA]
You are an expert enterprise automation engineer operating a legacy banking back-office
system. Your mission is to find the most direct, robust path to satisfy the specified goal.

[RELEVANT CONTEXT]
- Natural Language Goal: {goal}
- Current URL: {current_url}
- Active Frame: {active_frame}
- Page Title: {page_title}
- Interactive Controls (Set-of-Marks Indexed):
{som_control_list}
- Current Visual Screenshot: [ATTACHED_IMAGE]

[STEPS TO DO]
1. Observe the current screen and identify if the goal or a sub-step has been achieved.
2. Select the next single best action from the allowed action set.
3. Reference controls strictly by their SoM index [ID] and semantic purpose.
4. If an unexpected modal or dialog appears, plan a resolution or escalate.
5. Formulate an explicit EXPECTED STATE DELTA: what should change after your action?

[OUTPUT SCHEMA & ALLOWED CATEGORIES]
You must respond strictly with valid JSON conforming to this schema:
{
  "thought_process": "String explaining the operational intent and locator choice",
  "action_type": "CLICK" | "TYPE_TEXT" | "NAVIGATE" | "EXTRACT" | "FINISH" | "ESCALATE",
  "target_control_id": integer or null,
  "input_value": "String value or null",
  "expected_state_delta": "String describing the expected next page condition",
  "is_goal_complete": boolean
}
===========================================================================================
```

### 7.3 Set-of-Marks (SoM) Perception & AXTree Grounding (`surface/som_annotator.py`)
1. Inject a transient non-destructive DOM script that locates all visible interactive controls (`<button>`, `<a>`, `<input>`, `<select>`, and click-handled elements).
2. Compute their bounding client rectangles.
3. Render a lightweight SVG overlay placing high-contrast, numbered badges `[1]`, `[2]`, ... directly onto the controls.
4. Capture a full screenshot with annotations and simultaneously export an index lookup map:
   `[3] -> { role: "button", text: "Lookup Member", xpath: "//table//input[@type='submit']" }`
5. Remove the SVG overlay before firing any click.

### 7.4 Plan-Act-Verify (PAV) State Machine
- **Verify Loop:** After action execution, compare pre-action DOM hash and URL to post-action state.
- **Dead-End / Stuck Detector:** If state delta is identical for 3 consecutive turns $\rightarrow$ raise escalation event.
- **Max Steps Ceiling:** Hard limit of 15 steps during discovery to prevent infinite runaway loops.

---

## 8. Stage 2: Capability Compilation & Synthesis

The Compiler (`discovery/compiler.py`) transforms the raw exploratory trace into a pristine `CapabilityArtifact`:

1. **DAG Trace Pruning Pass:**
   - Detect cyclic steps: if step $N$ navigated to URL $A$, step $N+1$ clicked wrong link to URL $B$, and step $N+2$ clicked "Back" to URL $A$, the cycle $[N+1, N+2]$ is pruned from the artifact.
2. **Input Parameterization ($O(N)$ string matching):**
   - The user's input parameter (e.g. member ID `"1042"`) is matched against literal typed values.
   - Replaces literal string with binding reference: `value_binding = "$input.member_id"`.
3. **Multi-Tier Locator Generation:**
   - For every interacted control, queries Playwright to extract:
     - Tier 1: AXTree `role` and `accessible_name`.
     - Tier 2: Preceding label text or adjacent table cell text within a 150px horizontal bounding box.
     - Tier 3: Invariant XPath (stripping dynamic ASP.NET IDs `ctl00_...`).
     - Tier 4: Viewport coordinate ratio $(x / W, y / H)$.
4. **Checkpoint Assertion Synthesis:**
   - Inspects the DOM following a search click.
   - Synthesizes branching logic: if text `"No member found"` is discovered in testing, registers a `business_outcome` branch for `MEMBER_NOT_FOUND`.

---

## 9. Stage 3: Deterministic Replay Engine Specification

### 9.1 Zero-LLM Production Execution Firewall
The Replay Engine (`replay/executor.py`) is completely decoupled from all LLM SDKs.
- **Execution Speed Target:** $<25\text{ms}$ locator resolution; full member lookup replay in $<600\text{ms}$.
- **Zero API Cost:** Evaluates pure deterministic locator cascades.

### 9.2 Fast Multi-Tier Locator Resolution Pipeline
When resolving a `LocatorCascade`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                      STEP LOCATOR RESOLUTION FLOW                       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
       [ Tier 1: Role + Name ] ────┴────► Found uniquely? (<15ms)
                                                │
                                                ├─► YES ──► Return Element Handle
                                                │
                                                ▼ NO (Missing / Ambiguous)
   [ Tier 2: Label Proximity ] ────┴────► Found adjacent input? (<10ms)
                                                │
                                                ├─► YES ──► Return Element Handle
                                                │
                                                ▼ NO (Table layout shifted)
  [ Tier 3: Structural XPath ] ────┴────► Matches invariant path?
                                                │
                                                ├─► YES ──► Return Element Handle
                                                │
                                                ▼ NO (All DOM strategies exhausted)
 [ Tier 4: Coordinate Ratio ] ─────┴────► Click normalized viewport bbox
```

### 9.3 Tri-Partite Error Taxonomy & Result Contract

Every replay invocation yields a structured `ReplayResult` conforming to the strict tripartite contract:

```python
class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"

class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    duration_ms: int
    data: Optional[Dict[str, Any]] = None              # Populated on SUCCESS
    business_outcome_code: Optional[str] = None       # e.g., "MEMBER_NOT_FOUND"
    message: Optional[str] = None
    failure_context: Optional[Dict[str, Any]] = None  # Step, expected, observed, evidence paths
```

#### The Three Outcomes Defined:
1. **`SUCCESS`:** All steps executed, checkpoints confirmed, all declared outputs extracted and typed.
2. **`BUSINESS_OUTCOME`:** The target application returned a legitimate business answer (e.g. member does not exist, account frozen). **This is a successful execution, not an error.**
3. **`HARD_FAILURE`:** A step locator was completely unresolved after timeouts, or a 500 error occurred. Execution halts, triggers failure capture, and writes screenshots/DOM to `/evidence/`.

#### Automatic Recovery from Transient Conditions (`replay/recovery.py`):
Before declaring a hard failure, the engine executes transient recovery:
- Detects known modal banners (e.g., maintenance notification) $\rightarrow$ clicks `"OK"`.
- Detects loading spinner `#loading_indicator` $\rightarrow$ awaits element detachment (up to 3000ms).
- Resumes step execution without caller intervention.

---

## 10. Cross-Cutting Invariant: Omnipresent Safety Citadel

The Safety Citadel (`guardrails/`) operates as an interceptor pipeline across both Discovery and Replay.

```
Request to Act ──► [Layer 1: Route Allowlist] ──► [Layer 2: Risk Governor] ──► Action Dispatched
                                                                                      │
Result Data    ◄── [Layer 3: PII Redactor]    ◄───────────────────────────────────────┘
```

### 10.1 Layer 1: Strict Route & Domain Interceptor
- Enforces an immutable allowlist: `localhost:8000`, `127.0.0.1:8000`.
- Hooks Playwright's `page.route("**/*")`. Any network request or navigation targeting an external domain is instantly aborted with `route.abort("blockedbyclient")`.

### 10.2 Layer 2: Action Risk Governor
- Classifies actions by risk level:
  - `SAFE_READ`: Navigations, reading text, typing search queries. Executes automatically.
  - `RISKY_IRREVERSIBLE`: Fund transfers, deletions, account modifications.
- **Enforcement Policy:** If an action attempts a risky operation in `STRICT` mode, execution automatically pauses and triggers the **Human-in-the-Loop Seam** for human operator confirmation.

### 10.3 Layer 3: In-Memory PII & Credential Redactor
- All loggers, error traces, and artifact serializers pipe text through `pii_redactor.py`.
- Evaluates compiled $O(1)$ prefix filters and regex patterns:
  - SSN: `\b\d{3}-\d{2}-\d{4}\b` $\rightarrow$ `[REDACTED_SSN]`
  - Credit Card / Account numbers: `\b\d{10,16}\b` $\rightarrow$ `[REDACTED_ACCT]`
  - Passwords / PINs: Fields matching `type="password"` or `name="*pin*"` $\rightarrow$ `[REDACTED_CREDENTIAL]`
- Zero raw PII is ever written to disk or sent across network sockets.

---

## 11. Cross-Cutting Invariant: Human-in-the-Loop (HITL) Seam

### 11.1 The In-Situ Live Session Architecture
Per Section 3.6 of the PDF, the human operator takes over the **exact same live browser window**. The browser session is never terminated or restarted.

### 11.2 The Hybrid Dual-Stream Operator Recorder

```
                      ESCALATION TRIGGER (Stuck / Fatal / Risky)
                                          │
            ┌─────────────────────────────┴─────────────────────────────┐
            ▼                                                           ▼
┌───────────────────────────────────────┐   ┌───────────────────────────────────────────┐
│ STREAM 1: DISCRETE ACTION CAPTURE     │   │ STREAM 2: STRUCTURAL STATE SENTINEL       │
│ - Injected via page.add_init_script() │   │ - Captures Baseline Snapshot:             │
│ - Listens to: click, input, change    │   │   * URL_pre                               │
│ - Persists across reloads & iframes   │   │   * DOM_pre                               │
│ - Debounces keystrokes (400ms burst)  │   │   * Screenshot_pre                        │
│ - Masks passwords/SSN inside browser  │   │                                           │
│ - Emits over page.expose_binding()    │   │                                           │
└───────────────────┬───────────────────┘   └─────────────────────┬─────────────────────┘
                    │                                             │
                    │         (Human interacts in browser)        │
                    ▼                                             ▼
          Captured Action Stream                    Operator Signals RESUME (CLI)
          1. click(#btnOverrides)                                 │
          2. type(#txtAuth, "[MASKED]")                           ▼
          3. click(#btnSubmit)                      - Captures Post Snapshot:
                    │                                   * URL_post
                    │                                   * DOM_post
                    │                                   * Screenshot_post
                    │                                             │
                    └──────────────────────┬──────────────────────┘
                                           │
                                           ▼
                    ┌─────────────────────────────────────────────┐
                    │ RECONCILIATION & AUDIT SYNTHESIZER          │
                    │ - Correlates events with state delta        │
                    │ - Prunes blank-space clicks                 │
                    │ - Generates /evidence/human_intervention.json│
                    └─────────────────────────────────────────────┘
```

### 11.3 Injected In-Browser Recorder (`hitl/event_recorder.js`)
```javascript
// Injected at document-start into all frames and future navigations
(() => {
  if (window.__interface_ai_recorder_active) return;
  window.__interface_ai_recorder_active = true;

  // Render non-intrusive floating status dock
  const dock = document.createElement('div');
  dock.id = '__operator_dock__';
  dock.innerHTML = '<strong>AUTOMATION PAUSED</strong> — Operator in Control (Actions Recorded)';
  dock.style.cssText = 'position:fixed;bottom:12px;right:12px;background:#1e293b;color:#f8fafc;padding:10px 16px;border-radius:8px;z-index:2147483647;font-family:sans-serif;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,0.3);border-left:4px solid #f59e0b;';
  document.body.appendChild(dock);

  // Discrete Click Listener
  document.addEventListener('click', (e) => {
    const target = e.target;
    if (target.closest('#__operator_dock__')) return;
    const info = {
      action: 'click',
      tag: target.tagName,
      text: (target.innerText || target.value || '').substring(0, 50),
      role: target.getAttribute('role') || target.type || target.tagName.toLowerCase(),
      id: target.id || null,
      timestamp_ms: Date.now()
    };
    if (window.__interface_ai_record_event__) {
      window.__interface_ai_record_event__(JSON.stringify(info));
    }
  }, true);
})();
```

---

## 12. Strict Acceptance Targets & Calibration Pipeline

The implementation must achieve 100% compliance against these strict quantitative targets:

| Dimension | Target Metric | Verification Mechanism |
| :--- | :--- | :--- |
| **Replay Latency** | $<600\text{ms}$ total per lookup flow | Automated benchmark timer in `test_end_to_end.py` |
| **Locator Resolution** | $<25\text{ms}$ per step | In-memory locator micro-benchmarks |
| **Replay Determinism** | 10/10 successive identical replays | Multi-run stability loop asserting 100% consistency |
| **Outcome Differentiation** | 100% accuracy (`MEMBER_NOT_FOUND` $\neq$ Crash) | Test suite asserting clean business outcome code |
| **PII Scrubbing** | Zero plain-text SSNs/passwords in logs | Automated regex audit over all files in `/evidence/` |
| **HITL Handoff Integrity** | Browser PID identical before & after takeover | OS Process ID assertion in `test_hitl_handoff.py` |

---

## 13. Deliverable Documentation Contracts

### 13.1 `/README.md` Contract
Must contain:
1. System overview and requirements (Python 3.11+, Playwright).
2. Setup instructions: virtualenv creation, dependency installation, Playwright browser install (`playwright install chromium`).
3. Running with `.env` (Gemini API keys) and running in offline mode (using pre-compiled artifacts).
4. Exact CLI demo commands:
   - `python -m src.cli serve` (Start mock banking portal)
   - `python -m src.cli discover --goal "Lookup member 1042 savings balance"`
   - `python -m src.cli replay --artifact evidence/capability_artifact.json --input '{"member_id":"2088"}'`
   - `python -m src.cli replay --artifact evidence/capability_artifact.json --input '{"member_id":"9999"}'` (Error demo)
   - `python -m src.cli test` (Run full verification test suite)

### 13.2 `/REPORT.md` Contract
Must be 1–3 pages and strictly use the **exact seven headings** specified in Section 6.2 of the PDF:
1. `1. Architecture` — Component boundaries, state machine, zero-LLM replay firewall, and key trade-offs.
2. `2. Artifact schema` — The Pydantic schema design, typed interfaces, locator cascade, and contracts.
3. `3. Determinism & error handling` — Fast locator waterfall, waiting strategy, and tri-partite taxonomy (business outcome vs. recoverable vs. hard failure).
4. `4. Heterogeneity & multi-tenant` — Surface abstraction (Playwright web vs. Windows desktop UIA) and multi-tenant overlay inheritance (`BaseCapability` + `TenantOverlay`).
5. `5. Escalation & handoff` — In-situ live session pause, dual-stream operator recording, and resumption protocol.
6. `6. Safety` — Route allowlisting, safe vs. risky action gates, and in-memory PII sanitization.
7. `7. Cuts` — What was deliberately left out (e.g. out-of-scope full VNC console, premature distributed queues), why, and next steps.

---

## 14. Binding Agreement & Immediate Next Steps

This specification is the single source of truth for the system build. Every module, class, and execution path will adhere strictly to the contracts defined herein.

**Awaiting user review and feedback.** Once approved, we will formulate `implementation_plan.md` to begin construction.
