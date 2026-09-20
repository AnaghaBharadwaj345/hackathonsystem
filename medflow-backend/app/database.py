"""SQLite persistence for patient identities, medical history, and admissions."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "medflow.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                full_name TEXT NOT NULL,
                date_of_birth TEXT,
                sex TEXT,
                phone TEXT,
                allergies TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS medical_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                condition TEXT NOT NULL,
                details TEXT,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                admission_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                discharge_time TEXT,
                reason TEXT,
                triage_level INTEGER,
                department TEXT DEFAULT 'Emergency Department',
                outcome TEXT,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_patient ON medical_history(patient_id);
            CREATE INDEX IF NOT EXISTS idx_admissions_patient ON admissions(patient_id);
            """
        )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
