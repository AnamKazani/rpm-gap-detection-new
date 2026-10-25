"""
Transmission-Gap Detection for Remote Patient Monitoring (RPM)
MSIT 5910 Capstone - version 0.4.0 (Unit 7): the whole system in one file

Run it:  python3 src/gap_detection.py
It walks through the system in five steps (press Enter between steps), then
keeps the nurse dashboard open at http://127.0.0.1:8000 until you press Ctrl+C.

  Step 1  Input: the vendor file
  Step 2  Modules 1 and 2: ingestion and gap scoring, saved in SQLite
  Step 3  Modules 3 and 4 through the web API (real HTTP requests)
  Step 4  Evaluation: accuracy, detection time, speed, memory, response time
  Step 5  The nurse dashboard in the browser

Settings (environment variables):
  RPM_HOST     address to listen on (default 127.0.0.1; use 0.0.0.0 in a container)
  RPM_PORT     port to listen on (default 8000)
  RPM_FLAG_AT  usual gaps of silence before a device is flagged (default 2.0)

Only Python's standard library is used, so there is nothing to install.
All data is synthetic. All tests (unit and system): python3 -m pytest -v
"""
import csv
import json
import os
import random
import secrets
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.4.0"
HOST = os.environ.get("RPM_HOST", "127.0.0.1")
PORT = int(os.environ.get("RPM_PORT", "8000"))
FLAG_AT = float(os.environ.get("RPM_FLAG_AT", "2.0"))  # flag when silent for 2 x the usual gap...
MIN_SILENT_HOURS = 24    # ...and for at least one day
DEFAULT_GAP_HOURS = 24   # used until a device has a pattern
MIN_GAPS = 5             # gaps needed before a pattern counts
BASELINE_DAYS = 30       # learn the rhythm from the last 30 days
SESSION_MINUTES = 60     # readings this close together are one session
NURSES = {"Nurse Alvarez": "Program A", "Nurse Brooks": "Program B"}
OUTCOMES = ["device failure", "hospitalization", "disengagement", "unreachable", "resolved"]
VENDOR_FILE = Path(__file__).with_name("vendor_readings.csv")


def connect():
    """Create the database in memory, with the four tables from the design."""
    db = sqlite3.connect(":memory:", check_same_thread=False)   # shared with the web server
    db.executescript("""
        CREATE TABLE patients (patient_id, device_id, program, enrolled_on,
                               PRIMARY KEY (patient_id, device_id));
        CREATE TABLE readings (patient_id, device_id, reading_time,
                               PRIMARY KEY (patient_id, device_id, reading_time));
        CREATE TABLE scores (patient_id, device_id, silent, usual, score, reason, flagged);
        CREATE TABLE calls (patient_id, outcome, nurse, logged_at);
        CREATE TRIGGER no_edits BEFORE UPDATE ON calls
        BEGIN SELECT RAISE(ABORT, 'call outcomes cannot be changed'); END;
    """)
    return db


# ------------------------------------------------------ Module 1: ingestion
def load_roster(db, enrollment):
    """Save consented patients only, and return their IDs."""
    consented = [p for p in enrollment if p["consent"] == "Y"]
    db.executemany("INSERT INTO patients VALUES (?, ?, ?, ?)",
                   [(p["patient_id"], p["device_id"], p["program"], p["enrolled_on"])
                    for p in consented])
    return {p["patient_id"] for p in consented}


def clean_time(text, as_of):
    """Return the reading time in one standard format, or None if it is
    missing, unreadable, or later than as_of."""
    try:
        when = datetime.fromisoformat(text)
        if when.tzinfo is not None:                   # e.g. ...Z or ...+00:00
            when = when.astimezone().replace(tzinfo=None)   # to local time (Unit 7 fix)
        if when > as_of:
            return None
        return when.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def ingest(db, vendor_rows, consented, as_of):
    """Keep only patient, device and time, for consented patients."""
    counts = {"total": 0, "no_consent": 0, "bad_time": 0, "stored": 0}
    for row in vendor_rows:
        counts["total"] += 1
        when = clean_time(row["reading_time"], as_of)
        if row["patient_id"] not in consented:
            counts["no_consent"] += 1
        elif when is None:
            counts["bad_time"] += 1
        else:
            counts["stored"] += db.execute(
                "INSERT OR IGNORE INTO readings VALUES (?, ?, ?)",
                (row["patient_id"], row["device_id"], when)).rowcount
    counts["duplicates"] = (counts["total"] - counts["no_consent"]
                            - counts["bad_time"] - counts["stored"])
    return counts


