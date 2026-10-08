"""Unit tests for gap_detection.py, written with pytest.

TestBlackBox: written from the requirements only (inputs and expected outputs).
TestWhiteBox: written from the code's branches.
Each test name starts with its test case ID.
"""
from datetime import datetime, timedelta

import pytest

import gap_detection as gd

AS_OF = datetime(2026, 10, 1, 9, 0)      # a fixed time, so no result depends on the clock
ENROLLED = AS_OF - timedelta(days=45)


@pytest.fixture(autouse=True)
def close_databases(monkeypatch):
    """Close every database a test opens."""
    opened, real_connect = [], gd.connect
    monkeypatch.setattr(gd, "connect", lambda: opened.append(real_connect()) or opened[-1])
    yield
    for db in opened:
        db.close()


def every(hours, count, hours_ago):
    """`count` readings `hours` apart, the latest one `hours_ago` before AS_OF."""
    last = AS_OF - timedelta(hours=hours_ago)
    return [last - timedelta(hours=hours * i) for i in range(count)]


def scored_db(devices):
    """In-memory database with enrolled devices and their readings, scored.
    devices: (patient, device, program, reading times)"""
    db = gd.connect()
    for patient, device, program, times in devices:
        db.execute("INSERT INTO patients VALUES (?, ?, ?, ?)",
                   (patient, device, program, ENROLLED.isoformat()))
        db.executemany("INSERT INTO readings VALUES (?, ?, ?)",
                       [(patient, device, t.isoformat()) for t in times])
    gd.score_devices(db, AS_OF)
    return db


def vendor(patient, time):
    return {"patient_id": patient, "device_id": "D-" + patient, "reading_time": time,
            "pulse": 72}


class TestBlackBox:
    def test_S01_daily_device_quiet_10h_is_normal(self):
        s = gd.score_device(every(24, 40, 10), AS_OF, ENROLLED)
        assert not s["flagged"] and s["reason"] == "WITHIN_PERSONAL_PATTERN"

    def test_S02_quiet_two_usual_gaps_is_flagged(self):
        s = gd.score_device(every(24, 40, 48), AS_OF, ENROLLED)
        assert s["score"] == pytest.approx(2.0) and s["flagged"]

    def test_S03_quiet_47h_is_not_flagged(self):
        assert not gd.score_device(every(24, 40, 47), AS_OF, ENROLLED)["flagged"]

    def test_S04_rural_device_quiet_3_5_days_is_normal(self):
        assert not gd.score_device(every(72, 14, 84), AS_OF, ENROLLED)["flagged"]

    def test_S05_twice_daily_device_quiet_30h_is_flagged(self):
        assert gd.score_device(every(12, 80, 30), AS_OF, ENROLLED)["flagged"]

    def test_S06_needs_a_full_day_of_silence(self):
        assert not gd.score_device(every(6, 100, 23), AS_OF, ENROLLED)["flagged"]
        assert gd.score_device(every(6, 100, 24), AS_OF, ENROLLED)["flagged"]

    def test_S07_burst_of_readings_is_one_session(self):
        cuff = [t + timedelta(minutes=m) for t in every(24, 30, 30) for m in range(3)]
        assert gd.usual_gap(cuff) == pytest.approx(24, abs=0.1)

    def test_I01_only_consented_patients_and_3_fields(self):
        db = gd.connect()
        counts = gd.ingest(db, [vendor("P-1", "2026-09-30T08:00:00"),
                                vendor("P-2", "2026-09-30T08:00:00")], {"P-1"}, AS_OF)
        assert counts["no_consent"] == 1
        assert db.execute("SELECT * FROM readings").fetchall() == [
            ("P-1", "D-P-1", "2026-09-30T08:00:00")]

    def test_I02_unreadable_or_future_time_not_stored(self):
        db = gd.connect()
        gd.ingest(db, [vendor("P-1", "09/30/2026"), vendor("P-1", "2026-10-03T08:00:00")],
                  {"P-1"}, AS_OF)
        assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0

    def test_Q01_own_program_only_highest_score_first(self):
        db = scored_db([("P-A1", "D-A1", "Program A", every(24, 30, 120)),
                        ("P-A2", "D-A2", "Program A", every(24, 30, 60)),
                        ("P-A3", "D-A3", "Program A", every(24, 30, 5)),
                        ("P-B1", "D-B1", "Program B", every(24, 30, 96))])
        assert [r[0] for r in gd.outreach_queue(db, "Nurse Alvarez")] == ["P-A1", "P-A2"]

    def test_Q02_unknown_user_gets_empty_queue(self):
        db = scored_db([("P-A1", "D-A1", "Program A", every(24, 30, 120))])
        assert gd.outreach_queue(db, "Nobody") == []

    def test_L01_cannot_log_call_for_other_program(self):
        db = scored_db([("P-B1", "D-B1", "Program B", every(24, 30, 96))])
        with pytest.raises(PermissionError):
            gd.log_outcome(db, "Nurse Alvarez", "P-B1", "resolved")

    def test_L02_unknown_outcome_refused(self):
        db = scored_db([("P-A1", "D-A1", "Program A", every(24, 30, 120))])
        with pytest.raises(ValueError):
            gd.log_outcome(db, "Nurse Alvarez", "P-A1", "fixed itself")

    def test_E01_flags_match_synthetic_answer_key(self):
        enrollment, rows, answer_key = gd.make_synthetic_data(AS_OF)
        db = gd.connect()
        counts = gd.ingest(db, rows, gd.load_roster(db, enrollment), AS_OF)
        gd.score_devices(db, AS_OF)
        results = gd.check_answer_key(db, answer_key)
        assert counts["stored"] == 1045
        assert results["Gone quiet"] == [7, 7, 6]       # devices, flagged, 48-hour rule
        assert results["Normal rhythm"] == [20, 0, 5]


