Application code.

`gap_detection.py` holds the four modules from the architecture diagram, in
the same order: ingestion, gap scoring, outreach queue and call log. The
scoring rule is two small functions, `usual_gap()` and `score_device()`,
that take a list of reading times and an as-of time, so they can be tested
on their own. The synthetic data and the demo are at the bottom of the file.
