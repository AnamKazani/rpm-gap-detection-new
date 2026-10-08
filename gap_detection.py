"""
Transmission-Gap Detection for Remote Patient Monitoring
MSIT 5910 Capstone - initial version

Finds patients whose home monitoring device has gone quiet compared with their
OWN usual rhythm, and puts them in a ranked outreach queue for nurses.

Run it with:   python gap_detection.py

Only Python's standard library is used, so there is nothing to install.
All patient data is synthetic (made up by this program).

The four modules match the architecture diagram:
    Module 1  ingest()           keep 3 fields, skip patients without consent
    Module 2  score_patients()   compare silence with each patient's usual gap
    Module 3  outreach_queue()   ranked list, filtered to the nurse's program
    Module 4  log_outcome()      record what the call found (cannot be edited)
"""
import csv
import random
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ------------------------------------------------------------------ settings
DATA = Path(__file__).resolve().parent / "data"
DB_FILE = DATA / "rpm_gap.db"
VENDOR_FILE = DATA / "vendor_export.csv"
ENROLLMENT_FILE = DATA / "enrollment.csv"

ALLOWED_FIELDS = ["patient_id", "device_id", "reading_time"]  # the only fields kept
BASELINE_DAYS = 30       # learn each patient's rhythm from the last 30 days
MIN_GAPS = 5             # fewer gaps than this means no pattern yet
DEFAULT_GAP_HOURS = 24   # used until a patient has a pattern
FLAG_AT = 2.0            # flag when the silence is twice the usual gap...
MIN_SILENT_HOURS = 24    # ...and at least a full day
FIXED_RULE_HOURS = 48    # a one-size-fits-all rule, used only for comparison

NURSES = {"Nurse Alvarez": ["Program A"], "Nurse Brooks": ["Program B"]}
OUTCOMES = ["device failure", "hospitalization", "disengagement", "unreachable", "resolved"]
NOW = datetime.now().replace(minute=0, second=0, microsecond=0)


# ------------------------------------------------------- database connection
def connect():
    """Connect to the SQLite database file and create fresh tables."""
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.executescript("""
        DROP TABLE IF EXISTS transmissions;
        DROP TABLE IF EXISTS patients;
        DROP TABLE IF EXISTS scores;
        DROP TABLE IF EXISTS dispositions;

        CREATE TABLE transmissions (patient_id TEXT, device_id TEXT, reading_time TEXT,
                                    PRIMARY KEY (patient_id, device_id, reading_time));
        CREATE TABLE patients (patient_id TEXT PRIMARY KEY, device_id TEXT, program TEXT,
                               connectivity TEXT, enrolled_on TEXT);
        CREATE TABLE scores (patient_id TEXT PRIMARY KEY, silent_hours REAL,
                             usual_gap_hours REAL, score REAL, reason TEXT,
                             flagged INTEGER, fixed_rule_flag INTEGER);
        CREATE TABLE dispositions (id INTEGER PRIMARY KEY, patient_id TEXT, outcome TEXT,
                                   nurse TEXT, logged_at TEXT);

        -- Call outcomes are an audit trail: rows can be added, never changed.
        CREATE TRIGGER no_edits BEFORE UPDATE ON dispositions
        BEGIN SELECT RAISE(ABORT, 'call outcomes cannot be changed'); END;
        CREATE TRIGGER no_deletes BEFORE DELETE ON dispositions
        BEGIN SELECT RAISE(ABORT, 'call outcomes cannot be deleted'); END;
    """)
    return db


# --------------------------------------------- Module 1: ingestion service
def load_roster(db):
    """Read the enrollment file and keep only patients who gave consent."""
    with open(ENROLLMENT_FILE, newline="") as f:
        everyone = list(csv.DictReader(f))
    consented = [p for p in everyone if p["consent"] == "Y"]
    db.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?)",
                   [(p["patient_id"], p["device_id"], p["program"], p["connectivity"],
                     p["enrolled_on"]) for p in consented])
    db.commit()
    return {p["patient_id"] for p in consented}, len(everyone) - len(consented)


