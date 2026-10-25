# Early Detection of Transmission Gaps in Remote Patient Monitoring

**MSIT 5910 Capstone Project · Anam Kazani · University of the People**

This system notices when a patient's home monitoring device has gone quiet for longer than usual for that patient. It then puts that patient on a ranked call list for nurses, with a plain reason beside each name.

## The Problem

Most monitoring programs flag a patient after a fixed number of days without a reading. That treats every patient the same. People whose devices normally send every few days get flagged over and over, and the truly silent devices get lost in the noise. This project compares each device with its own usual gap instead. A device's score is its hours of silence divided by its usual gap, and it is flagged once the score reaches 2.0.

## How It Works

The final version (0.4.0) is one Python file that uses only the standard library (SQLite and http.server). It has four modules:

1. **Ingestion** reads the vendor file, keeps only the patient, device and time fields, and drops vital signs before anything is stored.
2. **Gap scoring** learns each device's usual rhythm over 30 days and scores current silence against it.
3. **Queue and dashboard** serve a ranked call list through a web API, and the server makes sure each nurse sees only their own program.
4. **Call logging** records each call outcome once, so results can be reviewed and checked for fairness.

![Architecture diagram showing vendor files flowing into ingestion, gap scoring, the queue API and nurse dashboard, with call outcomes saved to a log](design/Unit3.drawio%20(1).png)

## Results

The evaluation used 240 synthetic patients. No real patient data was used, and the project makes no clinical outcome claims.

| Measure | Personal baseline | Fixed 48-hour rule |
|---|---|---|
| Precision | 1.00 | 0.58 |
| Recall | 1.00 | 0.88 |
| False alarms | 0 | 40 |
| Missed devices | 0 | 8 |

The trade-off is slower detection for devices that normally send every three days, with a median of 142 hours. System testing with 46 test cases found four defects, and all four were fixed in version 0.4.0. Now 45 tests pass, and 1 is an expected failure for the cohort report, which is planned work.

## Deliverables

- [Source code](https://github.com/AnamKazani/rpm-gap-detection/tree/main/src)
- [Automated tests](https://github.com/AnamKazani/rpm-gap-detection/tree/main/tests)
- [Design documents and architecture diagram](https://github.com/AnamKazani/rpm-gap-detection/tree/main/design)
- [Final capstone report](https://github.com/AnamKazani/rpm-gap-detection/tree/main/docs)
- [Version history](https://github.com/AnamKazani/rpm-gap-detection/blob/main/CHANGELOG.md)
- [Releases](https://github.com/AnamKazani/rpm-gap-detection/releases)

## Run It

You need Python 3.10 or newer. Nothing else has to be installed to run the walkthrough.

```
git clone https://github.com/AnamKazani/rpm-gap-detection.git
cd rpm-gap-detection
python3 src/gap_detection.py
```

The walkthrough steps through each module and then opens the nurse dashboard at http://127.0.0.1:8000. To run the tests, install pytest and run `python3 -m pytest -v`.

## Contact

GitHub: [AnamKazani](https://github.com/AnamKazani)