class TestWhiteBox:
    def test_W01_no_readings_counts_from_enrollment(self):
        s = gd.score_device([], AS_OF, AS_OF - timedelta(days=3))
        assert s["silent"] == pytest.approx(72) and s["reason"] == "INSUFFICIENT_HISTORY"

    def test_W02_pattern_needs_five_gaps(self):
        assert gd.usual_gap(every(24, 6, 5)) == pytest.approx(24)
        assert gd.usual_gap(every(24, 5, 5)) is None

    def test_W03_reading_after_as_of_is_ignored(self):
        times = every(24, 40, 100) + [AS_OF + timedelta(days=2)]
        assert gd.score_device(times, AS_OF, ENROLLED)["flagged"]

    def test_W04_quiet_device_beside_working_one(self):
        db = scored_db([("P-1", "SCALE", "Program A", every(24, 30, 5)),
                        ("P-1", "CUFF", "Program A", every(24, 30, 120))])
        flags = dict(db.execute("SELECT device_id, flagged FROM scores"))
        assert flags == {"SCALE": 0, "CUFF": 1}

    def test_W05_resent_reading_stored_once(self):
        db = gd.connect()
        counts = gd.ingest(db, [vendor("P-1", "2026-09-30T08:00:00")] * 2, {"P-1"}, AS_OF)
        assert counts["duplicates"] == 1

    def test_W06_logged_call_leaves_queue_and_cannot_change(self):
        db = scored_db([("P-A1", "D-A1", "Program A", every(24, 30, 120))])
        gd.log_outcome(db, "Nurse Alvarez", "P-A1", "device failure")
        assert gd.outreach_queue(db, "Nurse Alvarez") == []
        with pytest.raises(gd.sqlite3.DatabaseError):
            db.execute("UPDATE calls SET outcome = 'resolved'")

    def test_W07_demo_runs_to_the_end(self, capsys):
        gd.main()
        assert "BLOCKED" in capsys.readouterr().out
