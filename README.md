

MEDFLOW simulates the flow of patients through an emergency department where resources such as ICU beds, ward beds, doctors, nurses, operating rooms, and ambulances are limited.

The main problem is deciding which patient should be treated next when several patients are waiting and resources are unavailable.

The system compares three scheduling policies:

First come, first served
Strict triage
MEDFLOW, which combines urgency, waiting time, clinical deadlines, deterioration risk, and resource reservations.
The purpose is to demonstrate how a smarter scheduling policy can reduce patient walkouts, waiting-time problems, and adverse events.”

The repository is primarily written in Python, with an HTML and JavaScript frontend and a Dockerfile for containerized deployment.

At the root level, the important files are:

Text
README.md
main.py
sim.py
medflow.html
test_sim.py
medflow-backend/
The repository has two main parts:

The standalone browser simulator in medflow.html
The FastAPI backend inside medflow-backend”
“The README explains the purpose of MEDFLOW, how to run it, the available API endpoints, the scheduling policies, and how to interpret the results.

It also documents the important design principles, including deterministic simulations, resource capacity limits, and fair policy comparisons.”

medflow.html
[Screen: Open medflow.html or show it in the browser]

“This is the frontend application.

It contains:

The user interface
Styling
The JavaScript simulation engine
The controls for starting, pausing, resetting, and speeding up the simulation
The capacity board
The patient queue
The event log
Live metrics
The policy comparison table
The chart showing queue depth and average wait time
The HTML file is designed to work independently in the browser. This means the simulator can still be demonstrated even if the backend is not running.”

Root sim.py
“This is a Python version of the simulation engine.

It contains the main Sim class, patient generation, resource allocation, scheduling policies, metrics, benchmarking, and what-if analysis.

The Python engine is designed to reproduce the behavior of the JavaScript engine using the same deterministic random-number generation approach.”

oot main.py
“This file is an API entry-point version of the project
ests sim pyThis file contains tests for the simulation logic.

The tests check important properties such as:

Resources are never over capacity
A resource is not assigned to two patients at the same time
Every patient is accounted for
The same seed produces reproducible results
MEDFLOW prevents starvation better than strict triage
The Python and JavaScript engines produce matching results

The MEDFLOW backend is built using Python and FastAPI. It provides APIs to create and control emergency-department simulation runs.

The main file, app/main.py, defines endpoints for creating runs, advancing simulated time, triggering mass-casualty events, resetting runs, viewing metrics, exporting results, comparing policies, and performing what-if resource analysis.

The simulation logic is implemented in app/sim.py. It models patient arrivals, triage levels, waiting times, resource allocation, treatment, deterioration, walkouts, and resource utilization

his is the MEDFLOW simulator interface.

At the top, we have:

- Start and pause controls
- Reset control
- Simulation speed
- Scheduling policy selection
- Seed input
- Load percentage
- Mass-casualty trigger
- Theme selection”

### Capacity Board

“The capacity board shows:

- ICU beds
- Ward beds
- Operating rooms
- Doctors
- Nurses
- Ambulances

Each tile represents one resource unit.

The colors represent the five triage levels.”

### Waiting Queue

“The waiting queue shows patients in priority order.

It displays:

- Patient name
- Patient ID
- Triage level
- Waiting time
- Required resources
- Priority score
- Ambulance status
- Deterioration status
- Whether the patient is being held or backfilled”

### Live Outcomes

“The live outcomes panel displays:

- Patients seen
- Patients seen on time
- Average wait
- 95th percentile wait
- Level 1 wait
- Level 5 wait
- Adverse events
- Patients who walked out
- Patients boarded in a ward
- ICU blocked time
- Ambulance delay”

### Queue Chart

“The chart shows the queue depth and average waiting time over the simulation.”

### Event Log

“The event log records important events such as:

- Patients starting treatment
- Patients being discharged
- Patient deterioration
- Patients leaving without being seen
- Mass-casualty events
- Patients being boarded because of ICU capacity”

---

## 12. Demonstrate the Simulator

“I will start with the default configuration:

- Seed: 4207
- Load: 100 percent
- Policy: MEDFLOW

I click **Start**.

The clock begins advancing. New patients arrive, they are placed in the waiting queue, and the scheduler assigns resources when they become available.”

“I can increase the speed to 6 times, 30 times, or 120 times to complete the simulation faster.”

“Now I will trigger a mass-casualty event.

The queue increases, the resources become more occupied, and the event log records the incoming patients.”

“I can pause the simulation to inspect the queue and understand why one patient has a higher priority than another.”

“Next, I can switch from MEDFLOW to First Come and reset the simulation.

I can run the same seed again using Strict Triage and MEDFLOW.

Because the seed remains the same, the patient arrival pattern remains consistent, which makes the comparison fair.”

“Finally, I click **Run all three** to display the policy comparison table.”

---

## 13. Explain How the Simulator Works

“The simulator advances one simulated minute at a time.

Each minute follows these steps:

### Step 1: Patient arrivals

Patients arrive based on the time of day and the selected load percentage.

Higher load means more patients entering the department.”

### Step 2: Ambulance handling

Some patients arrive by ambulance.

