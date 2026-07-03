"""
Hospital Discharge MCP Server
================================
Exposes 5 domain-specific tools over stdio transport for use by
ClinicalClearanceAgent and AdminClearanceAgent (and others).

Tools:
  1. get_doctor_approval      — clinical approval status
  2. get_lab_status           — pending lab report check
  3. get_billing_status       — outstanding billing check
  4. get_prescription_status  — pharmacy readiness check
  5. book_followup_appointment — schedule post-discharge follow-up
  6. send_discharge_notification — notify patient / staff
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("HospitalDischargeMCPServer")

# ── Simulated patient data store ─────────────────────────────────────────────
# In production this would call real EHR / hospital APIs.

_PATIENTS: dict[str, dict] = {
    "P001": {
        "name": "Rajesh Kumar",
        "condition": "Post-cardiac procedure",
        "department": "Cardiology",
        "doctor_approval": "Approved",
        "lab_status": "Completed",
        "lab_details": "CBC normal, Troponin negative, ECG stable",
        "billing_status": "Paid",
        "billing_balance": 0,
        "prescription_status": "Ready",
        "prescriptions": ["Aspirin 75mg", "Atorvastatin 40mg", "Metoprolol 25mg"],
    },
    "P002": {
        "name": "Priya Sharma",
        "condition": "Post-appendectomy recovery",
        "department": "General Surgery",
        "doctor_approval": "Pending",
        "lab_status": "Pending",
        "lab_details": "CBC pending culture results",
        "billing_status": "Unpaid",
        "billing_balance": 45000,
        "prescription_status": "Pending",
        "prescriptions": ["Amoxicillin 500mg", "Paracetamol 650mg"],
    },
    "P003": {
        "name": "Mohammed Al-Hassan",
        "condition": "Diabetic management",
        "department": "Endocrinology",
        "doctor_approval": "Approved",
        "lab_status": "Completed",
        "lab_details": "HbA1c 7.2%, Fasting glucose 110 mg/dL",
        "billing_status": "Paid",
        "billing_balance": 0,
        "prescription_status": "Ready",
        "prescriptions": ["Metformin 500mg", "Insulin Glargine 10 units"],
    },
}


def _get_patient(patient_id: str) -> dict:
    """Retrieve patient record or return a default 'not found' record."""
    return _PATIENTS.get(
        patient_id,
        {
            "name": "Unknown",
            "condition": "Unknown",
            "department": "General Medicine",
            "doctor_approval": "Pending",
            "lab_status": "Pending",
            "lab_details": "No records found",
            "billing_status": "Unpaid",
            "billing_balance": 0,
            "prescription_status": "Pending",
            "prescriptions": [],
        },
    )


# ── Tool 1: Doctor Approval ───────────────────────────────────────────────────

@mcp.tool()
def get_doctor_approval(patient_id: str) -> str:
    """
    Check whether the attending physician has approved the patient for discharge.

    Args:
        patient_id: The hospital patient ID (e.g. P001).

    Returns:
        JSON string with approval status and doctor's note.
    """
    patient = _get_patient(patient_id)
    status = patient["doctor_approval"]
    result = {
        "patient_id": patient_id,
        "patient_name": patient["name"],
        "doctor_approval": status,
        "note": (
            "Physician has reviewed and signed off on discharge."
            if status == "Approved"
            else "Discharge not yet approved. Physician review pending."
        ),
    }
    return json.dumps(result)


# ── Tool 2: Lab Status ────────────────────────────────────────────────────────

@mcp.tool()
def get_lab_status(patient_id: str) -> str:
    """
    Check the status of all pending laboratory reports for the patient.

    Args:
        patient_id: The hospital patient ID.

    Returns:
        JSON string with lab status and report details.
    """
    patient = _get_patient(patient_id)
    status = patient["lab_status"]
    result = {
        "patient_id": patient_id,
        "lab_status": status,
        "details": patient["lab_details"],
        "note": (
            "All lab reports finalized."
            if status == "Completed"
            else "Lab results still pending — discharge hold in place."
        ),
    }
    return json.dumps(result)


# ── Tool 3: Billing Status ────────────────────────────────────────────────────

@mcp.tool()
def get_billing_status(patient_id: str) -> str:
    """
    Retrieve the current billing/payment status for the patient.

    Args:
        patient_id: The hospital patient ID.

    Returns:
        JSON string with payment status and outstanding balance.
    """
    patient = _get_patient(patient_id)
    status = patient["billing_status"]
    balance = patient["billing_balance"]
    result = {
        "patient_id": patient_id,
        "billing_status": status,
        "outstanding_balance_inr": balance,
        "note": (
            "Account settled. Clearance granted."
            if status == "Paid"
            else f"Outstanding balance of ₹{balance:,}. Payment required before discharge."
        ),
    }
    return json.dumps(result)


# ── Tool 4: Prescription Status ───────────────────────────────────────────────

@mcp.tool()
def get_prescription_status(patient_id: str) -> str:
    """
    Check if the pharmacy has dispensed all prescribed medications.

    Args:
        patient_id: The hospital patient ID.

    Returns:
        JSON string with pharmacy readiness and medication list.
    """
    patient = _get_patient(patient_id)
    status = patient["prescription_status"]
    result = {
        "patient_id": patient_id,
        "prescription_status": status,
        "medications": patient["prescriptions"],
        "note": (
            "All medications dispensed and ready for patient collection."
            if status == "Ready"
            else "Prescriptions still being prepared by pharmacy."
        ),
    }
    return json.dumps(result)


# ── Tool 5: Book Follow-up Appointment ───────────────────────────────────────

@mcp.tool()
def book_followup_appointment(
    patient_id: str,
    followup_date: str = "",
    department: str = "",
) -> str:
    """
    Book a post-discharge follow-up appointment for the patient.

    Args:
        patient_id: The hospital patient ID.
        followup_date: Target date in YYYY-MM-DD format. Defaults to 7 days from today.
        department: Hospital department for follow-up. Defaults to patient's primary dept.

    Returns:
        JSON string with appointment confirmation details.
    """
    patient = _get_patient(patient_id)

    if not followup_date:
        followup_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    if not department:
        department = patient.get("department", "General Medicine")

    appointment_id = f"APT-{patient_id}-{random.randint(1000, 9999)}"

    result = {
        "patient_id": patient_id,
        "patient_name": patient["name"],
        "appointment_id": appointment_id,
        "followup_date": followup_date,
        "department": department,
        "time_slot": "10:00 AM",
        "status": "Confirmed",
        "note": f"Follow-up appointment booked at {department} on {followup_date} at 10:00 AM.",
    }
    return json.dumps(result)


# ── Tool 6: Send Discharge Notification ──────────────────────────────────────

@mcp.tool()
def send_discharge_notification(
    patient_id: str,
    recipient_type: str,
    message: str,
) -> str:
    """
    Send a discharge notification to patient or hospital staff.

    Args:
        patient_id: The hospital patient ID.
        recipient_type: Either 'patient' or 'staff'.
        message: The notification message to send.

    Returns:
        JSON string with delivery confirmation.
    """
    patient = _get_patient(patient_id)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Simulate delivery channel
    channel = "SMS + Email" if recipient_type == "patient" else "Internal Hospital System"

    result = {
        "patient_id": patient_id,
        "recipient_type": recipient_type,
        "recipient_name": patient["name"] if recipient_type == "patient" else "Hospital Staff",
        "channel": channel,
        "message_preview": message[:200] + ("..." if len(message) > 200 else ""),
        "status": "Delivered",
        "timestamp": timestamp,
    }
    return json.dumps(result)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