def ingest(db, consented):
    """Copy only the three allowed fields, only for consented patients.
    Returns (readings in the file, skipped for no consent, newly stored)."""
    before = db.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
    total = skipped = 0
    with open(VENDOR_FILE, newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            if row["patient_id"] not in consented:
                skipped += 1                                  # never stored
                continue
            values = [row[field] for field in ALLOWED_FIELDS]  # 3 fields only
            # The key is patient + device + time, so a reading is never stored twice.
            db.execute("INSERT OR IGNORE INTO transmissions VALUES (?, ?, ?)", values)
    db.commit()
    after = db.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
    return total, skipped, after - before


# ------------------------------------- Module 2: gap detection and scoring
def score_patients(db):
    """Compare each patient's silence with that patient's own usual gap."""
    patients = db.execute("SELECT patient_id, enrolled_on FROM patients").fetchall()
    for patient_id, enrolled_on in patients:
        readings = sorted(datetime.fromisoformat(t) for (t,) in db.execute(
            "SELECT reading_time FROM transmissions WHERE patient_id = ?", (patient_id,)))
        last = readings[-1] if readings else datetime.fromisoformat(enrolled_on)
        silent = (NOW - last).total_seconds() / 3600

        # Usual gap = median time between readings in the 30 days before the last one.
        recent = [t for t in readings if t >= last - timedelta(days=BASELINE_DAYS)]
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(recent, recent[1:])]
        has_pattern = len(gaps) >= MIN_GAPS
        usual = statistics.median(gaps) if has_pattern else DEFAULT_GAP_HOURS

        score = silent / usual          # how many usual gaps have passed in silence
        flagged = score >= FLAG_AT and silent >= MIN_SILENT_HOURS
        if not has_pattern:
            reason = "INSUFFICIENT_HISTORY"
        elif flagged:
            reason = "SILENCE_EXCEEDS_BASELINE"
        else:
            reason = "WITHIN_PERSONAL_PATTERN"

        db.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (patient_id, silent, usual if has_pattern else None, score, reason,
                    flagged, silent >= FIXED_RULE_HOURS))
    db.commit()


# ----------------------------------------------- Module 3: outreach queue
def outreach_queue(db, nurse, program=None):
    """Ranked queue for one nurse. The program filter comes from the nurse's
    assignment and is applied inside the SQL query. Asking for a program the
    nurse isn't assigned to returns nothing."""
    allowed = [p for p in NURSES[nurse] if program in (None, p)]
    if not allowed:
        return []
    marks = ", ".join("?" * len(allowed))
    return db.execute(f"""
        SELECT s.patient_id, s.silent_hours, s.usual_gap_hours, s.score, s.reason
        FROM scores s JOIN patients p ON p.patient_id = s.patient_id
        WHERE s.flagged = 1
          AND p.program IN ({marks})
          AND s.patient_id NOT IN (SELECT patient_id FROM dispositions)
        ORDER BY s.score DESC""", allowed).fetchall()


# ------------------------------------------ Module 4: disposition logging
def log_outcome(db, nurse, patient_id, outcome):
    """Record what the outreach call found. This takes the patient off the queue."""
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown outcome: {outcome}")
    db.execute("INSERT INTO dispositions (patient_id, outcome, nurse, logged_at) "
               "VALUES (?, ?, ?, ?)",
               (patient_id, outcome, nurse, datetime.now().isoformat(" ", "minutes")))
    db.commit()


