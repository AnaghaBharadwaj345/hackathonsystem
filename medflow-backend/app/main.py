"""Request and response models for the MEDFLOW API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .sim import DEFAULT_CAPACITY, HORIZON, POLICIES, RES_KEYS


class CapacityIn(BaseModel):
    """Override any subset of the department's resources."""

    icu: Optional[int] = Field(None, ge=0, le=200)
    ward: Optional[int] = Field(None, ge=0, le=500)
    or_: Optional[int] = Field(None, ge=0, le=100, alias="or")
    doctor: Optional[int] = Field(None, ge=0, le=300)
    nurse: Optional[int] = Field(None, ge=0, le=600)
    amb: Optional[int] = Field(None, ge=0, le=100)

    model_config = {"populate_by_name": True}

    def to_dict(self) -> Dict[str, int]:
        raw = self.model_dump(by_alias=True, exclude_none=True)
        return {k: v for k, v in raw.items() if k in RES_KEYS}


class RunCreate(BaseModel):
    seed: int = Field(4207, ge=1, le=2_147_483_647)
    policy: str = "medflow"
    load_pct: int = Field(100, ge=10, le=400, description="Arrival volume as a percentage of the baseline day")
    capacity: Optional[CapacityIn] = None
    horizon_min: int = Field(HORIZON, ge=60, le=10080)

    @field_validator("policy")
    @classmethod
    def _policy(cls, v: str) -> str:
        if v not in POLICIES:
            raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
        return v


class AdvanceIn(BaseModel):
    minutes: int = Field(60, ge=1, le=10080)


class SurgeIn(BaseModel):
    count: int = Field(8, ge=1, le=60, description="Number of casualties to inject now")


class PatientCreate(BaseModel):
    patient_id: Optional[str] = Field(None, min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    date_of_birth: Optional[str] = Field(None, max_length=30)
    sex: Optional[str] = Field(None, max_length=30)
    phone: Optional[str] = Field(None, max_length=40)
    allergies: Optional[str] = Field(None, max_length=2000)
    chronic_conditions: Optional[str] = Field(None, max_length=2000)


class HistoryCreate(BaseModel):
    condition: str = Field(..., min_length=1, max_length=500)
    notes: Optional[str] = Field(None, max_length=5000)


class AdmissionCreate(BaseModel):
    patient_id: str = Field(..., min_length=1, max_length=100)
    run_id: Optional[str] = Field(None, max_length=100)
    admitted_at: Optional[str] = None
    discharged_at: Optional[str] = None
    triage_level: Optional[int] = Field(None, ge=1, le=5)
    chief_complaint: Optional[str] = Field(None, max_length=2000)
    diagnosis: Optional[str] = Field(None, max_length=2000)
    treatment: Optional[str] = Field(None, max_length=5000)
    outcome: Optional[str] = Field(None, max_length=500)
    wait_min: Optional[int] = Field(None, ge=0)
    treatment_min: Optional[int] = Field(None, ge=0)


class DischargeUpdate(BaseModel):
    outcome: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=5000)


class BenchmarkIn(BaseModel):
    seed: int = Field(4207, ge=1, le=2_147_483_647)
    load_pct: int = Field(100, ge=10, le=400)
    policies: Optional[List[str]] = None
    replications: int = Field(1, ge=1, le=25, description="Distinct seeds per policy")
    capacity: Optional[CapacityIn] = None

    @field_validator("policies")
    @classmethod
    def _policies(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = [p for p in v if p not in POLICIES]
        if bad:
            raise ValueError(f"unknown policies: {', '.join(bad)}")
        return v


class DeltaIn(BaseModel):
    resource: str
    delta: int = Field(1, ge=-50, le=50)

    @field_validator("resource")
    @classmethod
    def _res(cls, v: str) -> str:
        if v not in RES_KEYS:
            raise ValueError(f"resource must be one of {', '.join(RES_KEYS)}")
        return v


class WhatIfIn(BaseModel):
    seed: int = Field(4207, ge=1, le=2_147_483_647)
    load_pct: int = Field(100, ge=10, le=400)
    policy: str = "medflow"
    replications: int = Field(3, ge=1, le=15)
    deltas: Optional[List[DeltaIn]] = None
    capacity: Optional[CapacityIn] = None

    @field_validator("policy")
    @classmethod
    def _policy(cls, v: str) -> str:
        if v not in POLICIES:
            raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
        return v


class RunSummary(BaseModel):
    run_id: str
    seed: int
    policy: str
    load_pct: int
    t: int
    clock: str
    horizon_min: int
    finished: bool
    created_at: float
    capacity: Dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_CAPACITY))


class Snapshot(BaseModel):
    run_id: str
    snapshot: Dict[str, Any]
