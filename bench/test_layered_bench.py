"""Acceptance of the fixed zero-network mechanism bench."""
import unittest
from layered_bench import evaluate


class LayeredBenchTests(unittest.TestCase):
    def test_current_mechanisms(self):
        result = evaluate()
        self.assertEqual(result["passed"], result["total"])
        self.assertEqual(result["model_calls"], 0)