# ---------------------------------------------------- Module 2: gap scoring
def usual_gap(times):
    """Median hours between sessions over the last 30 days. Readings less
    than an hour apart count as one session. None if there is no pattern yet."""
    times = sorted(times)
    sessions = [t for i, t in enumerate(times)
                if i == 0 or t - times[i - 1] > timedelta(minutes=SESSION_MINUTES)]
    recent = [t for t in sessions if t >= sessions[-1] - timedelta(days=BASELINE_DAYS)]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(recent, recent[1:])]
    if len(gaps) < MIN_GAPS:
        return None
    return statistics.median(gaps)


def score_device(times, as_of, enrolled_on):
    """How many usual gaps the device has been silent for, as of a given time."""
    times = [t for t in times if t <= as_of]          # ignore future readings
    last = max(times) if times else enrolled_on       # never sent? count from enrollment
    silent = (as_of - last).total_seconds() / 3600
    usual = usual_gap(times)
    score = silent / (usual or DEFAULT_GAP_HOURS)
    flagged = score >= FLAG_AT and silent >= MIN_SILENT_HOURS
    if usual is None:
        reason = "INSUFFICIENT_HISTORY"
    elif flagged:
        reason = "SILENCE_EXCEEDS_BASELINE"
    else:
        reason = "WITHIN_PERSONAL_PATTERN"
    return {"silent": silent, "usual": usual, "score": score,
            "reason": reason, "flagged": flagged}


def score_devices(db, as_of):
    """Score every enrolled device on its own and save the results."""
    devices = db.execute("SELECT patient_id, device_id, enrolled_on FROM patients")
    for patient_id, device_id, enrolled_on in devices.fetchall():
        times = [datetime.fromisoformat(t) for (t,) in db.execute(
            "SELECT reading_time FROM readings WHERE patient_id = ? AND device_id = ?",
            (patient_id, device_id))]
        s = score_device(times, as_of, datetime.fromisoformat(enrolled_on))
        db.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (patient_id, device_id, s["silent"], s["usual"], s["score"],
                    s["reason"], s["flagged"]))


def run_pipeline(db, enrollment, as_of):
    """Modules 1 and 2 together: read the vendor file, store the readings and
    score every device, all in one database transaction."""
    with open(VENDOR_FILE, newline="") as f:
        vendor_rows = list(csv.DictReader(f))
    with db:
        counts = ingest(db, vendor_rows, load_roster(db, enrollment), as_of)
        score_devices(db, as_of)
    return counts


def check_answer_key(db, answer_key):
    """Output validation: compare the flags with what each synthetic patient
    was built to be. Returns {situation: [devices, flagged, 48-hour rule]}."""
    results = {}
    for patient_id, silent, flagged in db.execute(
            "SELECT patient_id, silent, flagged FROM scores"):
        row = results.setdefault(answer_key[patient_id], [0, 0, 0])
        row[0] += 1
        row[1] += flagged
        row[2] += silent >= 48
    return results


# ------------------------------------------------- Module 3: outreach queue
def outreach_queue(db, nurse):
    """Flagged patients in the nurse's own program, highest score first."""
    program = NURSES.get(nurse)
    if program is None:                     # unknown users get nothing
        return []
    return db.execute("""
        SELECT s.patient_id, s.device_id, s.silent, s.usual, s.score, s.reason
        FROM scores s JOIN patients p USING (patient_id, device_id)
        WHERE s.flagged = 1 AND p.program = ?
          AND s.patient_id NOT IN (SELECT patient_id FROM calls)
        ORDER BY s.score DESC, s.silent DESC""", (program,)).fetchall()


