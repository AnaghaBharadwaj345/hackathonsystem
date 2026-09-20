"""
Engine tests.

The interesting ones are not the golden values but the invariants: capacity is
never exceeded, resources are never double-booked, and the deadline tier really
does bound how long anyone waits.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from app.sim import (DEFAULT_CAPACITY, RES_KEYS, Sim, compare_policies,
                     mulberry32, what_if)

POLICIES = ("fifo", "triage", "medflow")


# --------------------------------------------------------------------- rng
def test_mulberry32_matches_reference_stream():
    """First four draws for seed 1, taken from the reference implementation."""
    rng = mulberry32(1)
    got = [round(rng(), 12) for _ in range(4)]
    assert got == [round(v, 12) for v in
                   (0.6270739405881613, 0.002735721180215478,
                    0.5274470399599522, 0.9810509674716741)]


def test_streams_are_independent():
    """Two policies must see the identical patient sequence."""
    a = Sim(999, "fifo", 100).run_to_end()
    b = Sim(999, "medflow", 100).run_to_end()
    assert a.stats["arrived"] == b.stats["arrived"]


# -------------------------------------------------------------- invariants
@pytest.mark.parametrize("policy", POLICIES)
def test_capacity_is_never_exceeded(policy):
    s = Sim(31337, policy, 160)
    while s.t < s.horizon:
        s.step()
        for k in RES_KEYS:
            assert len(s.units[k]) == s.capacity[k]
            assert s.free(k) >= 0


@pytest.mark.parametrize("policy", POLICIES)
def test_no_resource_is_double_booked(policy):
    """Every occupied unit maps to exactly one patient who claims to hold it."""
    s = Sim(4207, policy, 140)
    for _ in range(600):
        s.step()
    for k in RES_KEYS:
        for idx, holder in enumerate(s.units[k]):
            if holder is None or k == "amb":
                continue
            owned = [h for h in holder.holds if h["type"] == k and h["idx"] == idx]
            assert len(owned) == 1, f"{k}[{idx}] not owned exactly once"


@pytest.mark.parametrize("policy", POLICIES)
def test_every_patient_is_accounted_for(policy):
    """
    Nobody vanishes. A patient counts as arrived when they reach the department,
    and from there the only exits are being treated or walking out. Anyone still
    en route by ambulance has not arrived yet.
    """
    s = Sim(777, policy, 120).run_to_end()
    assert s.stats["served"] + s.stats["lwbs"] + len(s.queue) == s.stats["arrived"]
    assert all(p.esi in (1, 2, 3, 4, 5) for p in s.queue)


def test_overdue_patients_outrank_newer_arrivals():
    """
    The starvation guarantee, tested directly on the ordering rule rather than
    statistically. Once a patient is past their deadline, no later arrival of
    equal or lower acuity can overtake them, and their rank only rises with
    every further minute of waiting.
    """
    s = Sim(1, "medflow", 100)

    def probe(esi: int, wait: int) -> float:
        p = s.make_patient()
        p.esi, p.wait, p.risk = esi, wait, 0.0
        return s.score(p)

    overdue_l5 = probe(5, 130)          # deadline 95, so 35 minutes late
    fresh_l3 = probe(3, 5)
    fresh_l4 = probe(4, 20)
    assert overdue_l5 > fresh_l3
    assert overdue_l5 > fresh_l4
    assert probe(5, 200) > probe(5, 130)          # lateness keeps accruing

    # Inside the overdue tier the ordering is most-overdue-first, which is what
    # bounds the worst wait. Acuity only breaks exact ties: a level 3 and a
    # level 5 who are both 100 minutes past their own deadline are separated by
    # acuity, but a level 5 who is later than a level 3 still goes first.
    assert probe(3, 100 + 100) > probe(5, 95 + 100)
    assert probe(5, 95 + 200) > probe(3, 100 + 100)

    # but nothing overtakes a resuscitation or emergent case
    assert probe(1, 0) > probe(5, 10_000)
    assert probe(2, 0) > probe(3, 10_000)


def test_strict_triage_starves_low_acuity_but_medflow_does_not():
    """Same rule, applied to the baseline: strict triage has no deadline tier."""
    t = Sim(1, "triage", 100)
    m = Sim(1, "medflow", 100)

    def probe(sim, esi, wait):
        p = sim.make_patient()
        p.esi, p.wait, p.risk = esi, wait, 0.0
        return sim.score(p)

    # a level 5 who has waited four hours against a level 3 who just walked in
    assert probe(t, 5, 240) < probe(t, 3, 0)
    assert probe(m, 5, 240) > probe(m, 3, 0)


def _totals(policy, seeds, load=100):
    out = {"walked_out": 0, "adverse_events": 0, "seen": 0}
    for seed in seeds:
        m = Sim(seed, policy, load).run_to_end().metrics()
        for k in out:
            out[k] += m[k]
    return out


def test_medflow_beats_strict_triage_on_walkouts_and_throughput():
    """
    The defensible headline claim. MEDFLOW treats more people and abandons far
    fewer of them than strict triage. It does not beat strict triage on adverse
    events -- the two are level there, which is what the next test pins down.
    """
    seeds = range(500, 500 + 10)
    med, tri = _totals("medflow", seeds), _totals("triage", seeds)
    assert med["walked_out"] < tri["walked_out"] * 0.6
    assert med["seen"] > tri["seen"]


def test_urgency_aware_policies_prevent_harm_that_fifo_does_not():
    """
    Where both urgency-aware policies win decisively is against first-come
    ordering: an order of magnitude fewer adverse events.
    """
    seeds = range(500, 500 + 10)
    fifo = _totals("fifo", seeds)
    for policy in ("triage", "medflow"):
        assert _totals(policy, seeds)["adverse_events"] < fifo["adverse_events"] * 0.2


def test_fifo_is_bad_for_critical_patients():
    """Sanity check on the baseline: ignoring urgency should hurt level 1."""
    fifo = Sim(4207, "fifo", 100).run_to_end().avg_wait_esi(1)
    med = Sim(4207, "medflow", 100).run_to_end().avg_wait_esi(1)
    assert fifo > med


# ------------------------------------------------------------ determinism
@pytest.mark.parametrize("policy", POLICIES)
def test_runs_are_reproducible(policy):
    a = Sim(2024, policy, 110).run_to_end().metrics()
    b = Sim(2024, policy, 110).run_to_end().metrics()
    assert a == b


GOLDEN = {
    "fifo": (238, 238, 71.0, 26.8, 72, 3, 0),
    "triage": (238, 228, 70.6, 34.4, 153, 3, 10),
    "medflow": (238, 236, 61.3, 39.5, 127, 0, 2),
}


@pytest.mark.parametrize("policy,expected", GOLDEN.items())
def test_golden_values(policy, expected):
    """Regression guard. If you tune the model, update these deliberately."""
    m = Sim(4207, policy, 100).run_to_end().metrics()
    got = (m["arrived"], m["seen"], m["seen_on_time_pct"], m["avg_wait_min"],
           m["p95_wait_min"], m["adverse_events"], m["walked_out"])
    assert got == expected


# ------------------------------------------------------------- batch APIs
def test_compare_policies_shape():
    out = compare_policies(seed=4207, load_pct=100, replications=2)
    assert {r["policy"] for r in out["rows"]} == set(POLICIES)
    assert out["replications"] == 2
    for row in out["rows"]:
        assert row["seen"] > 0


def test_what_if_ranks_capacity_options():
    out = what_if(seed=4207, load_pct=150, replications=2,
                  deltas=[{"resource": "icu", "delta": 2},
                          {"resource": "doctor", "delta": 2}])
    assert len(out["options"]) == 2
    assert out["baseline_capacity"]["icu"] == DEFAULT_CAPACITY["icu"]
    for opt in out["options"]:
        assert "change" in opt


def test_capacity_override_is_applied():
    s = Sim(1, "medflow", 100, capacity={"icu": 12, "doctor": 20})
    assert len(s.units["icu"]) == 12
    assert len(s.units["doctor"]) == 20


def test_csv_export_has_a_row_per_departed_patient():
    s = Sim(4207, "medflow", 100).run_to_end()
    lines = [ln for ln in s.patients_csv().splitlines() if ln.strip()]
    assert len(lines) - 1 == len(s.records)
    assert lines[0].startswith("patient_id,name,triage_level")


# -------------------------------------------- parity with the browser build
JS_BUILD = os.environ.get("MEDFLOW_HTML", "")


@pytest.mark.skipif(not shutil.which("node") or not os.path.exists(JS_BUILD),
                    reason="node or the frontend build is not available")
def test_python_engine_matches_the_javascript_engine():
    """
    The Python port is bit-exact against the browser engine, so a run
    demonstrated in the UI can be reproduced server side and vice versa.
    Set MEDFLOW_HTML to the path of medflow.html to enable this test.
    """
    src = open(JS_BUILD).read()
    core = src[src.index("/* ============================ RNG"):
               src.index("/* ============================ UI")]
    harness = """
    const out=[];
    for(const pol of ['fifo','triage','medflow'])
      for(const seed of [4207, 91]){
        const s=new Sim(seed,pol,120); while(s.t<HORIZON) s.step();
        out.push({seed,pol,seen:s.stats.served,lwbs:s.stats.lwbs,p95:s.p95()});
      }
    console.log(JSON.stringify(out));
    """
    proc = subprocess.run(["node", "-e", core + harness],
                          capture_output=True, text=True, check=True)
    for row in json.loads(proc.stdout):
        s = Sim(row["seed"], row["pol"], 120).run_to_end()
        assert (s.stats["served"], s.stats["lwbs"], s.p95()) == \
               (row["seen"], row["lwbs"], row["p95"])
