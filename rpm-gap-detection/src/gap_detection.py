"""
Transmission-Gap Detection for Remote Patient Monitoring (RPM)
MSIT 5910 Capstone - version 0.2.0 (Unit 5)

Flags home monitoring devices that have gone quiet compared with their own
usual rhythm, and lists those patients in a ranked queue for nurses.
All data is synthetic.

Run the demo:   python3 src/gap_detection.py
Run the tests:  python3 -m pytest -v --cov
"""
import random
import sqlite3
import statistics
from datetime import datetime, timedelta

FLAG_AT = 2.0            # flag when silent for 2 x the usual gap...
MIN_SILENT_HOURS = 24    # ...and for at least one day
DEFAULT_GAP_HOURS = 24   # used until a device has a pattern
MIN_GAPS = 5             # gaps needed before a pattern counts
BASELINE_DAYS = 30       # learn the rhythm from the last 30 days
SESSION_MINUTES = 60     # readings this close together are one session
NURSES = {"Nurse Alvarez": "Program A", "Nurse Brooks": "Program B"}
OUTCOMES = ["device failure", "hospitalization", "disengagement", "unreachable", "resolved"]


def connect():
    """Create the database in memory, with the four tables from the design."""
    db = sqlite3.connect(":memory:")
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


# ------------------------------------------------------- Module 4: call log
def log_outcome(db, nurse, patient_id, outcome):
    """Save what the call found. Nurses can only log their own patients."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    row = db.execute("SELECT program FROM patients WHERE patient_id = ?",
                     (patient_id,)).fetchone()
    if row is None or row[0] != NURSES.get(nurse):
        raise PermissionError(f"{nurse} cannot log calls for {patient_id}")
    db.execute("INSERT INTO calls VALUES (?, ?, ?, ?)",
               (patient_id, outcome, nurse, datetime.now().isoformat(" ", "minutes")))


# ---------------------------------------------------------- synthetic data
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


def make_synthetic_data(as_of):
    """Return (enrollment, vendor rows, answer key). The seed is fixed, so
    every run makes the same 30 patients."""
    rng = random.Random(5910)
    kinds = [g[1:] for g in GROUPS for _ in range(g[0])]
    rng.shuffle(kinds)
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


# -------------------------------------------------------------------- demo
def main():
    as_of = datetime.now().replace(minute=0, second=0, microsecond=0)
    enrollment, vendor, answer_key = make_synthetic_data(as_of)
    db = connect()

    print("MODULE 1 - INGESTION (keeps patient, device and time only)")
    counts = ingest(db, vendor, load_roster(db, enrollment), as_of)
    for name, value in counts.items():
        print(f"  {name:<12}{value:>6,}")

    print("\nMODULE 2 - GAP SCORING (flag at 2 x the device's usual gap)")
    score_devices(db, as_of)
    scored, flagged = db.execute("SELECT COUNT(*), SUM(flagged) FROM scores").fetchone()
    print(f"  Devices scored: {scored}   Flagged: {flagged}")
    print("  Output check against the synthetic answer key:")
    print(f"  {'Situation':<15}{'Devices':>8}{'Flagged':>9}{'48-hour rule':>14}")
    results = check_answer_key(db, answer_key)
    for situation in ["Gone quiet", "Normal rhythm", "New patient"]:
        devices, ours, rule = results[situation]
        print(f"  {situation:<15}{devices:>8}{ours:>9}{rule:>14}")
    for patient, usual in db.execute(
            "SELECT patient_id, usual FROM scores WHERE flagged = 1 AND silent < 48"):
        print(f"  Missed by a 48-hour rule: {patient} (usually every {usual:.0f} hours)")

    print("\nMODULE 3 - OUTREACH QUEUE for Nurse Alvarez (Program A)")
    queue = outreach_queue(db, "Nurse Alvarez")
    for rank, (patient, device, silent, usual, score, reason) in enumerate(queue, 1):
        usual_text = f"{usual:5.1f} h" if usual else "   none"
        print(f"  {rank}. {patient} {device}  silent {silent:6.1f} h  "
              f"usual {usual_text}  score {score:4.1f}  {reason}")

    print("\nMODULE 4 - CALL LOG")
    log_outcome(db, "Nurse Alvarez", queue[0][0], "device failure")
    left = len(outreach_queue(db, "Nurse Alvarez"))
    print(f"  Logged a call for {queue[0][0]}. Patients left in the queue: {left}")
    try:
        db.execute("UPDATE calls SET outcome = 'resolved'")
    except sqlite3.DatabaseError as error:
        print(f"  Editing the call log: BLOCKED ({error})")
    other = outreach_queue(db, "Nurse Brooks")[0][0]
    try:
        log_outcome(db, "Nurse Alvarez", other, "resolved")
    except PermissionError as error:
        print(f"  Call for a Program B patient: REFUSED ({error})")
    db.close()


if __name__ == "__main__":
    main()
