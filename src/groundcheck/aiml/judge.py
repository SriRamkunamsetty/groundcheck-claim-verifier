"""Production Structured Judge using Pydantic v2.
Standardizes LLM-as-a-Judge evaluations with strict schema validation,
confidence estimation, and structured defect taxonomy."""

from __future__ import annotations

import json
import re
import time
from enum import Enum
from typing import Callable, Sequence

from pydantic import BaseModel, Field

from ..metrics import Fact
from ..narrator import fmt


class JudgeVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class DefectCategory(str, Enum):
    NONE = "none"
    WRONG_CLAIM_ATTRIBUTION = "wrong_claim_attribution"
    NUMBER_WRONG = "number_wrong"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    CAUSAL_OVERCLAIM = "causal_overclaim"
    STYLE_HYPE = "style_hype"
    OTHER = "other"


class EvaluationResult(BaseModel):
    verdict: JudgeVerdict
    defect: DefectCategory = DefectCategory.NONE
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    failed_claims: list[str] = Field(default_factory=list)
    reasoning: str
    latency_ms: float = 0.0


JUDGE_SYSTEM_PROMPT = """You are a rigorous, production-grade auditor of AI-generated analytics summaries.
Your mandate: Verify that EVERY numeric claim corresponds to a verified computed fact for the EXACT metric and window specified.
Flag unhedged causal claims ('proves', 'definitively drove') and hyperbolic sensationalism.

You must respond ONLY with valid JSON matching this schema:
{
  "verdict": "pass" | "fail",
  "defect": "none" | "wrong_claim_attribution" | "number_wrong" | "unsupported_claim" | "causal_overclaim" | "style_hype" | "other",
  "confidence": 0.0 to 1.0,
  "failed_claims": ["list of offending clauses or numbers"],
  "reasoning": "Clear explanation of the determination"
}"""


class StructuredJudge:
    """Evaluates analytics texts against ground truth facts using an LLM provider
    with Pydantic validation."""

    def __init__(self, llm_callable: Callable[[str, str | None, int], str]):
        self.llm = llm_callable

    def evaluate(
        self,
        text: str,
        facts: Sequence[Fact],
        title: str = "Title",
    ) -> EvaluationResult:
        facts_summary = "\n".join(
            f"- {f.id} ({f.label}): {fmt(f)} [Unit: {f.unit}, Query: {f.query}]"
            for f in facts
        )

        user_prompt = (
            f"=== GROUND TRUTH FACTS FOR '{title}' ===\n"
            f"{facts_summary}\n\n"
            f"=== CANDIDATE SUMMARY UNDER REVIEW ===\n"
            f"{text}\n\n"
            "RULES:\n"
            "1. PASS only if every number equals the fact for the exact metric AND window cited.\n"
            "2. If a real number is used for the wrong timeframe (e.g. 24h number called 72h), FAIL with 'wrong_claim_attribution'.\n"
            "3. If a number does not exist in ground truth, FAIL with 'number_wrong' or 'unsupported_claim'.\n"
            "4. If correlation is claimed as proven causation without a control, FAIL with 'causal_overclaim'.\n"
            "5. Otherwise PASS."
        )

        start = time.perf_counter()
        try:
            raw_response = self.llm(user_prompt, JUDGE_SYSTEM_PROMPT, 500)
            latency_ms = (time.perf_counter() - start) * 1000.0

            # Extract JSON block even if model wraps in markdown
            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if not json_match:
                return EvaluationResult(
                    verdict=JudgeVerdict.ERROR,
                    defect=DefectCategory.OTHER,
                    reasoning=f"LLM did not return valid JSON: {raw_response[:120]}",
                    latency_ms=latency_ms,
                )

            data = json.loads(json_match.group(0))

            # Normalize values
            verdict_str = str(data.get("verdict", "")).strip().lower()
            defect_str = str(data.get("defect", "none")).strip().lower()

            return EvaluationResult(
                verdict=JudgeVerdict(verdict_str) if verdict_str in ("pass", "fail") else JudgeVerdict.ERROR,
                defect=DefectCategory(defect_str) if defect_str in DefectCategory._value2member_map_ else DefectCategory.OTHER,
                confidence=float(data.get("confidence", 0.9)),
                failed_claims=list(data.get("failed_claims", [])),
                reasoning=str(data.get("reasoning", "")),
                latency_ms=round(latency_ms, 2),
            )
        except Exception as exc:
            return EvaluationResult(
                verdict=JudgeVerdict.ERROR,
                defect=DefectCategory.OTHER,
                reasoning=f"Judge execution error: {exc}",
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )
