# rpm-gap-detection

MSIT 5910 Capstone Project (University of the People): early detection of
transmission gaps in remote patient monitoring (RPM).

When a patient's home monitoring device stops sending readings, it can go
unnoticed until the monthly review. This system compares each patient's
silence with that patient's own usual gap between readings, instead of one
fixed time limit, and puts the patients who need a call into a ranked
outreach queue for monitoring nurses. Every flag comes with a reason code.

All data is synthetic. No real patient data is used anywhere.

## Repository layout

| Folder    | Contents                                                  |
|-----------|-----------------------------------------------------------|
| `src/`    | Application code (`gap_detection.py`)                     |
| `tests/`  | Unit tests (pytest)                                       |
| `design/` | Architecture diagram (Figure 1 in the Unit 3 report)      |
| `docs/`   | Requirements and design documents                         |

## Run the system

```
python3 src/gap_detection.py        # on Windows: python src\gap_detection.py
```

The program uses only Python's standard library, so there is nothing to
install. It makes its synthetic data and database in memory each time.

## Run the tests

The tests need pytest and pytest-cov (one-time install):

```
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -v                # every test, grouped as black-box or white-box
python3 -m pytest -v --cov          # with statement and branch coverage
```

## Branches

| Branch       | Purpose                                                        |
|--------------|----------------------------------------------------------------|
| `main`       | Released versions only. Every release is tagged.               |
| `development`| Integration branch. Finished feature branches merge here.      |
| `feature/*`  | One branch per piece of work, created from `development`.      |

A feature branch is merged into `development` only when every test passes.
`development` is merged into `main` when a milestone is complete, and that
merge is tagged with a version number. See `CHANGELOG.md` for the releases.

## Releases

| Tag      | Milestone                                   |
|----------|---------------------------------------------|
| `v0.1.0` | Initial development (Unit 4 working system) |
| `v0.2.0` | Core logic and unit testing (Unit 5)        |
