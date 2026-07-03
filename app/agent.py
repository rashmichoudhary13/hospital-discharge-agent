# ruff: noqa
"""
Hospital Discharge Coordinator Agent  (RPM-optimised)
======================================================
Optimised multi-agent ADK 2.x architecture — stays under 7 RPM on free tier.

RPM optimisation changes:
  1. ClinicalClearanceAgent + AdminClearanceAgent merged into ClearanceAgent
     (4 tool calls in one agent pass instead of 2 separate LLM sessions → -2 RPM).
  2. DischargeSummaryAgent replaced by a deterministic Python FunctionTool
     (0 LLM calls instead of 1 → -1 RPM).
  3. All agent prompts trimmed to reduce token-level retries.
  Net: ~5-6 RPM per discharge vs 12-14 previously.

Flow:
  [Security Checkpoint callback]
       ↓
  ClearanceAgent          (doctor + labs + billing + pharmacy — one agent)
       ↓
  build_discharge_summary (Python FunctionTool — zero LLM calls)
       ↓
  FollowUpBookingAgent    (schedule follow-up appointment)
       ↓
  NotificationAgent       (notify patient + staff)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams, StdioServerParameters
from google.genai import types

from app.config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SECURITY CHECKPOINT — before_agent_callback (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

# PII patterns relevant to hospital domain
_PII_PATTERNS = {
    "MRN": re.compile(r"\bMRN[-:\s]?\d{5,10}\b", re.IGNORECASE),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "DOB": re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/\d{4}\b"),
    "PHONE": re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "INSURANCE_ID": re.compile(r"\b[A-Z]{2,4}\d{8,12}\b"),
}

# Prompt injection detection keywords
_INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "disregard your instructions",
    "you are now",
    "forget your role",
    "system prompt",
    "jailbreak",
    "override instructions",
    "bypass safety",
    "act as",
    "pretend to be",
    "ignore all previous",
]


def _scrub_pii(text: str) -> tuple[str, list[str]]:
    """Redact PII from text. Returns (scrubbed_text, list_of_found_types)."""
    found = []
    for pii_type, pattern in _PII_PATTERNS.items():
        if pattern.search(text):
            found.append(pii_type)
            text = pattern.sub(f"[REDACTED_{pii_type}]", text)
    return text, found


def _detect_injection(text: str) -> bool:
    """Returns True if prompt injection is detected."""
    lower = text.lower()
    return any(kw in lower for kw in _INJECTION_KEYWORDS)


def _audit_log(event: str, severity: str, details: dict) -> None:
    """Emit a structured JSON audit log entry."""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event,
        "severity": severity,
        "details": details,
    }
    logger.info("AUDIT: %s", json.dumps(entry))


def security_checkpoint(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """
    before_agent_callback — runs before every LlmAgent invocation.

    Security controls:
    • PII scrubbing: MRN, SSN, DOB, phone, email, insurance ID
    • Prompt injection: keyword detection
    • Domain rule: restricted patient ID check
    • Structured JSON audit log on every call

    Returns None to allow the agent to proceed, or a Content block to
    short-circuit the agent with a security error message.
    """
    # Get the last user message text
    user_text = ""
    if callback_context._invocation_context.user_content:
        for part in callback_context._invocation_context.user_content.parts or []:
            if hasattr(part, "text") and part.text:
                user_text += part.text

    if not user_text:
        _audit_log("SECURITY_PASS", "INFO", {"reason": "no user text"})
        return None

    # 1. Prompt injection check (before PII scrub — check original)
    if _detect_injection(user_text):
        _audit_log(
            "INJECTION_ATTEMPT",
            "CRITICAL",
            {"snippet": user_text[:120], "action": "blocked"},
        )
        return types.Content(
            role="model",
            parts=[
                types.Part(
                    text="⛔ SECURITY ALERT: Prompt injection attempt detected. "
                    "Request blocked and logged. Please submit a legitimate discharge query."
                )
            ],
        )

    # 2. PII scrubbing
    scrubbed, pii_found = _scrub_pii(user_text)
    if pii_found:
        _audit_log(
            "PII_DETECTED",
            "WARNING",
            {"types_found": pii_found, "action": "redacted_in_logs"},
        )

    # 3. Domain rule: restricted patient check
    restricted_ids = ["P999", "RESTRICTED"]
    for rid in restricted_ids:
        if rid.lower() in user_text.lower():
            _audit_log(
                "RESTRICTED_PATIENT",
                "CRITICAL",
                {"patient_hint": rid, "action": "blocked"},
            )
            return types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=f"⛔ ACCESS DENIED: Patient record is flagged as restricted. "
                        "Discharge request blocked. Contact hospital administration."
                    )
                ],
            )

    # 4. All clear — proceed
    _audit_log(
        "SECURITY_CLEAR",
        "INFO",
        {"pii_found": pii_found, "injection": False},
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MCP TOOLSETS — connect to mcp_server.py via stdio (Phase 3)
# Using sys.executable ensures the venv Python is used for the subprocess
# ─────────────────────────────────────────────────────────────────────────────

def _make_mcp_toolset() -> MCPToolset:
    """Create a fresh MCPToolset pointing to our hospital MCP server."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "app.mcp_server"],
            ),
            timeout=10.0,
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# DISCHARGE SUMMARY — Python FunctionTool (ZERO LLM calls, saves 1 RPM)
# ─────────────────────────────────────────────────────────────────────────────

