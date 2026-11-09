# Design notes

## The failure this catches
An AI summary can contain only *real* numbers and still be wrong: the 24-hour figure described as "72 hours". Checking that a number exists in the data is not enough; it must be attached to the right metric and window.

## Rules (narrator.verify / narrator.annotate share one implementation of them)
1. Every numeric claim must equal a computed fact (1% tolerance; K/M/B and % normalised).
2. The fact it matches must be one the *clause* is about. Each fact has a cue pattern (`72 hours`, `engagement`, `percentile`, ...). A clause is the text between `, ; ( )` or sentence ends. If the clause has cues and none belong to the matching fact, the number is **misattributed**.
3. No unhedged causal language ("proves", "caused by") and no hype ("exploded"). Negated forms ("does not prove") pass.
4. Window labels ("72 hours", "7-day") and bare years are not claims. Titles and logged event labels are masked (a title like "Apollo 13" must not count as a number).

## Honest limits
Attribution is a heuristic keyed on metric cues in the same clause. If one clause names several metrics, a value matching any of them passes, so keep one metric per clause. Custom facts need a `cue` regex in the facts file; without one they get existence, hype and causation checks only (the CLI says so). It catches the swapped-window class of error, not arbitrary semantic mistakes. Treat it as a floor, not a guarantee. Measure it: `golden gen`, label ~40 items blind, `golden calibrate --save`.

## Evaluation
- `eval`: scores narrators (grounded template, a swapped-window baseline, an inflating baseline, and optionally a real LLM) on numeric recall, hallucination rate, misattribution rate, pass rate.
- `golden`: generates summaries with *injected* defects (answer key stored separately so you label blind), a resumable labelling CLI, and judge calibration (Cohen's kappa, confusion matrix, per-defect misses). Human labels are never auto-filled.
