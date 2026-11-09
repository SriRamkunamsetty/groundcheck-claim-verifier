# GroundCheck: claim-level verification of AI-written analytics

An AI summary can contain only **real** numbers and still be wrong: the 24-hour figure described as "72 hours". GroundCheck checks that every number is real **and attached to the right metric and window**, then measures how far automated judges agree with *your* labels.

## Screenshots

> **About the screenshots.** All data shown is **synthetic** (the UI says so in an orange banner). Browser images are real captures of this project's own dashboard in Chromium; terminal images are the real stdout of each command, rendered into a terminal-styled page.

### Claim bench: a clean summary
![Claim bench: a clean summary](docs/screenshots/01-claim-bench-pass.png)

Every number is green: it matches a computed fact and sits in a clause about that fact's metric. Ground truth for the title is on the right.

### Claim bench: the swapped-window error
![Claim bench: the swapped-window error](docs/screenshots/02-claim-bench-swapped-window.png)

The same summary with the 24-hour figure described as "72 hours". `1.99M` is a real number, so an existence-only check passes it; claim-level checking flags it as the wrong metric and says which fact it actually is.

### Claim bench: an invented trend
![Claim bench: an invented trend](docs/screenshots/03-claim-bench-invented-trend.png)

A number no fact supports (`340%`) is marked red.

### Evals
![Evals](docs/screenshots/04-evals.png)

Three narrators scored on 12 cases, and the rules judge on 60 summaries with injected defects. Note the swapped-window baseline has 0% invented numbers: only claim-level checking fails it. The orange box states why the 100% rows are a regression check.

### Dark mode
![Dark mode](docs/screenshots/05-dark.png)

Follows the system colour scheme.

### Mobile
![Mobile](docs/screenshots/06-mobile.png)

Phone layout.

### Terminal: the five demo variants
![Terminal: the five demo variants](docs/screenshots/07-cli-demo.png)

`demo` verifies one clean summary and four with injected defects.

### Terminal: verify any summary against a facts file
![Terminal: verify any summary against a facts file](docs/screenshots/08-cli-verify.png)

Exit code 1 on failure, so it can gate a CI step.

### Terminal: blind golden-set labelling
![Terminal: blind golden-set labelling](docs/screenshots/09-cli-golden-label.png)

The labelling prompt: facts shown, injected-defect answer key hidden, resumable.

## What it does

- **Claim-level verifier** (`verify`, `annotate`): each numeric claim must equal a computed fact *and* sit in a clause about that fact's metric. It also flags hype and unhedged causal language ("proves", "caused by").
- **Works on any facts file**: `groundcheck verify --facts facts.json --text "..."` prints the text with flagged numbers marked and exits 1 on failure, so it can gate a pipeline.
- **Claim bench UI**: edit a summary and see each number highlighted green (right metric), orange (real number, wrong metric) or red (no source). The highlighted view and the verdict share one implementation of the rules and are tested to agree on 60 generated summaries.
- **Evals**: scores narrators (a grounded template, a swapped-window baseline, an inflating baseline, and optionally a real LLM) on numeric recall, invented numbers, wrong-metric numbers, style flags and pass rate.
- **Golden set + judge calibration**: generates summaries with *injected* defects (the answer key is stored separately so you label blind), a resumable labelling CLI, and calibration of the rules judge or an LLM judge against **your** labels (Cohen's kappa, confusion matrix, per-defect misses). Human labels are never auto-filled.
- **Real LLM adapters** (Anthropic, Gemini; stdlib only) for the narrator and judge.

## Quick start

```bash
pip install -e .
groundcheck serve --demo                               # the claim bench on http://127.0.0.1:8000
groundcheck demo                                       # five variants of one summary, verified
groundcheck verify --facts examples/facts.example.json --text "Aurora Rising: 1.99M views in the first 72 hours across 3 posts." --ignore "Aurora Rising"
groundcheck eval                                       # narrators scored on the demo data
python -m unittest discover -s tests -v
```

### Verify your own analytics
`facts.json` is a list of facts; see `examples/facts.example.json`:
```json
[{"id": "saves", "label": "saves", "value": 12400, "unit": "count", "cue": "saves?"}]
```
`unit` is `views | ratio | pct | count`. The optional **`cue`** is a regex for the words that signal this metric in prose; it turns on wrong-metric detection for that fact. Built-in cues exist for the demo metric ids (`velocity_24h`, `velocity_72h`, `eng_rate`, ...). Facts without a cue still get existence, hype and causation checks, and the CLI prints a note naming them.

## Measuring the verifier yourself (the part that matters)

```bash
groundcheck golden gen --demo --n 40                   # or --db real.db; items + a SEPARATE answer key
groundcheck golden label --labeler me                  # label blind: pass / fail + defect type (resumable)
groundcheck golden calibrate --judge rules --save      # kappa vs YOUR labels, confusion matrix, misses per defect
export ANTHROPIC_API_KEY=...                     # or GEMINI_API_KEY (+ GEMINI_MODEL)
groundcheck check-llm ; groundcheck golden calibrate --judge both ; groundcheck eval --llm anthropic
```

> **Read this before quoting any number from the Evals screen.** The injected defects are exactly the classes the rules were written to catch, so near-100% flagged there is a **regression check, not evidence of general accuracy**. Accuracy is agreement with *your* labels. No human labels are bundled or faked.

Design notes and rules: `docs/DESIGN.md`.

## Status: what is verified and what is not

The build environment had **no network, no Postgres and no real API keys**. Everything below marked *not run* is code that exists and is unit-tested against local fakes, but has never talked to the real service.

| Area | Status |
|---|---|
| 24 tests: claim-level verification, annotate-agrees-with-verify on 60 generated summaries, facts-file validation and custom cues, evals, golden set (gen/label/calibrate), LLM adapters over real local sockets, API hardening (POST validation, size cap), real-browser UI | **Pass** (`python -m unittest discover -s tests -v`) |
| Claim bench UI | Rendered and inspected in real Chromium; a browser test asserts the wrong number is highlighted |
- **Attribution is a heuristic** keyed on metric cues in the same clause. It catches the swapped-window class of error, not arbitrary semantic mistakes: treat it as a floor, not a guarantee. If one clause names several metrics, a value matching any of them passes, so keep one metric per clause.
- Cues are registered process-wide when a facts file is loaded.
- The rules judge is only as good as its cues; its real accuracy is whatever your labelled golden set says.
- The bench's POST endpoint is stateless and size-capped, but the dashboard has **no authentication**.

## Known limitations

- **Not run:** the real Anthropic and Gemini APIs (request shapes follow their documented REST format and were exercised against a local server; set `ANTHROPIC_MODEL` / `GEMINI_MODEL` to current ids). The LLM judge and `eval --llm` therefore have never been run against a real model.

## Project layout

```
src/groundcheck/__init__.py
src/groundcheck/cli.py
src/groundcheck/db.py
src/groundcheck/evals.py
src/groundcheck/facts_io.py
src/groundcheck/golden.py
src/groundcheck/ingest.py
src/groundcheck/llm.py
src/groundcheck/metrics.py
src/groundcheck/narrator.py
src/groundcheck/sample_data.py
src/groundcheck/schema.sql
src/groundcheck/web.py
tests/__init__.py
tests/test_core.py
tests/test_delivery.py
docs/DESIGN.md
examples/facts.example.json
examples/summary_swapped_window.txt
```

See `docs/PORTFOLIO.md` for resume bullets you can defend and the numbers you still need to measure yourself.