def build_discharge_summary(
    patient_id: str,
    patient_name: str,
    condition: str,
    lab_details: str,
    medications: str,
    followup_department: str,
) -> str:
    """
    Generate the official discharge summary document without an LLM call.

    Args:
        patient_id: Hospital patient ID (e.g. P001).
        patient_name: Full name of the patient.
        condition: Patient medical condition / procedure.
        lab_details: Lab report summary text.
        medications: Comma-separated list of medications.
        followup_department: Department for the follow-up visit.

    Returns:
        Formatted discharge summary as a plain string.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    meds_list = "\n".join(
        f"  • {m.strip()}" for m in medications.split(",") if m.strip()
    )
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏥 HOSPITAL DISCHARGE SUMMARY\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Patient ID       : {patient_id}\n"
        f"Patient Name     : {patient_name}\n"
        f"Condition        : {condition}\n"
        f"Discharge Date   : {today}\n"
        "Discharge Status : APPROVED ✅\n\n"
        "Clearances Obtained:\n"
        "  • ✅ Doctor Approval  : Confirmed\n"
        f"  • ✅ Lab Reports      : {lab_details}\n"
        "  • ✅ Billing          : Settled\n"
        "  • ✅ Prescriptions    : Ready for collection\n\n"
        "Medications to collect at pharmacy:\n"
        f"{meds_list}\n\n"
        "Discharge Instructions:\n"
        "  1. Rest for 48 hours and avoid strenuous activity.\n"
        "  2. Take all prescribed medications as directed.\n"
        f"  3. Attend follow-up appointment at {followup_department}.\n\n"
        "Emergency contact: Hospital main line\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


discharge_summary_tool = FunctionTool(func=build_discharge_summary)


# ─────────────────────────────────────────────────────────────────────────────
# SUB-AGENTS — optimised for minimum LLM calls
# ─────────────────────────────────────────────────────────────────────────────

# MERGED: ClinicalClearanceAgent + AdminClearanceAgent in one pass (saves ~2 RPM)
clearance_agent = LlmAgent(
    name="ClearanceAgent",
    model=config.model,
    description=(
        "Runs all 4 discharge clearance checks in one pass: "
        "doctor approval, lab reports, billing, and pharmacy."
    ),
    instruction="""
You are the ClearanceAgent. Run all 4 clearance checks for the given patient_id.

1. Call get_doctor_approval(patient_id).
   - If NOT 'Approved': report blockage. STOP.
2. Call get_lab_status(patient_id).
   - If NOT 'Completed': report blockage. STOP.
3. Call get_billing_status(patient_id).
   - If NOT 'Paid': report outstanding amount. STOP.
4. Call get_prescription_status(patient_id).
   - If NOT 'Ready': report blockage. STOP.

If all 4 pass: report "✅ ALL CLEARANCES GRANTED" with a brief summary:
  - Patient name and condition
  - Lab details
  - Medication list (comma-separated)
  - Department
Be concise.
""",
    tools=[_make_mcp_toolset()],
    before_agent_callback=security_checkpoint,
)

followup_booking_agent = LlmAgent(
    name="FollowUpBookingAgent",
    model=config.model,
    description="Books the post-discharge follow-up appointment for the patient.",
    instruction="""
You are the FollowUpBookingAgent. Book a follow-up appointment.

Call book_followup_appointment(patient_id, followup_date, department):
  - followup_date: exactly 7 days from today in YYYY-MM-DD format.
  - department: from the patient context provided.

Report: appointment ID, date, time (10:00 AM), department, location (Main Hospital Outpatient Block).
Only proceed if all clearances passed. Otherwise state why booking cannot proceed.
""",
    tools=[_make_mcp_toolset()],
    before_agent_callback=security_checkpoint,
)

notification_agent = LlmAgent(
    name="NotificationAgent",
    model=config.model,
    description="Sends discharge notifications to the patient and hospital staff.",
    instruction="""
You are the NotificationAgent. Call send_discharge_notification twice:

1. recipient_type='patient': warm tone, include discharge approval, medications, follow-up date.
2. recipient_type='staff': professional tone, patient discharged, room cleared, follow-up in records.

Report both delivery confirmations.
""",
    tools=[_make_mcp_toolset()],
    before_agent_callback=security_checkpoint,
)


# ─────────────────────────────────────────────────────────────────────────────
# ROOT ORCHESTRATOR (ADK entry point)
# ─────────────────────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    name="DischargeCoordinatorAgent",
    model=config.model,
    description="Master coordinator for hospital patient discharge process.",
    instruction="""
You are the DischargeCoordinatorAgent — master coordinator for the hospital discharge pipeline.

When asked to discharge a patient:

STEP 1 — Clearance:
  Delegate to ClearanceAgent with the patient_id.
  If it reports ANY blockage → stop and tell the user what is blocking. Do NOT continue.

STEP 2 — Discharge Summary:
  Only if Step 1 passed. Call build_discharge_summary with:
    - patient_id, patient_name, condition, lab_details from the clearance result
    - medications: comma-separated medication names from clearance result
    - followup_department: department name from clearance result
  Display the returned summary to the user.

STEP 3 — Follow-Up Booking:
  Only if Steps 1-2 passed. Delegate to FollowUpBookingAgent with the patient_id.

STEP 4 — Notifications:
  Only if all previous steps passed. Delegate to NotificationAgent.

FINAL:
  ✅ All steps done → "Patient [ID] successfully discharged!"
  ❌ Any blockage → explain what is needed to resolve it.

Rules:
- Sequential steps — never skip ahead.
- Be professional and compassionate.
- Demo patient IDs: P001 (ready), P002 (has blocks), P003 (ready).
""",
    tools=[
        AgentTool(agent=clearance_agent),
        discharge_summary_tool,
        AgentTool(agent=followup_booking_agent),
        AgentTool(agent=notification_agent),
    ],
    before_agent_callback=security_checkpoint,
)
