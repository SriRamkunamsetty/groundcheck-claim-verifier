import unittest
from fastapi.testclient import TestClient

from groundcheck.api.app import create_app


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(":memory:")
        cls.client = TestClient(cls.app)

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["version"], "2.0.0")

    def test_ready(self):
        resp = self.client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ready"])

    def test_metrics(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("groundcheck_claims_verified_total", resp.text)
        self.assertIn("groundcheck_http_requests_total", resp.text)

    def test_verify_endpoint_clean_passes(self):
        payload = {
            "text": "Aurora: 3.48M views in the first 72 hours across 3 posts, with engagement of 8.49%.",
            "facts": [
                {"id": "velocity_72h", "label": "72h views", "value": 3480000, "unit": "views"},
                {"id": "n_posts", "label": "posts", "value": 3, "unit": "count"},
                {"id": "eng_rate", "label": "engagement", "value": 0.0849, "unit": "ratio"},
            ],
            "ignore": ["Aurora"],
        }
        resp = self.client.post("/api/v1/verify", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["n_numbers"], 3)
        self.assertEqual(data["claim_accuracy"], 1.0)
        self.assertEqual(len(data["problems"]), 0)

    def test_verify_endpoint_swapped_window_fails(self):
        payload = {
            "text": "Aurora: 1.99M views in the first 72 hours.",
            "facts": [
                {"id": "velocity_24h", "label": "24h views", "value": 1990000, "unit": "views"},
                {"id": "velocity_72h", "label": "72h views", "value": 3480000, "unit": "views"},
            ],
            "ignore": ["Aurora"],
        }
        resp = self.client.post("/api/v1/verify", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertTrue(any("misattributed" in str(p).lower() or "is the value of" in str(p).lower() for p in data["problems"]))

    def test_batch_verify(self):
        payload = {
            "items": [
                {
                    "item_id": "item-1",
                    "text": "Post count was 3.",
                    "facts": [{"id": "n_posts", "label": "posts", "value": 3, "unit": "count"}],
                },
                {
                    "item_id": "item-2",
                    "text": "Surged 999% week over week.",
                    "facts": [{"id": "n_posts", "label": "posts", "value": 3, "unit": "count"}],
                },
            ]
        }
        resp = self.client.post("/api/v1/verify/batch", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_processed"], 2)
        self.assertEqual(data["passed"], 1)
        self.assertEqual(data["failed"], 1)

    def test_dataset_endpoints(self):
        resp_titles = self.client.get("/api/v1/titles")
        self.assertEqual(resp_titles.status_code, 200)
        titles = resp_titles.json()
        self.assertGreater(len(titles), 0)

        first_id = titles[0]["id"]
        resp_facts = self.client.get(f"/api/v1/facts/{first_id}")
        self.assertEqual(resp_facts.status_code, 200)
        self.assertGreater(len(resp_facts.json()), 0)


if __name__ == "__main__":
    unittest.main()