def explain(silent, usual, score, reason):
    """Turn a reason code into a sentence a nurse can check before calling."""
    if reason == "INSUFFICIENT_HISTORY":
        return (f"New device: silent {silent:.0f} h. Judged against the default "
                f"{DEFAULT_GAP_HOURS} h gap until it has a pattern.")
    return f"Silent {silent:.0f} h, which is {score:.1f} times its usual {usual:.0f} h gap."


# ------------------------------------------------------- Module 4: call log
class AlreadyLogged(Exception):
    """A call outcome is already saved for this patient."""


def log_outcome(db, nurse, patient_id, outcome):
    """Save what the call found. Nurses can only log their own patients,
    and only once per patient, because a logged call is final."""
    if not patient_id:
        raise ValueError("patient_id is required")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    row = db.execute("SELECT program FROM patients WHERE patient_id = ?",
                     (patient_id,)).fetchone()
    if row is None or row[0] != NURSES.get(nurse):
        raise PermissionError(f"{nurse} cannot log calls for {patient_id}")
    if db.execute("SELECT 1 FROM calls WHERE patient_id = ?", (patient_id,)).fetchone():
        raise AlreadyLogged(f"a call is already logged for {patient_id}")
    db.execute("INSERT INTO calls VALUES (?, ?, ?, ?)",
               (patient_id, outcome, nurse, datetime.now().isoformat(" ", "minutes")))


# ------------------------------------------- web API: links the dashboard to the modules
TOKENS = {}                  # sign-in token -> nurse
LOCK = threading.Lock()      # one database, so one request uses it at a time