The simulator checks ambulance availability and tracks the time until the patient reaches the department.”

### Step 3: Waiting-patient updates

Waiting patients accumulate waiting time and clinical risk.

Some patients can deteriorate to a higher triage level.

Lower-acuity patients may eventually leave without being seen if they wait too long.”

### Step 4: Treatment progress

Patients who are already receiving care continue through their treatment.

Resources are released when they are no longer needed.”

### Step 5: Resource allocation

The selected scheduling policy sorts the waiting queue.

MEDFLOW checks whether the complete resource bundle is available before admitting a patient.

For example, a patient may require:

- One bed
- One doctor
- One or more nurses
- Possibly an operating room

The system assigns these resources together rather than allowing a patient to hold one resource while waiting for another.”

### Step 6: Metrics

The simulator updates:

- Queue size
- Average waiting time
- Resource utilization
- Patients served
- Patients who walked out
- Adverse events
- Deterioration
- Ambulance delays
- ICU blocking time”

- The 3rd policy is MEDFLOW, the selected policy in the simulator. It is a hybrid scheduling strategy designed to balance urgency with fairness.

How MEDFLOW prioritizes patients
Every simulated minute, MEDFLOW recalculates each waiting patient’s priority and sorts the queue into three tiers:

Resuscitation and emergent patients first

Level 1 patients receive the highest priority.
Level 2 patients come next.
Their priority is effectively above all other patients, so they are not deferred by lower-acuity cases.
Overdue patients next

Each acuity level has a hard deadline:
Level 1: 15 minutes
Level 2: 45 minutes
Level 3: 100 minutes
Level 4: 120 minutes
Level 5: 95 minutes
Once a patient passes their deadline, MEDFLOW prioritizes them according to how overdue they are.
This prevents lower-acuity patients from waiting indefinitely and is the policy’s starvation guarantee.
All other patients

Patients who are not emergent and not overdue are ranked using:
Acuity
Waiting time relative to their target
Deterioration risk
Waiting increases a patient’s score, so the policy gradually raises the priority of patients who have been waiting too long.
The scoring logic is implemented in medflow.html at score(p).

How resources are assigned
MEDFLOW does not assign patients one resource at a time. It checks whether the patient’s entire resource bundle is available—for example, a bed, doctor, nurse, and possibly an operating room—and acquires them atomically.

This means a patient cannot hold a bed while waiting for a doctor, preventing resource deadlocks. See allocate().

Reservation and backfill
If a high-priority Level 1 or Level 2 patient cannot currently be admitted because a resource is unavailable, MEDFLOW can reserve capacity for that patient.

While waiting for the resource:

A shorter patient may still be admitted if they can finish before the reserved resource becomes available. This is called backfill.
A longer patient is blocked from using that reserved capacity.
Reservations are limited to genuinely urgent patients, so Level 4 patients do not unnecessarily reduce throughput.
ICU overflow behavior
If an ICU bed is unavailable, a patient who needs ICU care may temporarily be boarded in a ward bed, while retaining the required staffing. This is tracked as a boarded patient and contributes to ICU-blocked time.


How to launch the project
Option 1: Open the frontend directly
If the project contains a standalone HTML frontend, open the main HTML file in a browser.

For example:

Text
medflow.html
You can double-click the file or right-click it and choose Open with Browser.

Option 2: Run using a local web server
For a more reliable launch, start a local web server from the project directory:

bash
python -m http.server 8000
Then open:

Text
http://localhost:8000
If the frontend is located in another directory, run the command from that directory:

bash
cd path/to/frontend
python -m http.server 8000
Option 3: Use VS Code Live Server
Open the project in Visual Studio Code.
Install the Live Server extension.
Open the main HTML file.
Right-click the file.
Select Open with Live Server.
The project will open in the browser automatically.

How to use the simulator
Open the simulator in your browser.
Select a scheduling policy.
Set the patient load and available hospital capacity.
Choose a random seed if available.
Start or reset the simulation.
Advance the simulation using the time controls.
Observe:
Patient queue
Resource usage
Treated patients
Waiting times
Walkouts
Adverse events
Overall performance metrics
Use the surge or mass-casualty option to test the system under extreme demand.
Compare the policies to identify which approach performs best.
Running tests
If the repository includes Python tests, install the dependencies first:

bash
pip install -r requirements.txt
Then run:

bash
pytest -q
The tests verify simulation behavior such as:

Hospital capacity is not exceeded
Resources are not assigned to multiple patients at the same time
Patients are not lost during the simulation
Scheduling deadlines are respected
Results are deterministic for the same seed
Different policies can be compared consistently

References
Software Libraries & Frameworks
SimPy (Discrete-Event Simulation):
Team SimPy. SimPy: Discrete Event Simulation for Python. Read the Docs. Available at: https://simpy.readthedocs.io/
Anthropic Claude API:
Anthropic. Claude AI Developer Documentation & API Reference. Available at: https://docs.anthropic.com/
FastAPI Web Framework:
Ramírez, S. (Tiangolo). FastAPI: High Performance Python Web Framework. Available at: https://fastapi.tiangolo.com/
Python Programming Language:
Python Software Foundation. Python Language Reference, Version 3.x. Available at: https://www.python.org/
