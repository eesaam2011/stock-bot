import unittest
from pathlib import Path

import numpy as np

from phase3_execution_engine import Phase3ExecutionEngine, load_phase3_protocol


class Phase3ExecutionTests(unittest.TestCase):
    def test_embedded_protocol_is_safe_and_versioned(self):
        protocol, fingerprint = load_phase3_protocol(Path("missing-phase3-protocol.json"))
        self.assertEqual(protocol["protocol_id"], "NDR-PHASE3-DUAL-HEAD-2026-09-03-A")
        self.assertFalse(protocol["safety"]["alerts_enabled"])
        self.assertFalse(protocol["safety"]["orders_enabled"])
        self.assertEqual(len(fingerprint), 64)

    def test_combined_score_requires_both_heads(self):
        quality = np.asarray([0.9, 0.9, 0.1])
        execution = np.asarray([0.9, 0.1, 0.9])
        score = Phase3ExecutionEngine._combined(quality, execution)
        self.assertGreater(score[0], score[1])
        self.assertAlmostEqual(score[1], score[2])


if __name__ == "__main__":
    unittest.main()
