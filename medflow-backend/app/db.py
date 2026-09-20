"""SQLite persistence for patient demographics, medical history, and admissions."""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.getenv("MEDFLOW_DB_PATH", "medflow.db")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                patient_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                date_of_birth TEXT,
                sex TEXT,
                phone TEXT,
                allergies TEXT,
                chronic_conditions TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS medical_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
                condition TEXT NOT NULL,
                notes TEXT,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admissions (
                admission_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
                run_id TEXT,
                admitted_at TEXT NOT NULL,
                discharged_at TEXT,
                triage_level INTEGER,
                chief_complaint TEXT,
                diagnosis TEXT,
                treatment TEXT,
                outcome TEXT,
                wait_min INTEGER,
                treatment_min INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_admissions_patient ON admissions(patient_id);
            CREATE INDEX IF NOT EXISTS idx_history_patient ON medical_history(patient_id);
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_patient(data: Dict[str, Any]) -> Dict[str, Any]:
    patient_id = data.get("patient_id") or uuid.uuid4().hex
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO patients
            (patient_id,name,date_of_birth,sex,phone,allergies,chronic_conditions,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (patient_id, data["name"], data.get("date_of_birth"), data.get("sex"),
             data.get("phone"), data.get("allergies"), data.get("chronic_conditions"), _now()),
        )
    return get_patient(patient_id)  # type: ignore[return-value]


def get_patient(patient_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        if not row:
            return None
        patient = dict(row)
        patient["medical_history"] = [dict(r) for r in conn.execute(
            "SELECT * FROM medical_history WHERE patient_id = ? ORDER BY recorded_at DESC", (patient_id,)
        )]
        patient["admissions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM admissions WHERE patient_id = ? ORDER BY admitted_at DESC", (patient_id,)
        )]
        return patient


def list_patients(limit: int = 100) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM patients ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def add_history(patient_id: str, condition: str, notes: Optional[str] = None) -> Dict[str, Any]:
    with _lock, _connect() as conn:
        if not conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (patient_id,)).fetchone():
            raise KeyError(patient_id)
        conn.execute(
            "INSERT INTO medical_history(patient_id,condition,notes,recorded_at) VALUES (?,?,?,?)",
            (patient_id, condition, notes, _now()),
        )
    return get_patient(patient_id)  # type: ignore[return-value]


def add_admission(data: Dict[str, Any]) -> Dict[str, Any]:
    admission_id = data.get("admission_id") or uuid.uuid4().hex
    with _lock, _connect() as conn:
        if not conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (data["patient_id"],)).fetchone():
            raise KeyError(data["patient_id"])
        conn.execute(
            """INSERT INTO admissions
            (admission_id,patient_id,run_id,admitted_at,discharged_at,triage_level,
             chief_complaint,diagnosis,treatment,outcome,wait_min,treatment_min)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (admission_id, data["patient_id"], data.get("run_id"), data.get("admitted_at", _now()),
             data.get("discharged_at"), data.get("triage_level"), data.get("chief_complaint"),
             data.get("diagnosis"), data.get("treatment"), data.get("outcome"),
             data.get("wait_min"), data.get("treatment_min")),
        )
        row = conn.execute("SELECT * FROM admissions WHERE admission_id = ?", (admission_id,)).fetchone()
        return dict(row)
