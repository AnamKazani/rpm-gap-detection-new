# Changelog

Each release is a merge of `development` into `main`, tagged with its version.

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
