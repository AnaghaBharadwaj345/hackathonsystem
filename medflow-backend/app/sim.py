"""
MEDFLOW simulation engine.

A bit-exact Python port of the browser engine. The random number generator,
the draw order and the arithmetic are reproduced faithfully, so a given
(seed, policy, load, capacity) produces the same run here as in the frontend.
Verified by tests/test_parity.py.

Nothing in this module imports FastAPI or touches the network. It is a plain
library and can be used from a script, a notebook or a worker queue.
"""
from __future__ import annotations

import csv
import io
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 32-bit helpers so the RNG matches JavaScript exactly
# --------------------------------------------------------------------------
MASK = 0xFFFFFFFF


def _u32(x: int) -> int:
    return x & MASK


def _i32(x: int) -> int:
    x &= MASK
    return x - 0x100000000 if x >= 0x80000000 else x


def _imul(a: int, b: int) -> int:
    """Equivalent of JavaScript's Math.imul."""
    return _i32((_i32(a) * _i32(b)) & MASK)


def mulberry32(seed: int) -> Callable[[], float]:
    """The same PRNG the frontend uses. Small, fast, fully deterministic."""
    a = _i32(seed)

    def rnd() -> float:
        nonlocal a
        a = _i32(a + 0x6D2B79F5)
        t = _imul(a ^ (_u32(a) >> 15), 1 | a)
        t = _i32(_i32(t + _imul(t ^ (_u32(t) >> 7), 61 | t)) ^ t)
        return _u32(t ^ (_u32(t) >> 14)) / 4294967296.0

    return rnd


def gauss(rng: Callable[[], float]) -> float:
    """Box-Muller, consuming two draws, matching the frontend."""
    u = 0.0
    v = 0.0
    while u == 0.0:
        u = rng()
    while v == 0.0:
        v = rng()
    return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)


def jround(x: float) -> int:
    """JavaScript Math.round: halves go up, not to even."""
    return math.floor(x + 0.5)


# --------------------------------------------------------------------------
# Model constants
# --------------------------------------------------------------------------
RESOURCES: List[Tuple[str, str, int]] = [
    ("icu", "ICU beds", 6),
    ("ward", "Ward beds", 22),
    ("or", "Operating rooms", 3),
    ("doctor", "Doctors on shift", 9),
    ("nurse", "Nurses on shift", 20),
    ("amb", "Ambulances", 4),
]
RES_KEYS: List[str] = [r[0] for r in RESOURCES]
RES_LABELS: Dict[str, str] = {k: label for k, label, _ in RESOURCES}
DEFAULT_CAPACITY: Dict[str, int] = {k: c for k, _, c in RESOURCES}

# Arrival rate multiplier by hour of day, starting at midnight.
HOURLY = [0.42, 0.36, 0.30, 0.28, 0.32, 0.42, 0.62, 0.88,
          1.12, 1.32, 1.38, 1.30, 1.22, 1.16, 1.16, 1.22,
          1.32, 1.48, 1.52, 1.40, 1.20, 0.96, 0.72, 0.52]

BASE_LAMBDA = 0.185          # arrivals per simulated minute at load 100%
HORIZON = 1440               # one day
DAY_START_MIN = 360          # the clock starts at 06:00

TARGET_WAIT = {1: 5, 2: 15, 3: 35, 4: 60, 5: 85}      # door-to-provider targets
ACUITY = {1: 1000, 2: 60, 3: 20, 4: 8, 5: 3}          # claim on scarce capacity
DEADLINE = {1: 15, 2: 45, 3: 100, 4: 120, 5: 95}      # hard "must be seen by"
MEAN_DUR = {1: 200, 2: 150, 3: 95, 4: 50, 5: 25}
LWBS_LIMIT = {4: 150, 5: 110}                          # walkout thresholds

POLICIES = ("fifo", "triage", "medflow")
POLICY_LABELS = {
    "fifo": "First come, first served",
    "triage": "Strict triage",
    "medflow": "MEDFLOW",
}

