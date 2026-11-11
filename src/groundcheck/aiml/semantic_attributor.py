"""Semantic claim attribution engine.
Blends deterministic regex cue patterns with n-gram vectorization and cosine similarity
to detect paraphrased metrics, swapped windows, and semantic drift."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Sequence

from ..metrics import Fact
from ..narrator import _cue


@dataclass(frozen=True)
class AttributionMatch:
    fact_id: str
    confidence: float
    method: str  # "regex", "semantic", "hybrid"
    detail: str


METRIC_SYNONYMS: dict[str, list[str]] = {
    "velocity_24h": [
        "views in first 24 hours", "first day viewership", "24h views", "day one volume",
        "24-hour total views", "opening 24 hours reach", "first twenty four hours views"
    ],
    "velocity_72h": [
        "views in first 72 hours", "three day volume", "first 3 days viewership", "72h views",
        "72-hour view count", "opening three days views", "first seventy-two hours views"
    ],
    "velocity_7d": [
        "views in first 7 days", "one week views", "first week volume", "7d views",
        "168 hours viewership", "first seven days views", "weekly total views"
    ],
    "eng_rate": [
        "engagement rate", "interaction percentage", "social engagement ratio",
        "likes comments shares over views", "engagement of views", "audience interaction"
    ],
    "sov_7d": [
        "share of voice", "competitive market share", "category view percentage",
        "share of 7-day views", "relative voice volume"
    ],
    "benchmark_72h": [
        "genre percentile", "percentile rank", "peer velocity benchmark",
        "percentile among genre titles", "cohort ranking"
    ],
    "n_posts": [
        "posts tracked", "videos published", "uploads count", "total clips", "posts count"
    ],
    "anomaly_views": [
        "unusual spike views", "peak anomalous day", "surge volume", "spike viewership"
    ],
    "anomaly_day": [
        "peak day index", "anomaly occurred on day", "day of spike"
    ],
}


def _tokenize(text: str) -> list[str]:
    """Tokenize and generate character n-grams (3-gram to 5-gram) and words for robust similarity."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    words = [w for w in cleaned.split() if len(w) > 1]
    grams = []
    for w in words:
        grams.append(w)
        if len(w) >= 3:
            for n in (3, 4):
                if len(w) >= n:
                    grams.extend([w[i:i+n] for i in range(len(w) - n + 1)])
    return grams


def _vector_cosine(vec_a: Counter[str], vec_b: Counter[str]) -> float:
    dot = sum(vec_a[k] * vec_b[k] for k in vec_a if k in vec_b)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticClaimAttributor:
    """Attributes numeric claims in text clauses to underlying facts using hybrid
    regex cue detection + semantic n-gram cosine matching."""

    def __init__(
        self,
        similarity_threshold: float = 0.28,
        custom_embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.similarity_threshold = similarity_threshold
        self.custom_embed_fn = custom_embed_fn
        self._corpus_vectors: dict[str, list[Counter[str]]] = {}
        self._build_index()

    def _build_index(self):
        for fid, synonyms in METRIC_SYNONYMS.items():
            self._corpus_vectors[fid] = [_tokenize(s) for s in synonyms]

    def register_metric_synonyms(self, fact_id: str, synonyms: list[str]):
        """Register custom domain synonyms for external facts."""
        vectors = [_tokenize(s) for s in synonyms]
        if fact_id in self._corpus_vectors:
            self._corpus_vectors[fact_id].extend(vectors)
        else:
            self._corpus_vectors[fact_id] = vectors

    def score_clause(self, clause: str, fact_id: str) -> float:
        """Compute the semantic similarity score (0.0 to 1.0) between a clause and a fact metric."""
        clause_toks = Counter(_tokenize(clause))
        if not clause_toks:
            return 0.0

        targets = self._corpus_vectors.get(fact_id, [])
        if not targets:
            # Fallback to fact_id tokenization itself
            targets = [_tokenize(fact_id.replace("_", " "))]

        best_score = 0.0
        for target_toks in targets:
            score = _vector_cosine(clause_toks, Counter(target_toks))
            if score > best_score:
                best_score = score
        return best_score

    def attribute_clause(
        self,
        clause: str,
        facts: Sequence[Fact],
    ) -> list[AttributionMatch]:
        """Determine all candidate facts that this clause is semantically about."""
        matches: list[AttributionMatch] = []

        for fact in facts:
            # 1. Deterministic regex check
            rx = _cue(fact.id)
            if rx and re.search(rx, clause, re.IGNORECASE):
                matches.append(
                    AttributionMatch(
                        fact_id=fact.id,
                        confidence=1.0,
                        method="regex",
                        detail=f"Matched deterministic cue pattern: {rx}",
                    )
                )
                continue

            # 2. Semantic matching check
            score = self.score_clause(clause, fact.id)
            if score >= self.similarity_threshold:
                matches.append(
                    AttributionMatch(
                        fact_id=fact.id,
                        confidence=round(score, 3),
                        method="semantic",
                        detail=f"Semantic n-gram similarity: {score:.3f} >= {self.similarity_threshold}",
                    )
                )

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches

    def check_claim_attribution(
        self,
        clause: str,
        fact: Fact,
        all_facts: Sequence[Fact],
    ) -> tuple[bool, str]:
        """Check if a specific fact is legitimately attributable to this clause.
        Returns (is_valid, reason)."""
        matches = self.attribute_clause(clause, all_facts)
        if not matches:
            # If clause has zero cues and zero semantic signal for any metric,
            # attribution cannot be verified semantically (defaults to existence check).
            return True, "No metric cues or semantic signals detected in clause."

        # Check if the intended fact is within top matches
        matched_ids = {m.fact_id for m in matches}
        if fact.id in matched_ids:
            conf = next(m.confidence for m in matches if m.fact_id == fact.id)
            return True, f"Attributed to {fact.id} (confidence: {conf:.2f})"

        # If clause clearly points to other metrics, flag misattribution!
        top_alt = matches[0]
        return False, (
            f"Misattribution detected: clause semantically matches {top_alt.fact_id} "
            f"(confidence {top_alt.confidence:.2f}), but the stated number belongs to {fact.id}."
        )