# ------------------------------------------------------------ synthetic data
# Stands in for the vendor platform and the enrollment system. Each group is one
# kind of patient: (how many, program, connection, usual hours between readings,
# hours silent right now, days of history, consent)
GROUPS = [
    (7, "Program A", "Broadband",      24, (2, 26),    45, "Y"),  # daily, normal
    (8, "Program B", "Cellular",       24, (2, 26),    45, "Y"),  # daily, normal
    (3, "Program A", "Rural cellular", 72, (52, 84),   45, "Y"),  # every 3 days, normal
    (2, "Program B", "Rural cellular", 72, (52, 84),   45, "Y"),  # every 3 days, normal
    (2, "Program A", "Cellular",       24, (80, 230),  45, "Y"),  # daily, gone quiet
    (3, "Program B", "Broadband",      24, (80, 230),  45, "Y"),  # daily, gone quiet
    (1, "Program A", "Rural cellular", 72, (230, 260), 45, "Y"),  # every 3 days, gone quiet
    (1, "Program A", "Broadband",      12, (28, 34),   45, "Y"),  # twice a day, gone quiet
    (1, "Program A", "Cellular",       24, (50, 58),    4, "Y"),  # new patient
    (1, "Program A", "Cellular",       24, (2, 26),    45, "N"),  # no consent
    (1, "Program B", "Broadband",      24, (2, 26),    45, "N"),  # no consent
]


