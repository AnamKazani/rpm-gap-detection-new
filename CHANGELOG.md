# Changelog

Each release is a merge of `development` into `main`, tagged with its version.

## v0.4.0 - System testing and maintenance (Unit 7)

System testing
- 25 new tests in `tests/test_system.py` covering integration, system,
  acceptance, regression and performance testing. Together with the 21 unit
  tests, that makes 46 tests.
- The first run on v0.3.0 had 4 failures, which pointed to 4 defects, all fixed:
  - D1: A request body that was not a JSON object crashed the request. The API
    now answers 400.
  - D2: Logging a call with no patient ID returned 403 instead of 400. A
    patient ID is now required.
  - D3: Reading times with a time zone (such as +00:00) were rejected. They
    are now converted to local time.
  - D4: The same call could be logged twice. A second call now returns 409.
- The dashboard now escapes apostrophes in patient IDs.
- Result: 45 tests pass, and 1 is marked as an expected failure because the
  FR-8 cohort report has not been built yet.
- The regression tests confirm the Unit 6 results are unchanged.

## v0.3.0 - Integration and evaluation (Unit 6)

- All four modules joined into one running application in a single Python
  file using only the standard library: SQLite for storage and http.server
  for the web API and nurse dashboard.
- A walkthrough steps through each module and then starts the dashboard.
- Evaluation on 240 synthetic patients: the personal baseline flagged all 64
  devices that needed a call with no false alarms (precision and recall 1.00).
  A fixed 48-hour rule raised 40 false alarms and missed 8 devices.
- Runtime settings such as the address, port and flag threshold are read from
  environment variables.

## v0.2.0 - Core logic and unit testing (Unit 5)

Core logic
- Shorter, simpler code. The scoring rule is two functions, `usual_gap()`
  and `score_device()`, that take reading times and an as-of time.
- Output validation against the synthetic answer key: all 7 devices built to
  go quiet are flagged and none of the 20 normal devices are. A fixed 48-hour
  rule would flag 5 normal devices and miss 1.

Unit testing
- 21 pytest tests (black-box and white-box) across all four modules, with
  100% statement and branch coverage.
- The first run had 6 failures, which pointed to 4 problems, all fixed:
  - Ingestion now rejects missing, unreadable and future reading times.
  - Scoring is per device, so a working device cannot hide a quiet one.
  - Readings less than 60 minutes apart count as one session.
  - Unknown users get an empty queue, and a nurse cannot log a call for a
    patient outside her program.

## v0.1.0 - Initial development (Unit 4)

- First working version, shown in the Unit 4 demonstration video.
- One Python file using only the standard library, running all four modules
  on synthetic data: ingestion, gap scoring, outreach queue and call log.