FIRST = ["Asha", "Ravi", "Meera", "Nikhil", "Priya", "Arjun", "Fatima", "Dev",
         "Sneha", "Imran", "Kavya", "Rohit", "Anita", "Yusuf", "Divya", "Karan",
         "Leela", "Sam", "Tara", "Vikram", "Noor", "Ishaan", "Rhea", "Manav",
         "Zoya", "Aditya"]
LAST = ["Rao", "Nair", "Shah", "Iyer", "Khan", "Bose", "Menon", "Patel",
        "Reddy", "Das", "Gill", "Joshi", "Verma", "Pillai", "Sethi", "Mathew"]

RES_DISPLAY = {"icu": "ICU", "ward": "bed", "or": "theatre",
               "doctor": "doctor", "nurse": "nurse"}


class Patient:
    __slots__ = ("id", "name", "esi", "esi0", "need", "dur", "arrive", "wait",
                 "risk", "holds", "phase", "stepdown", "by_amb", "blocked",
                 "served_at", "state", "score", "eta", "amb_free", "treat_end",
                 "step_end", "ward_idx", "boarded")

    def __init__(self) -> None:
        self.id = 0
        self.name = ""
        self.esi = 3
        self.esi0 = 3
        self.need: Dict[str, int] = {}
        self.dur = 0
        self.arrive = 0
        self.wait = 0
        self.risk = 0.0
        self.holds: List[Dict[str, Any]] = []
        self.phase = "waiting"
        self.stepdown = 0
        self.by_amb = False
        self.blocked = 0
        self.served_at: Optional[int] = None
        self.state = ""
        self.score: Optional[float] = None
        self.eta = 0
        self.amb_free = 0
        self.treat_end = 0
        self.step_end = 0
        self.ward_idx = False
        self.boarded = 0


