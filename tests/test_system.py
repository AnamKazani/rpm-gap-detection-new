"""System-level tests for gap_detection.py (Unit 7), written with pytest.

The 21 unit tests in test_gap_detection.py check each module on its own.
This file tests the modules working together and the system as a whole:

  IT  integration tests   modules talking to each other and to the database
  ST  system tests        the running application tested from the outside (HTTP)
  AT  acceptance tests    the Unit 3 functional requirements, as a nurse would see them
  RT  regression tests    results from Units 5 and 6 must not change
  PT  performance tests   speed and stability (NFR-3)

Run:  python3 -m pytest -v
All data is synthetic.
"""
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import gap_detection as gd

AS_OF = datetime(2026, 10, 1, 9, 0)        # fixed time for results that must repeat


# ------------------------------------------------------------------ helpers
@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The whole system running: synthetic vendor file -> Modules 1 and 2 ->
    database -> web server. Returns (address, database, enrollment list)."""
    saved = gd.VENDOR_FILE, gd.PORT
    gd.VENDOR_FILE = tmp_path_factory.mktemp("data") / "vendor_readings.csv"
    as_of = datetime.now().replace(microsecond=0)
    enrollment, vendor, _ = gd.make_synthetic_data(as_of)
    write_vendor_file(vendor)
    db = gd.connect()
    gd.run_pipeline(db, enrollment, as_of)
    gd.PORT = 0                                       # any free port
    server = gd.start_server(db)
    yield f"http://127.0.0.1:{server.server_port}", db, enrollment
    server.shutdown()
    server.server_close()
    gd.VENDOR_FILE, gd.PORT = saved


def write_vendor_file(rows):
    import csv
    with open(gd.VENDOR_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sign_in(base, nurse="Nurse Alvarez"):
    return gd.call(base, "POST", "/api/login", {"nurse": nurse})[1]["token"]


def raw_post(base, path, data, token=None):
    """POST any bytes, even ones that are not valid JSON. Returns the status,
    or "no reply" if the server dropped the connection."""
    request = urllib.request.Request(base + path, method="POST", data=data,
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (ConnectionError, urllib.error.URLError):
        return "no reply"


def scored(as_of, scale):
    """Synthetic data of 30 x scale patients, ingested and scored."""
    enrollment, vendor, answer_key = gd.make_synthetic_data(as_of, scale)
    db = gd.connect()
    gd.ingest(db, vendor, gd.load_roster(db, enrollment), as_of)
    gd.score_devices(db, as_of)
    return db, answer_key


# -------------------------------------------------------- integration tests
class TestIntegration:
    def test_IT01_login_then_queue_in_score_order(self, app):
        base = app[0]
        status, queue = gd.call(base, "GET", "/api/queue", token=sign_in(base))
        scores = [p["score"] for p in queue["patients"]]
        assert status == 200 and len(scores) == 5
        assert scores == sorted(scores, reverse=True)

    def test_IT02_queue_shows_what_scoring_saved(self, app):
        base, db = app[0], app[1]
        _, queue = gd.call(base, "GET", "/api/queue", token=sign_in(base))
        for p in queue["patients"]:
            saved = db.execute("SELECT score, reason FROM scores WHERE patient_id = ?",
                               (p["patient_id"],)).fetchone()
            assert (round(saved[0], 1), saved[1]) == (p["score"], p["reason"])

    def test_IT03_logged_call_is_saved_and_leaves_queue(self, app):
        base, db = app[0], app[1]
        token = sign_in(base, "Nurse Brooks")
        first = gd.call(base, "GET", "/api/queue", token=token)[1]["patients"][0]
        status, reply = gd.call(base, "POST", "/api/calls",
                                {"patient_id": first["patient_id"], "outcome": "unreachable"}, token)
        assert status == 201
        assert db.execute("SELECT outcome FROM calls WHERE patient_id = ?",
                          (first["patient_id"],)).fetchone() == ("unreachable",)
        queue = gd.call(base, "GET", "/api/queue", token=token)[1]["patients"]
        assert first["patient_id"] not in [p["patient_id"] for p in queue]

    def test_IT04_failed_scoring_leaves_no_half_saved_data(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gd, "VENDOR_FILE", tmp_path / "vendor.csv")
        enrollment, vendor, _ = gd.make_synthetic_data(AS_OF)
        write_vendor_file(vendor)
        db = gd.connect()

        def broken(db, as_of):
            raise RuntimeError("scoring stopped")
        monkeypatch.setattr(gd, "score_devices", broken)
        with pytest.raises(RuntimeError):
            gd.run_pipeline(db, enrollment, AS_OF)
        assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0

    def test_IT05_reading_the_same_file_twice_adds_nothing(self):
        enrollment, vendor, _ = gd.make_synthetic_data(AS_OF)
        db = gd.connect()
        consented = gd.load_roster(db, enrollment)
        first = gd.ingest(db, vendor, consented, AS_OF)
        second = gd.ingest(db, vendor, consented, AS_OF)
        assert first["stored"] > 0 and second["stored"] == 0


# ------------------------------------------------------------- system tests
class TestSystem:
    def test_ST01_full_walkthrough_passes_all_checks(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(gd, "VENDOR_FILE", tmp_path / "vendor.csv")
        monkeypatch.setattr(gd, "PORT", 0)
        gd.main(keep_open=False)
        assert "Integration checks passed: 7 of 7" in capsys.readouterr().out

    def test_ST02_health_check_reports_version(self, app):
        status, reply = gd.call(app[0], "GET", "/api/health")
        assert status == 200 and reply == {"status": "ok", "version": gd.VERSION}

    def test_ST03_unknown_address_returns_404(self, app):
        assert gd.call(app[0], "GET", "/api/patients")[0] == 404

    def test_ST04_missing_or_fake_token_returns_401(self, app):
        assert gd.call(app[0], "GET", "/api/queue")[0] == 401
        assert gd.call(app[0], "GET", "/api/queue", token="not-a-real-token")[0] == 401

    def test_ST05_other_programs_patient_returns_403(self, app):
        base, db = app[0], app[1]
        other = gd.outreach_queue(db, "Nurse Brooks")[0][0]
        status, _ = gd.call(base, "POST", "/api/calls",
                            {"patient_id": other, "outcome": "resolved"}, sign_in(base))
        assert status == 403

    def test_ST06_body_that_is_not_a_json_object_returns_400(self, app):
        base = app[0]
        token = sign_in(base)
        assert raw_post(base, "/api/login", b"[]") == 400
        assert raw_post(base, "/api/calls", b"not json", token) == 400

    def test_ST07_call_without_patient_returns_400(self, app):
        base = app[0]
        status, _ = gd.call(base, "POST", "/api/calls", {"outcome": "resolved"}, sign_in(base))
        assert status == 400

    def test_ST08_times_with_a_time_zone_are_stored(self):
        as_of = datetime(2026, 10, 1, 9, 0)
        reading = (as_of - timedelta(hours=30)).astimezone(timezone.utc)   # e.g. ...+00:00
        assert gd.clean_time(reading.isoformat(), as_of) == (
            as_of - timedelta(hours=30)).isoformat(timespec="seconds")

    def test_ST09_patient_data_is_never_cached(self, app):
        base = app[0]
        request = urllib.request.Request(base + "/api/queue",
                                         headers={"Authorization": f"Bearer {sign_in(base)}"})
        with urllib.request.urlopen(request) as response:
            assert response.headers["Cache-Control"] == "no-store"


# --------------------------------------------------------- acceptance tests
class TestAcceptance:
    def test_AT01_FR2_only_three_fields_are_kept(self, app):
        db = app[1]
        columns = [c[1] for c in db.execute("PRAGMA table_info(readings)")]
        assert columns == ["patient_id", "device_id", "reading_time"]

    def test_AT02_FR3_no_consent_never_reaches_a_nurse(self, app):
        base, db, enrollment = app
        refused = {p["patient_id"] for p in enrollment if p["consent"] == "N"}
        stored = {p for (p,) in db.execute("SELECT patient_id FROM readings")}
        shown = set()
        for nurse in gd.NURSES:
            queue = gd.call(base, "GET", "/api/queue", token=sign_in(base, nurse))[1]
            shown |= {p["patient_id"] for p in queue["patients"]}
        assert refused and not (refused & stored) and not (refused & shown)

    def test_AT03_FR5_every_row_explains_itself(self, app):
        base = app[0]
        queue = gd.call(base, "GET", "/api/queue", token=sign_in(base))[1]["patients"]
        for p in queue:
            assert p["reason"] in ("SILENCE_EXCEEDS_BASELINE", "INSUFFICIENT_HISTORY")
            assert p["explanation"].startswith(("Silent", "New device"))

    def test_AT04_FR6_each_nurse_sees_only_her_program(self, app):
        base, db = app[0], app[1]
        for nurse, program in gd.NURSES.items():
            queue = gd.call(base, "GET", "/api/queue", token=sign_in(base, nurse))[1]
            for p in queue["patients"]:
                assert db.execute("SELECT program FROM patients WHERE patient_id = ?",
                                  (p["patient_id"],)).fetchone()[0] == program

    def test_AT05_FR7_a_call_can_be_logged_only_once(self, app):
        base = app[0]
        token = sign_in(base)
        patient = gd.call(base, "GET", "/api/queue", token=token)[1]["patients"][0]["patient_id"]
        body = {"patient_id": patient, "outcome": "resolved"}
        assert gd.call(base, "POST", "/api/calls", body, token)[0] == 201
        assert gd.call(base, "POST", "/api/calls", body, token)[0] == 409

    @pytest.mark.xfail(reason="FR-8 cohort report is not built yet (planned for a later version)",
                       strict=True)
    def test_AT06_FR8_cohort_report_exists(self):
        assert hasattr(gd, "cohort_report")


# --------------------------------------------------------- regression tests
class TestRegression:
    def test_RT01_accuracy_on_240_patients_is_unchanged(self):
        db, answer_key = scored(AS_OF, scale=8)
        rows = db.execute("SELECT patient_id, silent, flagged FROM scores").fetchall()
        should = [answer_key[p] in ("Gone quiet", "New patient") for p, _, _ in rows]
        for flags, alarms_expected, missed_expected in (
                ([bool(f) for _, _, f in rows], 0, 0),            # personal baseline
                ([s >= 48 for _, s, _ in rows], 40, 8)):          # fixed 48-hour rule
            alarms = sum(d and not s for s, d in zip(should, flags))
            missed = sum(s and not d for s, d in zip(should, flags))
            assert (len(rows), alarms, missed) == (224, alarms_expected, missed_expected)

    def test_RT02_detection_times_are_unchanged(self):
        db, answer_key = scored(AS_OF, scale=8)
        kinds, _ = gd.synthetic_kinds(scale=8)
        every = {f"P-{1000 + n}": k[1] for n, k in enumerate(kinds, 1)}
        hours = {}
        for patient, device, enrolled in db.execute(
                "SELECT patient_id, device_id, enrolled_on FROM patients").fetchall():
            if answer_key[patient] == "Gone quiet":
                times = [datetime.fromisoformat(t) for (t,) in db.execute(
                    "SELECT reading_time FROM readings WHERE patient_id = ?", (patient,))]
                first = next(h for h in range(1, 2000) if gd.score_device(
                    times, max(times) + timedelta(hours=h),
                    datetime.fromisoformat(enrolled))["flagged"])
                hours.setdefault(every[patient], []).append(first)
        medians = {k: statistics.median(v) for k, v in hours.items()}
        assert (medians[12], medians[24], round(medians[72])) == (25, 49, 142)


# -------------------------------------------------------- performance tests
class TestPerformance:
    def test_PT01_NFR3_4800_patients_scored_well_within_an_hour(self):
        now = datetime.now()
        enrollment, vendor, _ = gd.make_synthetic_data(now, scale=160)
        db = gd.connect()
        start = time.perf_counter()
        gd.ingest(db, vendor, gd.load_roster(db, enrollment), now)
        gd.score_devices(db, now)
        seconds = time.perf_counter() - start
        print(f"\n    4,800 patients, {len(vendor):,} rows: ingested and scored in {seconds:.2f} s")
        assert seconds < 60

    def test_PT02_queue_answers_95_percent_within_200_ms(self, app):
        base = app[0]
        token, times = sign_in(base), []
        for _ in range(100):
            start = time.perf_counter()
            gd.call(base, "GET", "/api/queue", token=token)
            times.append((time.perf_counter() - start) * 1000)
        times.sort()
        print(f"\n    queue API: median {statistics.median(times):.1f} ms, "
              f"95th percentile {times[94]:.1f} ms")
        assert times[94] < 200

    def test_PT03_50_requests_at_once_all_succeed(self, app):
        base = app[0]
        token = sign_in(base)
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(
                lambda _: gd.call(base, "GET", "/api/queue", token=token)[0], range(50)))
        assert statuses == [200] * 50
