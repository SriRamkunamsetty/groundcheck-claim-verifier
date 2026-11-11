import unittest
from datetime import date, datetime, timezone

from groundcheck.aiml.adversarial import AdversarialSynthesizer
from groundcheck.aiml.judge import DefectCategory, JudgeVerdict, StructuredJudge
from groundcheck.aiml.ml_anomalies import MLAnomalyDetector
from groundcheck.aiml.semantic_attributor import SemanticClaimAttributor
from groundcheck.metrics import Fact


class TestSemanticAttributor(unittest.TestCase):
    def setUp(self):
        self.attributor = SemanticClaimAttributor(similarity_threshold=0.25)
        self.facts = [
            Fact("velocity_24h", "views in first 24h", 1.99e6, "views", ""),
            Fact("velocity_72h", "views in first 72h", 3.48e6, "views", ""),
            Fact("eng_rate", "engagement rate", 0.0849, "ratio", ""),
        ]

    def test_regex_deterministic_match_has_confidence_one(self):
        matches = self.attributor.attribute_clause("3.48M views in the first 72 hours", self.facts)
        self.assertTrue(len(matches) > 0)
        self.assertEqual(matches[0].fact_id, "velocity_72h")
        self.assertEqual(matches[0].confidence, 1.0)
        self.assertEqual(matches[0].method, "regex")

    def test_paraphrased_metric_semantic_match(self):
        # "three day viewership" is not a regex match for 72h, but is semantically matched
        matches = self.attributor.attribute_clause("three day volume was high", self.facts)
        matched_ids = [m.fact_id for m in matches]
        self.assertIn("velocity_72h", matched_ids)

    def test_custom_synonym_registration(self):
        self.attributor.register_metric_synonyms("custom_metric", ["viral reach ratio", "network spread"])
        score = self.attributor.score_clause("viral reach was strong", "custom_metric")
        self.assertGreater(score, 0.3)


class TestMLAnomalyDetector(unittest.TestCase):
    def test_detect_spike(self):
        detector = MLAnomalyDetector(z_threshold=2.5)
        # Create 14 days of data with a massive spike on day 7
        daily_series = [
            (date(2026, 9, i), 10000.0 * (1.1 ** i) if i != 7 else 350000.0)
            for i in range(1, 15)
        ]
        events = [(datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc), "tv_spot", "Superbowl Spot")]
        anomalies = detector.detect_anomalies(daily_series, events)
        self.assertTrue(len(anomalies) > 0)
        spike = anomalies[0]
        self.assertEqual(spike.direction, "up")
        self.assertIn("Superbowl Spot", spike.correlated_events)


class TestAdversarialSynthesizer(unittest.TestCase):
    def test_synthesizes_all_mutation_types(self):
        synth = AdversarialSynthesizer()
        template = "Title: [[f:velocity_72h]] views in the first 72 hours. It aligns with an event, which fits the timing but does not prove cause."
        facts = [
            Fact("velocity_24h", "24h views", 1.99e6, "views", ""),
            Fact("velocity_72h", "72h views", 3.48e6, "views", ""),
        ]
        suite = synth.generate_suite(template, facts, "Title")
        defects = {r.defect_type for r in suite}
        self.assertIn("wrong_claim_attribution", defects)
        self.assertIn("number_wrong", defects)
        self.assertIn("unsupported_claim", defects)
        self.assertIn("causal_overclaim", defects)


class TestStructuredJudge(unittest.TestCase):
    def test_parses_json_and_evaluates(self):
        fake_llm = lambda p, s, t: '{"verdict":"fail","defect":"wrong_claim_attribution","confidence":0.95,"failed_claims":["1.99M in 72h"],"reasoning":"Swapped 24h into 72h"}'
        judge = StructuredJudge(fake_llm)
        res = judge.evaluate("1.99M in 72h", [Fact("velocity_24h", "24h", 1.99e6, "views", "")])
        self.assertEqual(res.verdict, JudgeVerdict.FAIL)
        self.assertEqual(res.defect, DefectCategory.WRONG_CLAIM_ATTRIBUTION)
        self.assertEqual(res.confidence, 0.95)
        self.assertIn("1.99M in 72h", res.failed_claims)


if __name__ == "__main__":
    unittest.main()
