"""Patient records API: identities, medical history, and repeat admissions."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .database import connect, init_db, row_dict

router = APIRouter(prefix="/api", tags=["patients"])
init_db()


class PatientCreate(BaseModel):
    external_id: str | None = None
    full_name: str = Field(min_length=1, max_length=200)
    date_of_birth: str | None = None
    sex: str | None = None
    phone: str | None = None
    allergies: str | None = None


class HistoryCreate(BaseModel):
    condition: str = Field(min_length=1, max_length=200)
    details: str | None = None


class AdmissionCreate(BaseModel):
    reason: str | None = None
    triage_level: int | None = Field(None, ge=1, le=5)
    department: str = "Emergency Department"
    notes: str | None = None


class DischargeUpdate(BaseModel):
    outcome: str | None = None
    notes: str | None = None


@router.post("/patients", status_code=201)
def create_patient(payload: PatientCreate) -> dict[str, Any]:
    try:
        with connect() as conn:
            cur = conn.execute(
                """INSERT INTO patients
                   (external_id, full_name, date_of_birth, sex, phone, allergies)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (payload.external_id, payload.full_name, payload.date_of_birth,
                 payload.sex, payload.phone, payload.allergies),
            )
            patient = conn.execute("SELECT * FROM patients WHERE id = ?", (cur.lastrowid,)).fetchone()
            return {"patient": row_dict(patient)}
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(409, "external_id already exists") from exc
        raise


@router.get("/patients")
def list_patients() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM patients ORDER BY created_at DESC, id DESC").fetchall()
        return {"patients": [dict(row) for row in rows]}


@router.get("/patients/{patient_id}")
def get_patient(patient_id: int) -> dict[str, Any]:
    with connect() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if patient is None:
            raise HTTPException(404, "Patient not found")
        history = conn.execute(
            "SELECT * FROM medical_history WHERE patient_id = ? ORDER BY recorded_at DESC, id DESC",
            (patient_id,),
        ).fetchall()
        admissions = conn.execute(
            "SELECT * FROM admissions WHERE patient_id = ? ORDER BY admission_time DESC, id DESC",
            (patient_id,),
        ).fetchall()
        return {"patient": dict(patient), "medical_history": [dict(r) for r in history],
                "admissions": [dict(r) for r in admissions]}


@router.post("/patients/{patient_id}/history", status_code=201)
def add_history(patient_id: int, payload: HistoryCreate) -> dict[str, Any]:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(404, "Patient not found")
        cur = conn.execute(
            "INSERT INTO medical_history (patient_id, condition, details) VALUES (?, ?, ?)",
            (patient_id, payload.condition, payload.details),
        )
        row = conn.execute("SELECT * FROM medical_history WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"history": dict(row)}


@router.post("/patients/{patient_id}/admissions", status_code=201)
def create_admission(patient_id: int, payload: AdmissionCreate) -> dict[str, Any]:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(404, "Patient not found")
        cur = conn.execute(
            """INSERT INTO admissions
               (patient_id, reason, triage_level, department, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, payload.reason, payload.triage_level, payload.department, payload.notes),
        )
        row = conn.execute("SELECT * FROM admissions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"admission": dict(row)}


@router.patch("/admissions/{admission_id}/discharge")
def discharge(admission_id: int, payload: DischargeUpdate) -> dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE admissions SET discharge_time = CURRENT_TIMESTAMP,
               outcome = ?, notes = COALESCE(?, notes) WHERE id = ?""",
            (payload.outcome, payload.notes, admission_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Admission not found")
        row = conn.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone()
        return {"admission": dict(row)}