class Api(BaseHTTPRequestHandler):
    db = None

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, DASHBOARD.encode(), "text/html; charset=utf-8")
        if self.path == "/api/health":
            return self.send_json(200, {"status": "ok", "version": VERSION})
        if self.path == "/api/queue":
            nurse = self.signed_in_nurse()
            if nurse is None:
                return self.send_json(401, {"error": "sign in first"})
            with LOCK:
                queue = outreach_queue(self.db, nurse)                         # Module 3
            patients = [{"rank": rank, "patient_id": p, "device_id": d,
                         "silent_hours": round(s, 1), "usual_hours": round(u, 1) if u else None,
                         "score": round(sc, 1), "reason": r, "explanation": explain(s, u, sc, r)}
                        for rank, (p, d, s, u, sc, r) in enumerate(queue, 1)]
            return self.send_json(200, {"program": NURSES[nurse], "patients": patients,
                                        "outcomes": OUTCOMES})
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        body = self.read_json()
        if body is None:                                      # Unit 7 fix
            return self.send_json(400, {"error": "the request body must be a JSON object"})
        if self.path == "/api/login":
            if body.get("nurse") not in NURSES:
                return self.send_json(401, {"error": "unknown user"})
            token = secrets.token_hex(16)
            TOKENS[token] = body["nurse"]
            return self.send_json(200, {"nurse": body["nurse"], "token": token})
        if self.path == "/api/calls":
            nurse = self.signed_in_nurse()
            if nurse is None:
                return self.send_json(401, {"error": "sign in first"})
            try:
                with LOCK, self.db:                                            # Module 4
                    log_outcome(self.db, nurse, body.get("patient_id"), body.get("outcome"))
                    left = len(outreach_queue(self.db, nurse))
            except PermissionError as error:
                return self.send_json(403, {"error": str(error)})
            except AlreadyLogged as error:
                return self.send_json(409, {"error": str(error)})
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            return self.send_json(201, {"logged": body.get("patient_id"), "left_in_queue": left})
        self.send_json(404, {"error": "not found"})

    def signed_in_nurse(self):
        return TOKENS.get(self.headers.get("Authorization", "").removeprefix("Bearer "))

    def read_json(self):
        """The request body as a dict, or None if it is not a JSON object."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def send_json(self, status, data):
        self.reply(status, json.dumps(data).encode(), "application/json")

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")         # patient data: never cache
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                             # keep the terminal tidy
        pass


def start_server(db):
    """Start the web server in the background, on RPM_PORT or any free port."""
    Api.db = db
    try:
        server = ThreadingHTTPServer((HOST, PORT), Api)
    except OSError:                                           # port busy: use a free one
        server = ThreadingHTTPServer((HOST, 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(base, method, path, body=None, token=None):
    """Send one real HTTP request to the running app; return (status, reply)."""
    request = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>RPM Outreach Queue</title>
<style>
  body { margin: 0; background: #f3f5f7; color: #1c2530; font: 15px/1.45 -apple-system, "Segoe UI", sans-serif; }
  header { background: #124e5c; color: #fff; padding: 14px 24px; font-size: 20px; font-weight: 650; }
  main { max-width: 1180px; margin: 0 auto; padding: 20px 24px; }
  .card { background: #fff; border: 1px solid #dde3e8; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
  select, button { font: inherit; padding: 6px 10px; border-radius: 6px; border: 1px solid #b8c2cc; }
  button { background: #124e5c; color: #fff; font-weight: 600; cursor: pointer; }
  #message { display: none; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-weight: 600; }
  #message.ok { display: block; background: #e7f5ec; color: #17603a; }
  #message.error { display: block; background: #fdecec; color: #9b1c1c; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; text-transform: uppercase; color: #5b6672; padding: 8px; border-bottom: 2px solid #dde3e8; }
  td { padding: 10px 8px; border-bottom: 1px solid #dde3e8; vertical-align: top; white-space: nowrap; }
  td.why { white-space: normal; }
  .score { color: #fff; font-weight: 700; border-radius: 999px; padding: 2px 10px; }
  small, .hint, footer { color: #5b6672; font-size: 13px; }
  small { display: block; font-size: 12px; }
</style></head>
<body>
<header>RPM Outreach Queue</header>
<main>
  <div class="card"><b>Nurse</b>
    <select id="nurse"><option>Nurse Alvarez</option><option>Nurse Brooks</option></select>
    <button onclick="signIn()">Sign in</button> <span id="who" class="hint"></span></div>
  <div id="message"></div>
  <div class="card"><b id="title">Outreach queue</b>
    <p class="hint">Patients whose device has gone quiet compared with its own usual rhythm, highest score first.
      Score = hours silent / the device's usual gap; a device is flagged at 2.0.</p>
    <table><thead><tr><th>#</th><th>Patient</th><th>Device</th><th>Silent</th><th>Usual gap</th>
      <th>Score</th><th>Why this patient is here</th><th>Log what the call found</th></tr></thead>
      <tbody id="rows"><tr><td colspan="8" class="hint">Sign in to see your queue.</td></tr></tbody></table></div>
  <footer>All data is synthetic. A logged call is final: outcomes cannot be edited.</footer>
</main>
<script>
let token = null;
const $ = (id) => document.getElementById(id);
const safe = (text) => String(text).replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");

// Every screen action goes through this one function: browser -> API -> database.
async function api(method, path, body) {
  const response = await fetch(path, { method, body: body && JSON.stringify(body),
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function show(text, kind) { $("message").textContent = text; $("message").className = kind; }

async function signIn() {
  const user = await api("POST", "/api/login", { nurse: $("nurse").value });
  token = user.token;
  $("who").textContent = "Signed in as " + user.nurse;
  show("", "");
  loadQueue();
}

async function loadQueue() {
  const data = await api("GET", "/api/queue");
  $("title").textContent = `Outreach queue: ${data.program} (${data.patients.length} patients)`;
  const options = data.outcomes.map((o) => `<option>${o}</option>`).join("");
  $("rows").innerHTML = data.patients.map((p) => `<tr>
    <td>${p.rank}</td><td>${safe(p.patient_id)}</td><td>${safe(p.device_id)}</td>
    <td>${p.silent_hours.toFixed(1)} h</td><td>${p.usual_hours ? p.usual_hours.toFixed(1) + " h" : "none yet"}</td>
    <td><span class="score" style="background:${p.reason === "INSUFFICIENT_HISTORY" ? "#475467"
      : p.score >= 4 ? "#b42318" : "#b54708"}">${p.score.toFixed(1)}</span></td>
    <td class="why">${safe(p.explanation)}<small>${p.reason}</small></td>
    <td><select id="pick-${p.rank}"><option value="">Choose outcome...</option>${options}</select>
      <button onclick="logCall('${safe(p.patient_id)}', $('pick-${p.rank}').value)">Save</button></td>
  </tr>`).join("") || `<tr><td colspan="8" class="hint">No patients need a call right now.</td></tr>`;
}

async function logCall(patientId, outcome) {
  if (!outcome) return show(`Choose what the call found for ${patientId} before saving.`, "error");
  try {
    const result = await api("POST", "/api/calls", { patient_id: patientId, outcome });
    show(`Call logged for ${patientId} (${outcome}). ${result.left_in_queue} patients left in your queue.`, "ok");
    loadQueue();
  } catch (error) { show("Not saved: " + error.message, "error"); }
}
</script></body></html>
"""


