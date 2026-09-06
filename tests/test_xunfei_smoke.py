from __future__ import annotations

import unittest

from tools.xunfei_smoke import _LogicalXunfeiBackend


class LogicalXunfeiBackendTests(unittest.TestCase):
    def test_composite_receipt_contains_one_verified_segment_per_item(self) -> None:
        backend = _LogicalXunfeiBackend("test-scope")

        receipt = backend.submit(
            "submission-1",
            {
                "plan": [
                    {"item_id": "item-1", "content": "First"},
                    {"item_id": "item-2", "content": "Second"},
                ],
                "profile": {"generation_mode": "composite_cut"},
            },
        )

        self.assertEqual(set(receipt["segments"]), {"item-1", "item-2"})
        self.assertTrue(all(receipt["segments"].values()))
        self.assertEqual(backend.submit_calls, 1)


if __name__ == "__main__":
    unittest.main()
