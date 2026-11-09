# Portfolio notes

A verifier for AI-generated analytics that catches the error existence-checks miss, with an evaluation workflow that measures the verifier against human labels instead of trusting it.

## Bullets you can defend today (each is backed by the test suite or a screenshot in this repo)
- Designed a claim-level verifier for AI-written analytics that flags real numbers attached to the wrong metric or window, plus unhedged causal claims and hype; a highlighted UI view is tested to agree with the verdict on 60 generated summaries.
- Built an evaluation harness: narrator scoring (recall, invented numbers, wrong-metric numbers), a blind golden-set generator with a separate answer key, a resumable labelling CLI, and Cohen's-kappa judge calibration.
- Shipped a standalone `verify` CLI that works on any facts file (custom per-fact cue patterns, exit code for CI gating) and a claim-bench web UI; 24 automated tests including a real-browser check.

## Add these only after you have measured them yourself (do not publish a number you have not measured)
- Labelled [N] summaries blind; rules-judge kappa vs my labels = [x]; LLM-judge kappa = [y].
- Defects missed by each judge, by type: [from `golden calibrate`].

## Interview questions this project prepares you for
**Why isn't checking that a number exists enough?**  
Because the dangerous error is a real number on the wrong claim, e.g. the 24h figure called the 72h figure. That is why every claim is attributed to a metric via cues in its clause.

**Your rules flag 100% of injected defects. Is the verifier 100% accurate?**  
No. The defects are the classes the rules target, so that number is a regression check. Accuracy is agreement with independent human labels, which the golden-set workflow measures.

**What are the false-negative risks?**  
Cues are keyword patterns, so a paraphrase with no cue is unattributed, and a clause naming several metrics lets a value matching any of them pass. I document both and measure misses per defect type.