# ----------------------------------------------------------- synthetic data
# (how many, program, usual hours between readings, hours silent now,
#  days of history, consent, what scoring should find)
GROUPS = [
    (7, "Program A", 24, (2, 26), 45, "Y", "Normal rhythm"),
    (8, "Program B", 24, (2, 26), 45, "Y", "Normal rhythm"),
    (3, "Program A", 72, (52, 84), 45, "Y", "Normal rhythm"),     # rural, every 3 days
    (2, "Program B", 72, (52, 84), 45, "Y", "Normal rhythm"),     # rural, every 3 days
    (2, "Program A", 24, (80, 230), 45, "Y", "Gone quiet"),
    (3, "Program B", 24, (80, 230), 45, "Y", "Gone quiet"),
    (1, "Program A", 72, (230, 260), 45, "Y", "Gone quiet"),
    (1, "Program A", 12, (28, 34), 45, "Y", "Gone quiet"),         # twice a day
    (1, "Program A", 24, (50, 58), 4, "Y", "New patient"),
    (1, "Program A", 24, (2, 26), 45, "N", "No consent"),
    (1, "Program B", 24, (2, 26), 45, "N", "No consent"),
]


def synthetic_kinds(scale=1):
    """What each synthetic patient is built to be, in patient order. The seed
    is fixed, so every run makes the same patients."""
    rng = random.Random(5910)
    kinds = [g[1:] for g in GROUPS for _ in range(g[0] * scale)]
    rng.shuffle(kinds)
    return kinds, rng


