from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_patient_crud_flow():
    patient = client.post(
        "/api/patients",
        json={
            "name": "Asha Rao",
            "date_of_birth": "1990-02-14",
            "sex": "female",
            "phone": "+91-99999-99999",
            "allergies": "Penicillin",
            "chronic_conditions": "Asthma",
        },
    )
    assert patient.status_code == 201, patient.text
    body = patient.json()
    patient_id = body["patient_id"]
    assert patient_id

    detail = client.get(f"/api/patients/{patient_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Asha Rao"

    history = client.post(
        f"/api/patients/{patient_id}/history",
        json={"condition": "Asthma", "notes": "Follow-up required"},
    )
    assert history.status_code == 200, history.text

    admission = client.post(
        f"/api/patients/{patient_id}/admissions",
        json={
            "triage_level": 2,
            "chief_complaint": "Shortness of breath",
            "diagnosis": "Asthma exacerbation",
            "treatment": "Nebulizer and monitoring",
            "outcome": "admitted",
            "wait_min": 15,
            "treatment_min": 120,
        },
    )
    assert admission.status_code == 201, admission.text
    admission_id = admission.json()["admission"]["admission_id"]

    discharge = client.patch(
        f"/api/admissions/{admission_id}/discharge",
        json={"outcome": "discharged", "notes": "Improved"},
    )
    assert discharge.status_code == 200, discharge.text
    assert discharge.json()["admission"]["outcome"] == "discharged"


def test_patient_list_and_missing_patient():
    res = client.get("/api/patients")
    assert res.status_code == 200
    assert "patients" in res.json()

    missing = client.get("/api/patients/does-not-exist")
    assert missing.status_code == 404


def test_patient_history_and_admission_validation():
    bad_history = client.post(
        "/api/patients/does-not-exist/history",
        json={"condition": "X"},
    )
    assert bad_history.status_code == 404

    bad_admission = client.post(
        "/api/patients/does-not-exist/admissions",
        json={"triage_level": 1},
    )
    assert bad_admission.status_code == 404


def test_db_can_create_patient_directly():
    from app.db import create_patient, get_patient

    patient = create_patient({"name": "Test User", "sex": "male"})
    assert patient["name"] == "Test User"
    assert get_patient(patient["patient_id"]) is not None

    # clean up a little
    from app.db import _connect

    with _connect() as conn:
        conn.execute("DELETE FROM patients WHERE patient_id = ?", (patient["patient_id"],))


__all__ = ["client"]


# if __name__ == "__main__":
#     import pytest
#     raise SystemExit(pytest.main(["-q", __file__]))
