# MEDFLOW API

Backend for the hospital resource management simulator. A discrete-event model
of an emergency department, a scheduler that decides who gets the next bed, and
an HTTP API around both.

The engine is a bit-exact port of the browser build, so a run demonstrated in
the UI reproduces byte for byte on the server. `tests/test_sim.py` asserts it.

```
app/
  sim.py       simulation engine and scheduler. No web framework, no I/O.
  schemas.py   request and response models
  store.py     in-memory run registry with locking and TTL eviction
  main.py      FastAPI app: REST endpoints and the live WebSocket feed
tests/         invariant, determinism, parity and API checks
```

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Interactive docs at `http://localhost:8000/docs`.

```bash
docker build -t medflow-api . && docker run -p 8000:8000 medflow-api
```

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness and active run count |
| `GET` | `/api/config` | Policies, resources, triage levels, clinical targets |
| `POST` | `/api/runs` | Create a run from a seed, policy, load and capacity |
| `GET` | `/api/runs` | List live runs |
| `GET` | `/api/runs/{id}` | Full snapshot: resources, queue, metrics, log |
| `POST` | `/api/runs/{id}/advance` | Step the clock forward by N minutes |
| `POST` | `/api/runs/{id}/surge` | Inject a mass casualty incident now |
| `POST` | `/api/runs/{id}/reset` | Rewind to minute zero, same configuration |
| `DELETE` | `/api/runs/{id}` | Discard a run |
| `GET` | `/api/runs/{id}/metrics` | Just the headline numbers |
| `GET` | `/api/runs/{id}/patients.csv` | Patient-level export, one row per departure |
| `POST` | `/api/benchmark` | Every policy over identical seeds, with a winner per column |
| `POST` | `/api/what-if` | Marginal value of one more bed, doctor or nurse |
| `WS` | `/ws/runs/{id}` | Live snapshot stream with pause, speed and surge commands |

### Create and advance

```bash
RID=$(curl -s -X POST localhost:8000/api/runs \
  -H 'content-type: application/json' \
  -d '{"seed":4207,"policy":"medflow","load_pct":120}' | jq -r .run_id)

curl -s -X POST localhost:8000/api/runs/$RID/advance \
  -H 'content-type: application/json' -d '{"minutes":600}' | jq .snapshot.metrics
```

Capacity is overridable per run:

```json
{"seed": 4207, "policy": "medflow", "load_pct": 130,
 "capacity": {"icu": 10, "doctor": 12, "or": 4}}
```

### Compare policies

```bash
curl -s -X POST localhost:8000/api/benchmark \
  -H 'content-type: application/json' \
  -d '{"seed":4207,"load_pct":100,"replications":5}' | jq '.rows[] | {policy, seen, walked_out, adverse_events}'
```

`replications` runs several seeds per policy and reports a standard deviation
alongside each mean, so you can tell a real difference from a lucky run.

### Price a staffing decision

```bash
curl -s -X POST localhost:8000/api/what-if \
  -H 'content-type: application/json' \
  -d '{"seed":4207,"load_pct":150,"replications":3}' | jq '.options[] | {label, change}'
```

Adds one unit of each resource in turn, re-runs the same seeds and ranks the
options by reduction in harm and abandonment. At 150% load on the default
department, one more doctor removes roughly 25 adverse events and 21 walkouts
a day, while one more ward bed changes almost nothing. That is the answer to
"where is our bottleneck", and it is the most persuasive thing in the project.

### Live feed

```js
const ws = new WebSocket(`ws://localhost:8000/ws/runs/${runId}?speed=30`);
ws.onmessage = (e) => render(JSON.parse(e.data).snapshot);
ws.send(JSON.stringify({ action: "surge", count: 8 }));
ws.send(JSON.stringify({ action: "speed", value: 120 }));
ws.send(JSON.stringify({ action: "pause" }));
```

Each message advances `speed` simulated minutes and returns a full snapshot.

## The scheduler

Three policies, so the comparison has a baseline.

- **First come, first served.** Arrival order, urgency ignored.
- **Strict triage.** Urgency only. No aging, so low-acuity patients starve.
- **MEDFLOW.** The one being argued for.

MEDFLOW sorts the queue every simulated minute into three tiers:

1. Resuscitation and emergent cases are never deferred for anyone.
2. Anyone past their hard clinical deadline is served most-overdue-first. This
   is the starvation guarantee, and it is why level 4 and 5 patients stop
   walking out. Acuity breaks exact ties.
3. Everyone else is ranked by acuity, stretched by how far past target they have
   waited and by modelled deterioration risk.

Resources are then acquired as whole bundles, atomically and in a fixed global
order: a patient is admitted only when the bed, the clinician and the nurses are
free at the same instant. A patient therefore never holds one resource while
waiting for another, so the Coffman hold-and-wait condition is never satisfied
and deadlock is structurally impossible rather than merely unlikely.

When the top patient still cannot be placed, the resource they are short of is
reserved instead of being handed down the queue, and someone lower is backfilled
into it only if they will be finished before the reservation comes due — the
backfill idea borrowed from HPC batch schedulers. A critical patient who needs
intensive care and finds no ICU bed is boarded in a ward bed rather than left
waiting.

## Reading the results honestly

Two things make the numbers trustworthy, and they are worth saying out loud
when presenting.

**Arrivals run on a separate random stream from in-hospital events.** For a
given seed every policy faces the identical patient sequence, so the only
variable is the ordering decision.

**Waiting times include people who left without being seen.** Without this a
policy that abandons its long waiters looks artificially fast, because the
patients who would have dragged the average up are never counted.

With those in place, MEDFLOW does not win every column, and claiming it does
will not survive a sharp question. On the default department at 100% load,
averaged over ten seeds:

- Against first-come ordering it wins outright, with roughly an order of
  magnitude fewer adverse events.
- Against strict triage it treats more patients and cuts walkouts by more than
  half, because overdue patients stop being skipped forever.
- Against strict triage on adverse events the two are level. Both are far better
  than first-come; neither is meaningfully better than the other.
- It pays for the walkout reduction with somewhat longer average waits for
  level 2 and 3 patients, because capacity that strict triage would have given
them now goes to overdue patients instead.

Push `load_pct` high enough and all three policies fail together. No scheduler
manufactures staff who are not on shift, which is the point of `/api/what-if`.

## Wiring the existing frontend

The browser build carries its own copy of the engine, so it works standalone.
To drive it from the server instead, replace the `tick()` loop with the
WebSocket feed and render from `snapshot` — the field names in
`Sim.snapshot()` deliberately mirror what the UI already draws. Keep the local
engine as an offline fallback; a demo that survives conference wifi failing is
worth the duplication.

## Tests

```bash
pytest -q
MEDFLOW_HTML=/path/to/medflow.html pytest -q   # also runs the JS parity test
```

The invariant tests are the ones to point at if someone asks whether the model
is sound: capacity is never exceeded, no resource unit is ever double-booked,
no patient vanishes, and the deadline ordering rule is asserted directly rather
than inferred from averages.

## Runtime flow

The actual usage flow is:

1. User opens the frontend or calls the API.
2. A new simulation run is created.
3. Patients arrive based on the chosen seed and load level.
4. The policy decides who gets served next.
5. Resources are checked and assigned as a bundle.
6. The simulation steps forward in time.
7. Metrics are computed and returned.
8. The user can reset, surge, benchmark, or compare resource scenarios.

This is the core execution path of the project: patient arrival → policy
selection → resource allocation → simulation advance → metrics and visibility.