def make_synthetic_data(as_of, scale=1):
    """Return (enrollment, vendor rows, answer key) for 30 patients, or
    30 x scale patients for the load test."""
    kinds, rng = synthetic_kinds(scale)
    enrollment, readings, answer_key = [], [], {}
    for n, (program, every, silent, history, consent, truth) in enumerate(kinds, 1):
        patient, device = f"P-{1000 + n}", f"DEV-{4000 + n}"
        answer_key[patient] = truth
        enrolled = as_of - timedelta(days=history)
        enrollment.append({"patient_id": patient, "device_id": device, "program": program,
                           "consent": consent, "enrolled_on": enrolled.isoformat()})
        t = as_of - timedelta(hours=rng.uniform(*silent))
        while t > enrolled:
            readings.append((patient, device, t.isoformat(timespec="seconds")))
            t -= timedelta(hours=max(every / 2, rng.gauss(every, every / 10)))
    readings += rng.sample(readings, len(readings) // 100)      # vendor resends a few
    # two bad rows: a time in the wrong format, and a device clock set ahead
    readings.append(("P-1001", "DEV-4001", (as_of - timedelta(hours=5)).strftime("%m/%d/%Y")))
    readings.append(("P-1007", "DEV-4007", (as_of + timedelta(days=2)).isoformat()))
    vendor = [{"patient_id": p, "device_id": d, "reading_time": t,
               "systolic": rng.randint(110, 165), "pulse": rng.randint(56, 100)}
              for p, d, t in readings]
    return enrollment, vendor, answer_key


# --------------------------------------------------------------- evaluation
def evaluate(base, token):
    """Step 4: accuracy, detection time, speed, memory and response time."""
    as_of = datetime(2026, 10, 1, 9, 0)                 # fixed, so results repeat exactly
    enrollment, vendor, answer_key = make_synthetic_data(as_of, scale=8)     # 240 patients
    db = connect()
    ingest(db, vendor, load_roster(db, enrollment), as_of)
    score_devices(db, as_of)
    kinds, _ = synthetic_kinds(scale=8)
    rhythm = {f"P-{1000 + n}": {12: "Twice a day", 24: "Daily", 72: "Every 3 days"}[k[1]]
              for n, k in enumerate(kinds, 1)}
    rows = db.execute("SELECT patient_id, silent, flagged FROM scores").fetchall()

    print(f"  Accuracy on 240 synthetic patients ({len(rows)} devices)")
    print(f"    {'Rule':<20}{'Precision':>10}{'Recall':>8}{'False alarms':>14}{'Missed':>8}")
    for rule, flags in (("Personal baseline", lambda silent, flagged: flagged),
                        ("Fixed 48-hour rule", lambda silent, flagged: silent >= 48)):
        should = [answer_key[p] in ("Gone quiet", "New patient") for p, _, _ in rows]
        did = [bool(flags(s, f)) for _, s, f in rows]
        hits = sum(a and b for a, b in zip(should, did))
        alarms = sum(b and not a for a, b in zip(should, did))
        missed = sum(a and not b for a, b in zip(should, did))
        print(f"    {rule:<20}{hits / (hits + alarms):>10.2f}{hits / (hits + missed):>8.2f}"
              f"{alarms:>14}{missed:>8}")
    normal = [(rhythm[p], s >= 48) for p, s, _ in rows if answer_key[p] == "Normal rhythm"]
    for name, label in (("Daily", "daily"), ("Every 3 days", "every-3-day")):
        group = [alarm for r, alarm in normal if r == name]
        print(f"    48-hour rule false alarms among normal {label} senders: {sum(group)} of {len(group)}")

    print("  Hours from last reading to flag (median, devices that went quiet)")
    hours = {}
    for patient, device, enrolled in db.execute(
            "SELECT patient_id, device_id, enrolled_on FROM patients").fetchall():
        if answer_key[patient] == "Gone quiet":
            times = [datetime.fromisoformat(t) for (t,) in db.execute(
                "SELECT reading_time FROM readings WHERE patient_id = ? AND device_id = ?",
                (patient, device))]
            first = next(h for h in range(1, 2000) if score_device(
                times, max(times) + timedelta(hours=h), datetime.fromisoformat(enrolled))["flagged"])
            hours.setdefault(rhythm[patient], []).append(first)
    for name in ("Twice a day", "Daily", "Every 3 days"):
        print(f"    {name:<14}{statistics.median(hours[name]):>5.0f} h   (fixed 48-hour rule: 48 h)")

    now = datetime.now()                                 # load test: 4,800 patients
    enrollment, vendor, _ = make_synthetic_data(now, scale=160)
    db = connect()
    start = time.perf_counter()
    ingest(db, vendor, load_roster(db, enrollment), now)
    ingest_seconds = time.perf_counter() - start
    start = time.perf_counter()
    score_devices(db, now)
    scoring_ms = (time.perf_counter() - start) * 1000
    devices = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    print(f"  Speed with 4,800 patients ({len(vendor):,} rows): ingestion "
          f"{len(vendor) / ingest_seconds:,.0f} rows per second, "
          f"scoring {devices:,} devices in {scoring_ms:,.0f} ms")
    try:
        import resource                                  # macOS and Linux
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(f"  Memory: the program peaked at "
              f"{peak / (1024 * 1024 if sys.platform == 'darwin' else 1024):.0f} MB")
    except ImportError:
        print("  Memory: not measured on Windows")

    times = []
    for _ in range(100):
        start = time.perf_counter()
        call(base, "GET", "/api/queue", token=token)
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    print(f"  Queue API response time (100 requests): median {statistics.median(times):.1f} ms,"
          f" 95th percentile {times[94]:.1f} ms")


# -------------------------------------------------------------- walkthrough
def main(keep_open=False):
    """Walk through the whole system. keep_open leaves the dashboard running."""
    shown = []
    def step(title):
        if shown and keep_open and sys.stdin.isatty():
            input("\n(press Enter for the next step)")
        shown.append(title)
        print(f"\n{title}\n{'-' * len(title)}")

    print(f"RPM GAP DETECTION v{VERSION} - full system walkthrough (synthetic data)")
    as_of = datetime.now().replace(microsecond=0)
    enrollment, vendor, answer_key = make_synthetic_data(as_of)

    step("STEP 1  Input: the vendor file")
    with open(VENDOR_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(vendor[0]))
        writer.writeheader()
        writer.writerows(vendor)
    print(f"  {VENDOR_FILE.name}: {len(vendor):,} rows (first three and last two shown)")
    print(f"    {'patient':<9}{'device':<10}{'reading_time':<22}{'systolic':>8}{'pulse':>7}")
    for row in vendor[:3] + vendor[-2:]:
        print(f"    {row['patient_id']:<9}{row['device_id']:<10}{row['reading_time']:<22}"
              f"{row['systolic']:>8}{row['pulse']:>7}")

    step("STEP 2  Modules 1 and 2: ingestion and gap scoring (saved in SQLite)")
    db = connect()
    counts = run_pipeline(db, enrollment, as_of)
    print(f"  Rows read              {counts['total']:>6,}")
    print(f"  Stored                 {counts['stored']:>6,}  (patient, device and time only)")
    print(f"  Skipped, no consent    {counts['no_consent']:>6,}")
    print(f"  Rejected, bad time     {counts['bad_time']:>6,}")
    print(f"  Duplicates ignored     {counts['duplicates']:>6,}")
    scored, flagged = db.execute("SELECT COUNT(*), SUM(flagged) FROM scores").fetchone()
    print(f"  Devices scored {scored}, flagged {flagged} (score of {FLAG_AT} or more)")
    check = check_answer_key(db, answer_key)
    print(f"  Answer key: {check['Gone quiet'][1]} of {check['Gone quiet'][0]} quiet devices "
          f"flagged, {check['Normal rhythm'][1]} of {check['Normal rhythm'][0]} normal devices flagged")

    step("STEP 3  Modules 3 and 4 through the web API (real HTTP requests)")
    server = start_server(db)
    base = f"http://{HOST}:{server.server_port}"
    results = []
    status, user = call(base, "POST", "/api/login", {"nurse": "Nurse Alvarez"})
    token = user.get("token")
    print(f'  > POST /api/login  {{"nurse": "Nurse Alvarez"}}\n  < {status}  signed in, token {token[:8]}...')
    results.append(status == 200)
    status, queue = call(base, "GET", "/api/queue", token=token)
    print(f"  > GET  /api/queue  (with token)\n  < {status}  {len(queue['patients'])} patients "
          f"for {queue['program']}, highest score first:")
    for p in queue["patients"]:
        print(f"      {p['rank']}  {p['patient_id']}  score {p['score']:>4}  {p['explanation']}")
    results.append(status == 200 and len(queue["patients"]) == 5)
    with LOCK:
        other = outreach_queue(db, "Nurse Brooks")[0][0]      # a Program B patient
    for body, expected in (({"patient_id": queue["patients"][0]["patient_id"],
                             "outcome": "device failure"}, 201),
                           ({"patient_id": other, "outcome": "resolved"}, 403)):
        status, reply = call(base, "POST", "/api/calls", body, token)
        print(f"  > POST /api/calls  {json.dumps(body)}\n  < {status}  {json.dumps(reply)}")
        results.append(status == expected)
    status, reply = call(base, "GET", "/api/queue")
    print(f"  > GET  /api/queue  (no token)\n  < {status}  {json.dumps(reply)}")
    results.append(status == 401)
    try:
        with LOCK:
            db.execute("UPDATE calls SET outcome = 'resolved'")
        results.append(False)
    except sqlite3.DatabaseError as error:
        print(f"  Editing the logged call in the database: BLOCKED ({error})")
        results.append(True)
    status, reply = call(base, "GET", "/api/health")
    print(f"  > GET  /api/health\n  < {status}  {json.dumps(reply)}")
    results.append(status == 200)
    print(f"  Integration checks passed: {sum(results)} of {len(results)}")

    step("STEP 4  Evaluation")
    evaluate(base, token)

    step("STEP 5  Nurse dashboard")
    print(f"  Open {base} in your browser and sign in as Nurse Alvarez.")
    if keep_open:
        print("  Press Ctrl+C here to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main(keep_open=True)
