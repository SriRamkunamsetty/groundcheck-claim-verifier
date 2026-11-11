"""Adversarial Synthesizer.
Generates challenging test cases with systematic perturbations to stress-test
automated verifiers, LLM judges, and human annotation workflows."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from ..metrics import Fact
from ..narrator import fmt, render


@dataclass(frozen=True)
class PerturbationResult:
    original_text: str
    perturbed_text: str
    defect_type: str
    severity: str  # "subtle", "moderate", "blatant"
    explanation: str


class AdversarialSynthesizer:
    """Synthesizes adversarial evaluation cases across multiple defect taxonomies."""

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def generate_suite(
        self,
        base_template: str,
        facts: Sequence[Fact],
        title: str,
    ) -> list[PerturbationResult]:
        """Generate a complete battery of adversarial mutations for a single narrative."""
        by_id = {f.id: f for f in facts}
        clean_text = render(base_template, facts)
        results: list[PerturbationResult] = []

        # 1. Swapped Window (Subtle, dangerous error)
        if "[[f:velocity_72h]]" in base_template and "velocity_24h" in by_id:
            swapped = base_template.replace("[[f:velocity_72h]]", "[[f:velocity_24h]]")
            results.append(
                PerturbationResult(
                    original_text=clean_text,
                    perturbed_text=render(swapped, facts),
                    defect_type="wrong_claim_attribution",
                    severity="subtle",
                    explanation="24h view count substituted into the 72h narrative window.",
                )
            )

        # 2. Subtle Numerical Drift (3% - 5% inflation, just beyond 1% tolerance)
        if "velocity_72h" in by_id:
            drifted_val = by_id["velocity_72h"].value * 1.04
            drifted_fact = Fact("d", "d", drifted_val, "views", "")
            subtle = base_template.replace("[[f:velocity_72h]]", fmt(drifted_fact))
            results.append(
                PerturbationResult(
                    original_text=clean_text,
                    perturbed_text=render(subtle, facts),
                    defect_type="number_wrong",
                    severity="subtle",
                    explanation="4% numerical drift injected (violates 1% tolerance window).",
                )
            )

        # 3. Gross Inflation (25% boost)
        if "velocity_72h" in by_id:
            gross_val = by_id["velocity_72h"].value * 1.25
            gross_fact = Fact("g", "g", gross_val, "views", "")
            gross = base_template.replace("[[f:velocity_72h]]", fmt(gross_fact))
            results.append(
                PerturbationResult(
                    original_text=clean_text,
                    perturbed_text=render(gross, facts),
                    defect_type="number_wrong",
                    severity="blatant",
                    explanation="25% view inflation injected.",
                )
            )

        # 4. Fabricated Trend
        results.append(
            PerturbationResult(
                original_text=clean_text,
                perturbed_text=clean_text + " Views surged 340% week-over-week against industry averages.",
                defect_type="unsupported_claim",
                severity="blatant",
                explanation="Injected entirely fabricated 340% week-over-week trend claim.",
            )
        )

        # 5. Causal Overclaim
        hedged_phrase = "which fits the timing but does not prove cause"
        if hedged_phrase in clean_text:
            overclaim_text = clean_text.replace(
                hedged_phrase, "which definitively proves the campaign drove the spike"
            )
        else:
            overclaim_text = clean_text + " This conclusively proves the influencer push caused the breakout."

        results.append(
            PerturbationResult(
                original_text=clean_text,
                perturbed_text=overclaim_text,
                defect_type="causal_overclaim",
                severity="moderate",
                explanation="Unhedged, unverified causal link asserted without controlled testing.",
            )
        )

        return results