def make_synthetic_data():
    rng = random.Random(5910)
    kinds = [group[1:] for group in GROUPS for _ in range(group[0])]
    rng.shuffle(kinds)
    enrollment, readings = [], []
    for n, (program, connection, every, silent, history, consent) in enumerate(kinds, 1):
        patient, device = f"P-{1000 + n}", f"DEV-{4000 + n}"
        enrolled = NOW - timedelta(days=history)
        enrollment.append([patient, device, program, connection,
                           enrolled.isoformat(timespec="seconds"), consent])
        t = NOW - timedelta(hours=rng.uniform(*silent))        # the latest reading
        while t > enrolled:
            readings.append((patient, device, t.isoformat(timespec="seconds")))
            t -= timedelta(hours=max(every / 2, rng.gauss(every, every / 10)))
    readings += rng.sample(readings, len(readings) // 100)    # vendor resends a few
    readings.sort(key=lambda r: r[2])

    DATA.mkdir(exist_ok=True)
    with open(ENROLLMENT_FILE, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["patient_id", "device_id", "program", "connectivity",
                      "enrolled_on", "consent"])
        out.writerows(enrollment)
    with open(VENDOR_FILE, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["record_id", "patient_id", "device_id", "reading_time", "systolic",
                      "diastolic", "pulse", "weight_lb", "battery_pct", "signal_dbm"])
        for i, (patient, device, time) in enumerate(readings, 1):
            out.writerow([f"R-{i:05d}", patient, device, time, rng.randint(110, 165),
                          rng.randint(65, 98), rng.randint(56, 100),
                          round(rng.uniform(120, 280), 1), rng.randint(15, 100),
                          rng.randint(-112, -58)])


# ------------------------------------------------------------------ the demo
def describe(hours):
    """30 -> '30 hours', 80 -> '3.3 days'"""
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def line(label, value, note=""):
    print(f"  {label} {'.' * (34 - len(label))} {value}{'   ' + note if note else ''}")


def pause(next_step):
    input(f"\nPress Enter to run {next_step}...")
    print()


def main():
    print("=" * 72)
    print("  TRANSMISSION-GAP DETECTION   initial version   synthetic data only")
    print("=" * 72)

    make_synthetic_data()
    db = connect()
    tables = [name for (name,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
    print("\nSETUP")
    print(f"  Python {sys.version.split()[0]}, standard library only (nothing to install)")
    print(f"  Synthetic data written to data/{VENDOR_FILE.name} and data/{ENROLLMENT_FILE.name}")
    print(f"  Connected to database data/{DB_FILE.name} (SQLite {sqlite3.sqlite_version})")
    print(f"  Tables: {', '.join(tables)}")
    pause("Module 1 (ingestion)")

    # Module 1
    print("MODULE 1 - INGESTION")
    with open(VENDOR_FILE, newline="") as f:
        columns = next(csv.reader(f))
    print(f"  Vendor file has {len(columns)} columns:")
    print(f"    {', '.join(columns[:6])},")
    print(f"    {', '.join(columns[6:])}")
    print(f"  Kept only 3: {', '.join(ALLOWED_FIELDS)}")
    consented, excluded = load_roster(db)
    total, skipped, stored = ingest(db, consented)
    _, _, stored_again = ingest(db, consented)
    line("Readings in the vendor file", f"{total:,}")
    line("Skipped, patient has no consent", skipped, f"({excluded} patients)")
    line("Duplicates skipped", total - skipped - stored)
    line("Stored in the database", f"{stored:,}")
    line("Ran ingestion again, new rows", stored_again)
    pause("Module 2 (gap scoring)")

    # Module 2
    print("MODULE 2 - GAP SCORING")
    print("  Usual gap = median time between this patient's readings over 30 days.")
    print(f"  Score = hours silent / usual gap. Flagged at {FLAG_AT:.0f} or more.")
    score_patients(db)
    rows = db.execute("SELECT s.*, p.connectivity FROM scores s "
                      "JOIN patients p USING (patient_id)").fetchall()
    flagged = [r for r in rows if r[5]]
    fixed = [r for r in rows if r[6]]
    fixed_only = [r for r in fixed if not r[5]]
    ours_only = [r for r in flagged if not r[6]]
    print()
    line("Patients scored", len(rows))
    line("Flagged for a call", len(flagged))
    line("Scores without a reason code", sum(1 for r in rows if not r[4]))
    print("\n  The same patients under a fixed 48-hour rule:")
    line("  Flagged by the fixed rule", len(fixed))
    kinds = Counter(r[7] for r in fixed_only)
    line("  ...but normal for that patient", len(fixed_only),
         "(" + ", ".join(f"{n} {kind.lower()}" for kind, n in kinds.items()) + ")")
    line("  Missed by the fixed rule", len(ours_only),
         "(" + ", ".join(f"{r[0]} usually sends every {describe(r[2])}"
                         for r in ours_only) + ")")
    example = max(fixed_only, key=lambda r: r[1])
    print(f"\n  Example: {example[0]} ({example[7].lower()}) has been silent "
          f"{describe(example[1])}, but usually")
    print(f"  sends every {describe(example[2])}, so this is normal for them. Not flagged.")
    pause("Module 3 (outreach queue)")

    # Module 3
    nurse = "Nurse Alvarez"
    print(f"MODULE 3 - OUTREACH QUEUE   {nurse} ({', '.join(NURSES[nurse])})")
    queue = outreach_queue(db, nurse)
    print(f"  {'Rank':<5} {'Patient':<8} {'Silent for':<11} {'Usual gap':<15} "
          f"{'Score':>5}  Reason")
    for rank, (patient, silent, usual, score, reason) in enumerate(queue, 1):
        gap = f"every {describe(usual)}" if usual else "no pattern yet"
        print(f"  {rank:<5} {patient:<8} {describe(silent):<11} {gap:<15} "
              f"{score:>4.1f}x  {reason}")
    print()
    line(f"{nurse} asks for Program B", len(outreach_queue(db, nurse, "Program B")),
         "patients returned")
    line("Nurse Brooks' queue (Program B)", len(outreach_queue(db, "Nurse Brooks")),
         "patients")
    pause("Module 4 (call outcome)")

    # Module 4
    print("MODULE 4 - CALL OUTCOME")
    first = queue[0][0]
    log_outcome(db, nurse, first, "device failure")
    print(f"  {nurse} called {first} and logged: device failure")
    line("Program A queue", f"{len(queue)} open -> {len(outreach_queue(db, nurse))} open")
    try:
        db.execute("UPDATE dispositions SET outcome = 'resolved'")
        result = "changed (this should not happen)"
    except sqlite3.DatabaseError as error:
        result = f"BLOCKED: {error}"
    line("Trying to change that record", result)

    print(f"\nDone. Everything above is stored in data/{DB_FILE.name}.")
    db.close()


if __name__ == "__main__":
    main()