class Sim:
    """One simulated emergency department over one simulated day."""

    def __init__(
        self,
        seed: int = 4207,
        policy: str = "medflow",
        load_pct: int = 100,
        capacity: Optional[Dict[str, int]] = None,
        horizon: int = HORIZON,
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r}, expected one of {POLICIES}")

        self.seed = int(seed)
        self.policy = policy
        self.load = load_pct / 100.0
        self.horizon = int(horizon)
        self.capacity = dict(DEFAULT_CAPACITY)
        if capacity:
            for k, v in capacity.items():
                if k in self.capacity:
                    self.capacity[k] = max(0, int(v))

        # Stream A drives arrivals and patient generation; stream B drives
        # everything that happens inside the hospital. Keeping them apart is
        # what lets two policies face an identical patient sequence.
        self.rng = mulberry32(self.seed)
        self.rng_b = mulberry32((self.seed * 7919 + 13) & MASK)

        self.t = 0
        self.next_id = 1
        self.units: Dict[str, List[Optional[Patient]]] = {
            k: [None] * self.capacity[k] for k in RES_KEYS
        }
        self.queue: List[Patient] = []
        self.inbound: List[Patient] = []
        self.amb_calls: List[Dict[str, Any]] = []
        self.active: List[Patient] = []
        self.log: List[Dict[str, Any]] = []
        self.history: List[Dict[str, float]] = []
        self.records: List[Dict[str, Any]] = []

        self.stats: Dict[str, Any] = {
            "arrived": 0, "served": 0, "lwbs": 0, "deterioration": 0,
            "escalations": 0, "boarded": 0, "on_time": 0,
            "wait_sum": 0.0, "wait_n": 0, "waits": [],
            "wait_by_esi": {1: [], 2: [], 3: [], 4: [], 5: []},
            "util": {k: 0.0 for k in RES_KEYS}, "util_n": 0,
            "icu_blocked_min": 0, "amb_delay_min": 0, "conflicts": 0,
        }

    # ---------------------------------------------------------------- utils
    @staticmethod
    def clock(t: int) -> str:
        m = (t + DAY_START_MIN) % 1440
        return f"{m // 60:02d}:{m % 60:02d}"

    def add_log(self, msg: str, cls: str = "") -> None:
        self.log.append({"t": self.t, "clock": self.clock(self.t), "msg": msg, "kind": cls})
        if len(self.log) > 400:
            self.log.pop(0)

    def free(self, k: str) -> int:
        return self.units[k].count(None)

    def lam(self) -> float:
        hour = int(((self.t / 60.0) + 6) % 24)
        return BASE_LAMBDA * HOURLY[hour] * self.load

    # ------------------------------------------------------------ resources
    def resolve(self, p: Patient) -> Tuple[Dict[str, int], int]:
        """
        Overflow boarding. A critical patient who needs intensive care but finds
        no ICU bed is placed in a ward bed with the same staffing rather than
        left waiting in a corridor.
        """
        n = dict(p.need)
        boarded = 0
        if n.get("icu"):
            have = self.free("icu")
            if have < n["icu"]:
                boarded = n["icu"] - have
                if have > 0:
                    n["icu"] = have
                else:
                    del n["icu"]
                n["ward"] = n.get("ward", 0) + boarded
        return n, boarded

    def can_take(self, need: Dict[str, int]) -> bool:
        return all(self.free(k) >= v for k, v in need.items())

    def take(self, p: Patient, need: Dict[str, int], until: Dict[str, Optional[int]]) -> None:
        """
        All-or-nothing acquisition in a fixed global order. Because a patient
        never holds one resource while waiting for another, the Coffman
        hold-and-wait condition is never satisfied and deadlock cannot occur.
        """
        for k in RES_KEYS:
            if not need.get(k):
                continue
            for _ in range(need[k]):
                i = self.units[k].index(None)
                self.units[k][i] = p
                p.holds.append({"type": k, "idx": i, "until": until.get(k)})

    def release(self, p: Patient, rtype: str) -> None:
        keep = []
        for h in p.holds:
            if h["type"] == rtype:
                self.units[h["type"]][h["idx"]] = None
            else:
                keep.append(h)
        p.holds = keep

    def release_all(self, p: Patient) -> None:
        for h in p.holds:
            self.units[h["type"]][h["idx"]] = None
        p.holds = []

    # ------------------------------------------------------------- patients
    def make_patient(self) -> Patient:
        r = self.rng()
        if r < 0.05:
            esi = 1
        elif r < 0.20:
            esi = 2
        elif r < 0.55:
            esi = 3
        elif r < 0.85:
            esi = 4
        else:
            esi = 5

        need: Dict[str, int] = {}
        if esi == 1:
            need["icu"] = 1
            need["doctor"] = 1
            need["nurse"] = 2
        elif esi == 2:
            if self.rng() < 0.40:
                need["icu"] = 1
            else:
                need["ward"] = 1
            need["doctor"] = 1
            need["nurse"] = 1
        elif esi == 3:
            need["ward"] = 1
            need["doctor"] = 1
            need["nurse"] = 1
        elif esi == 4:
            need["ward"] = 1
            need["doctor"] = 1
            need["nurse"] = 1
        else:
            need["doctor"] = 1
            need["nurse"] = 1

        or_prob = {1: 0.45, 2: 0.25, 3: 0.08, 4: 0.02, 5: 0.0}[esi]
        if self.rng() < or_prob:
            need["or"] = 1

        dur = max(12, jround(MEAN_DUR[esi] * math.exp(0.34 * gauss(self.rng))))
        name = (FIRST[int(self.rng() * len(FIRST))] + " "
                + LAST[int(self.rng() * len(LAST))][0] + ".")

        p = Patient()
        p.id = self.next_id
        self.next_id += 1
        p.name = name
        p.esi = esi
        p.esi0 = esi
        p.need = need
        p.dur = dur
        p.arrive = self.t
        p.stepdown = (60 + jround(self.rng() * 120)) if need.get("icu") else 0
        return p

    def surge(self, n: Optional[int] = None) -> None:
        """Mass casualty incident: a burst of patients, skewed to high acuity."""
        if n is None:
            n = 5 + int(self.rng() * 7)
        for _ in range(n):
            p = self.make_patient()
            if p.esi > 2 and self.rng() < 0.6:
                p.esi = max(1, p.esi - 2)
            p.by_amb = True
            self.queue.append(p)
            self.stats["arrived"] += 1
        self.add_log(f"Mass casualty: {n} patients incoming", "alert")

    # -------------------------------------------------------------- scoring
    def score(self, p: Patient) -> float:
        if self.policy == "fifo":
            return 100000 - p.arrive
        if self.policy == "triage":
            return ACUITY[p.esi] * 1000 - p.arrive * 0.001

        # MEDFLOW sorts into three tiers.
        # 1. Resuscitation and emergent cases are never deferred for anyone.
        if p.esi == 1:
            return 1e9 + p.wait
        if p.esi == 2:
            return 1e8 + p.wait
        # 2. Past the hard deadline, most overdue first. This is the starvation
        #    guarantee: once late, nothing newer overtakes you, so the worst
        #    wait in the department is bounded rather than open-ended.
        dl = DEADLINE[p.esi]
        if p.wait > dl:
            return 1e6 + (p.wait - dl) * 10 + ACUITY[p.esi]
        # 3. Everyone else: acuity stretched by lateness against target and risk.
        return ACUITY[p.esi] * (1 + min(3.0, p.wait / TARGET_WAIT[p.esi])) * (1 + 0.3 * p.risk)

    # ----------------------------------------------------------------- tick
    def step(self) -> None:
        """Advance the simulation by one minute."""
        t = self.t

        # 1. arrivals
        if self.rng() < self.lam():
            p = self.make_patient()
            amb_prob = {1: 0.72, 2: 0.5, 3: 0.22, 4: 0.07, 5: 0.02}[p.esi]
            if self.rng() < amb_prob:
                p.by_amb = True
                self.amb_calls.append({"p": p, "called": t})
            else:
                self.queue.append(p)
                self.stats["arrived"] += 1
        if self.rng() < 0.00055 * self.load:
            self.surge()

        # 2. ambulance dispatch and arrival
        for i in range(len(self.amb_calls) - 1, -1, -1):
            c = self.amb_calls[i]
            cp: Patient = c["p"]
            if self.free("amb") > 0:
                idx = self.units["amb"].index(None)
                self.units["amb"][idx] = cp
                cp.eta = t + 14 + jround(self.rng() * 16)
                cp.amb_free = cp.eta + 12 + jround(self.rng() * 14)
                self.inbound.append(cp)
                self.amb_calls.pop(i)
            else:
                self.stats["amb_delay_min"] += 1
                cp.risk += 0.02 * (6 - cp.esi)

        for i in range(len(self.inbound) - 1, -1, -1):
            p = self.inbound[i]
            if t >= p.eta:
                p.arrive = t
                self.queue.append(p)
                self.stats["arrived"] += 1
                self.inbound.pop(i)

        for i, holder in enumerate(self.units["amb"]):
            if holder is not None and holder.amb_free <= t:
                self.units["amb"][i] = None

        # 3. waiting: aging, deterioration, walkouts
        for i in range(len(self.queue) - 1, -1, -1):
            p = self.queue[i]
            p.wait = t - p.arrive
            rate = {1: 0.055, 2: 0.03, 3: 0.012, 4: 0.004, 5: 0.001}[p.esi]
            p.risk += rate * (1 + p.wait / 90.0)

            if p.esi > 1 and self.rng_b() < rate * 0.055 * (p.wait / 30.0):
                p.esi -= 1
                self.stats["escalations"] += 1
                self.add_log(
                    f"#{p.id} {p.name} deteriorated to level {p.esi} after {p.wait} min",
                    "alert")

            if p.esi <= 2 and p.wait > {1: 20, 2: 55}[p.esi] and self.rng_b() < 0.006:
                self.stats["deterioration"] += 1
                self.add_log(
                    f"Adverse event: #{p.id} level {p.esi} waited {p.wait} min", "alert")
                p.risk += 4

            lim = LWBS_LIMIT.get(p.esi)
            if lim and p.wait > lim and self.rng_b() < 0.02:
                self.stats["lwbs"] += 1
                self.queue.pop(i)
                # they still waited, so their wait belongs in the statistics
                self.stats["wait_sum"] += p.wait
                self.stats["wait_n"] += 1
                self.stats["waits"].append(p.wait)
                self.stats["wait_by_esi"][p.esi].append(p.wait)
                self.records.append(self._record(p, outcome="left_without_being_seen"))
                self.add_log(f"#{p.id} left without being seen ({p.wait} min)")

        # 4. progress patients already in care
        for i in range(len(self.active) - 1, -1, -1):
            p = self.active[i]
            keep = []
            for h in p.holds:
                if (h["until"] is not None and h["until"] <= t
                        and h["type"] not in ("icu", "ward")):
                    self.units[h["type"]][h["idx"]] = None
                else:
                    keep.append(h)
            p.holds = keep

            if p.phase == "treating" and t >= p.treat_end:
                if any(h["type"] == "icu" for h in p.holds) and p.stepdown > 0:
                    p.phase = "stepdown"
                else:
                    self.release_all(p)
                    p.phase = "done"
                    self.active.pop(i)
                    self.records.append(self._record(p, outcome="discharged", discharged=t))
                    self.add_log(f"#{p.id} discharged", "good")

            elif p.phase == "stepdown":
                if not p.ward_idx:
                    if self.free("ward") > 0:
                        idx = self.units["ward"].index(None)
                        self.units["ward"][idx] = p
                        p.ward_idx = True
                        p.holds.append({"type": "ward", "idx": idx, "until": None})
                        self.release(p, "icu")
                        p.step_end = t + p.stepdown
                        if p.blocked > 0:
                            self.add_log(
                                f"#{p.id} moved out of ICU after {p.blocked} min blocked")
                    else:
                        p.blocked += 1
                        self.stats["icu_blocked_min"] += 1
                elif t >= p.step_end:
                    self.release_all(p)
                    p.phase = "done"
                    self.active.pop(i)
                    self.records.append(self._record(p, outcome="discharged", discharged=t))
                    self.add_log(f"#{p.id} discharged", "good")

        # 5. allocate
        self.allocate()

        # 6. bookkeeping
        for k in RES_KEYS:
            cap = self.capacity[k]
            if cap:
                self.stats["util"][k] += (cap - self.free(k)) / cap
        self.stats["util_n"] += 1

        if t % 10 == 0:
            avg = (sum(p.wait for p in self.queue) / len(self.queue)) if self.queue else 0.0
            self.history.append({"t": t, "queue": len(self.queue), "avg_wait": avg})
            if len(self.history) > 300:
                self.history.pop(0)

        self.t += 1

    # ------------------------------------------------------------ scheduler
    def allocate(self) -> None:
        t = self.t
        for p in self.queue:
            p.wait = t - p.arrive
            p.score = self.score(p)
            p.state = ""
        self.queue.sort(key=lambda q: -q.score)

        # For each resource type, how long until the nth unit frees up?
        remaining: Dict[str, List[int]] = {}
        for k in RES_KEYS:
            ends: List[int] = []
            for p in self.active:
                for h in p.holds:
                    if h["type"] != k:
                        continue
                    if h["until"] is not None:
                        end = h["until"]
                    elif p.phase == "stepdown":
                        end = p.step_end or (t + 30)
                    else:
                        end = p.treat_end
                    ends.append(max(0, end - t))
            ends.sort()
            remaining[k] = ends

        def window_for(k: str, deficit: int) -> float:
            e = remaining[k]
            return e[deficit - 1] if len(e) >= deficit else math.inf

        reserved: Dict[str, int] = {}
        reserved_window: Dict[str, float] = {}
        blocked_heads = 0
        use_reservation = self.policy == "medflow"

        for p in list(self.queue):
            if p.holds:
                continue

            probe, _ = self.resolve(p)
            hits_reserved = False
            reserved_type = None
            for k, v in probe.items():
                if reserved.get(k) and self.free(k) - reserved[k] < v:
                    hits_reserved = True
                    reserved_type = k

            if hits_reserved:
                # Backfill: let this patient through only if they will be
                # finished and out before the reserved unit is actually needed,
                # so holding it back costs nothing.
                win = reserved_window.get(reserved_type, 0)
                if p.dur > win:
                    p.state = "backfill-blocked"
                    continue
                p.state = "backfill"

            eff, boarded = self.resolve(p)
            if self.can_take(eff):
                start = t
                until: Dict[str, Optional[int]] = {"ward": None, "icu": None}
                if eff.get("doctor"):
                    until["doctor"] = start + max(15, jround(p.dur * 0.45))
                if eff.get("or"):
                    until["or"] = start + min(p.dur, 35 + jround(p.dur * 0.35))
                if eff.get("nurse"):
                    until["nurse"] = start + max(15, jround(p.dur * 0.75))
                if not eff.get("ward") and not eff.get("icu"):
                    until["nurse"] = start + p.dur

                self.take(p, eff, until)
                if boarded:
                    p.boarded = boarded
                    self.stats["boarded"] += 1
                    self.add_log(f"#{p.id} boarded in a ward bed, ICU full", "alert")

                p.phase = "treating"
                p.treat_end = start + p.dur
                p.served_at = start
                p.wait = start - p.arrive

                self.stats["served"] += 1
                if p.wait <= TARGET_WAIT[p.esi]:
                    self.stats["on_time"] += 1
                self.stats["wait_sum"] += p.wait
                self.stats["wait_n"] += 1
                self.stats["waits"].append(p.wait)
                self.stats["wait_by_esi"][p.esi].append(p.wait)

                self.active.append(p)
                self.queue.remove(p)
                self.add_log(
                    f"#{p.id} {p.name} (L{p.esi}) started care after {p.wait} min")
            else:
                self.stats["conflicts"] += 1
                p.state = p.state or "held"
                # Only hold capacity back for patients who cannot afford to
                # wait. Reserving for a level 4 would cost throughput and buy
                # nothing.
                if use_reservation and blocked_heads < 1 and p.esi <= 2:
                    blocked_heads += 1
                    for k, v in probe.items():
                        deficit = v - self.free(k)
                        if deficit > 0:
                            reserved[k] = reserved.get(k, 0) + v
                            reserved_window[k] = window_for(k, deficit)
                    p.state = "held"

    # --------------------------------------------------------------- output
    def _record(self, p: Patient, outcome: str, discharged: Optional[int] = None) -> Dict[str, Any]:
        return {
            "patient_id": p.id,
            "name": p.name,
            "triage_level": p.esi,
            "triage_level_on_arrival": p.esi0,
            "arrived_min": p.arrive,
            "arrived_clock": self.clock(p.arrive),
            "wait_min": p.wait,
            "treatment_min": p.dur if outcome == "discharged" else 0,
            "discharged_min": discharged,
            "resources": "|".join(f"{k}x{v}" for k, v in p.need.items()),
            "by_ambulance": p.by_amb,
            "boarded_in_ward": bool(p.boarded),
            "icu_blocked_min": p.blocked,
            "outcome": outcome,
            "seen_within_target": outcome == "discharged" and p.wait <= TARGET_WAIT[p.esi],
        }

    def p95(self) -> int:
        w = self.stats["waits"]
        if not w:
            return 0
        a = sorted(w)
        return a[min(len(a) - 1, int(len(a) * 0.95))]

    def avg_wait_esi(self, esi: int) -> Optional[float]:
        a = self.stats["wait_by_esi"][esi]
        return (sum(a) / len(a)) if a else None

    def util_pct(self, k: str) -> float:
        n = self.stats["util_n"]
        return (self.stats["util"][k] / n * 100.0) if n else 0.0

    def compliance(self) -> float:
        resolved = self.stats["served"] + self.stats["lwbs"]
        return (self.stats["on_time"] / resolved * 100.0) if resolved else 0.0

    def avg_wait(self) -> float:
        n = self.stats["wait_n"]
        return (self.stats["wait_sum"] / n) if n else 0.0

    def metrics(self) -> Dict[str, Any]:
        """The headline numbers. This is what the comparison endpoint returns."""
        return {
            "arrived": self.stats["arrived"],
            "seen": self.stats["served"],
            "seen_on_time_pct": round(self.compliance(), 1),
            "avg_wait_min": round(self.avg_wait(), 1),
            "p95_wait_min": self.p95(),
            "wait_by_level": {
                str(e): (round(v, 1) if v is not None else None)
                for e in (1, 2, 3, 4, 5)
                for v in [self.avg_wait_esi(e)]
            },
            "adverse_events": self.stats["deterioration"],
            "deteriorated": self.stats["escalations"],
            "walked_out": self.stats["lwbs"],
            "boarded_in_ward": self.stats["boarded"],
            "icu_blocked_min": self.stats["icu_blocked_min"],
            "ambulance_delay_min": self.stats["amb_delay_min"],
            "utilisation_pct": {k: round(self.util_pct(k), 1) for k in RES_KEYS},
        }

    def snapshot(self, queue_limit: int = 25, log_limit: int = 40) -> Dict[str, Any]:
        """Everything a UI needs to draw one frame."""
        return {
            "run": {
                "seed": self.seed,
                "policy": self.policy,
                "load_pct": round(self.load * 100),
                "capacity": self.capacity,
                "horizon_min": self.horizon,
            },
            "t": self.t,
            "clock": self.clock(self.t),
            "day": (self.t + DAY_START_MIN) // 1440 + 1,
            "finished": self.t >= self.horizon,
            "resources": [
                {
                    "key": k,
                    "label": RES_LABELS[k],
                    "capacity": self.capacity[k],
                    "in_use": self.capacity[k] - self.free(k),
                    "avg_utilisation_pct": round(self.util_pct(k), 1),
                    "units": [
                        None if u is None else {
                            "patient_id": u.id,
                            "triage_level": u.esi,
                            "blocked": k == "icu" and u.phase == "stepdown",
                        }
                        for u in self.units[k]
                    ],
                }
                for k in RES_KEYS
            ],
            "waiting": [
                {
                    "patient_id": p.id,
                    "name": p.name,
                    "triage_level": p.esi,
                    "escalated": p.esi < p.esi0,
                    "by_ambulance": p.by_amb,
                    "wait_min": p.wait,
                    "needs": [
                        (f"{v} " if v > 1 else "") + RES_DISPLAY.get(k, k)
                        for k, v in p.need.items()
                    ],
                    "priority": None if p.score is None else round(p.score, 2),
                    "priority_tier": (
                        "never defer" if (p.score or 0) >= 1e8
                        else "overdue" if (p.score or 0) >= 1e6
                        else "scored"
                    ) if self.policy == "medflow" else "scored",
                    "state": p.state,
                }
                for p in self.queue[:queue_limit]
            ],
            "counts": {
                "waiting": len(self.queue),
                "in_care": len(self.active),
                "en_route": len(self.inbound),
                "awaiting_ambulance": len(self.amb_calls),
            },
            "metrics": self.metrics(),
            "history": self.history[-120:],
            "log": list(reversed(self.log[-log_limit:])),
        }

    def patients_csv(self) -> str:
        buf = io.StringIO()
        cols = ["patient_id", "name", "triage_level", "triage_level_on_arrival",
                "arrived_min", "arrived_clock", "wait_min", "treatment_min",
                "discharged_min", "resources", "by_ambulance", "boarded_in_ward",
                "icu_blocked_min", "outcome", "seen_within_target"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in self.records:
            w.writerow(r)
        return buf.getvalue()

    # ------------------------------------------------------------ execution
    def advance(self, minutes: int) -> int:
        """Run forward, stopping at the horizon. Returns minutes actually run."""
        ran = 0
        while ran < minutes and self.t < self.horizon:
            self.step()
            ran += 1
        return ran

    def run_to_end(self) -> "Sim":
        while self.t < self.horizon:
            self.step()
        return self


# --------------------------------------------------------------------------
# Batch helpers
# --------------------------------------------------------------------------
def run_once(seed: int, policy: str, load_pct: int,
             capacity: Optional[Dict[str, int]] = None,
             horizon: int = HORIZON) -> Sim:
    return Sim(seed, policy, load_pct, capacity, horizon).run_to_end()


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


BENCH_FIELDS = [
    ("seen", "max"), ("seen_on_time_pct", "max"), ("avg_wait_min", "min"),
    ("p95_wait_min", "min"), ("adverse_events", "min"), ("walked_out", "min"),
    ("deteriorated", "min"),
]


def compare_policies(seed: int, load_pct: int, policies: Optional[List[str]] = None,
                     replications: int = 1,
                     capacity: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    Run each policy over the same seeds and summarise.

    Because arrivals come from a separate random stream, every policy sees the
    identical patient sequence for a given seed. The only thing that varies is
    the ordering decision, which is what makes the comparison meaningful.
    """
    policies = policies or list(POLICIES)
    seeds = [seed + i * 37 for i in range(max(1, replications))]
    rows = []

    for pol in policies:
        runs = [run_once(s, pol, load_pct, capacity) for s in seeds]
        per_seed = [r.metrics() for r in runs]
        summary: Dict[str, Any] = {}
        for field, _ in BENCH_FIELDS:
            vals = [float(m[field]) for m in per_seed]
            summary[field] = round(_mean(vals), 1)
            summary[field + "_sd"] = round(_stdev(vals), 1)
        for lvl in ("1", "2", "5"):
            vals = [m["wait_by_level"][lvl] for m in per_seed if m["wait_by_level"][lvl] is not None]
            summary[f"wait_level_{lvl}_min"] = round(_mean(vals), 1) if vals else None
        rows.append({
            "policy": pol,
            "label": POLICY_LABELS[pol],
            "replications": len(seeds),
            **summary,
        })

    # mark the winner per column so a UI does not have to re-derive it
    best: Dict[str, Any] = {}
    for field, direction in BENCH_FIELDS:
        vals = [r[field] for r in rows]
        best[field] = max(vals) if direction == "max" else min(vals)

    return {
        "seed": seed,
        "load_pct": load_pct,
        "replications": len(seeds),
        "seeds": seeds,
        "capacity": {**DEFAULT_CAPACITY, **(capacity or {})},
        "rows": rows,
        "best": best,
        "note": ("Arrivals are drawn from a separate random stream, so every policy "
                 "faces the identical patient sequence. Waiting times include "
                 "patients who left without being seen."),
    }


def what_if(seed: int, load_pct: int, policy: str = "medflow",
            deltas: Optional[List[Dict[str, int]]] = None,
            replications: int = 3,
            capacity: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    Marginal value of capacity. Answers "what does one more ICU bed buy us?"
    by re-running the same seeds with the resource added and reporting the
    change in outcomes.
    """
    base_cap = {**DEFAULT_CAPACITY, **(capacity or {})}
    seeds = [seed + i * 37 for i in range(max(1, replications))]
    if deltas is None:
        deltas = [{"resource": k, "delta": 1} for k in ("icu", "ward", "doctor", "nurse", "or")]

    def agg(cap: Dict[str, int]) -> Dict[str, float]:
        ms = [run_once(s, policy, load_pct, cap).metrics() for s in seeds]
        return {f: round(_mean([float(m[f]) for m in ms]), 1) for f, _ in BENCH_FIELDS}

    baseline = agg(base_cap)
    options = []
    for d in deltas:
        res = d["resource"]
        if res not in base_cap:
            continue
        amount = int(d.get("delta", 1))
        cap = dict(base_cap)
        cap[res] = max(0, cap[res] + amount)
        after = agg(cap)
        options.append({
            "resource": res,
            "label": RES_LABELS[res],
            "delta": amount,
            "capacity_after": cap[res],
            "metrics": after,
            "change": {f: round(after[f] - baseline[f], 1) for f, _ in BENCH_FIELDS},
        })

    # rank by what a hospital actually cares about: harm, then abandonment
    options.sort(key=lambda o: (o["change"]["adverse_events"],
                                o["change"]["walked_out"],
                                o["change"]["avg_wait_min"]))
    return {
        "seed": seed,
        "load_pct": load_pct,
        "policy": policy,
        "replications": len(seeds),
        "baseline_capacity": base_cap,
        "baseline": baseline,
        "options": options,
    }
