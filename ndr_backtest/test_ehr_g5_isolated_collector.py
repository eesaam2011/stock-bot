import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).with_name("ehr_g5_isolated_collector.py")
spec = importlib.util.spec_from_file_location("ehr_g5_isolated_collector", MODULE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

class CollectorTests(unittest.TestCase):
    def test_pagination_and_sorting(self):
        def fetch(symbol, start, end, token):
            if token is None:
                return {"bars": {symbol: [{"t":"2026-07-14T04:00:00Z","o":1,"c":2,"h":2,"l":1,"v":10}]}, "next_page_token":"next"}
            return {"bars": {symbol: [{"t":"2026-07-15T04:00:00Z","o":2,"c":3,"h":3,"l":2,"v":11}]}}
        bars = collector.collect("TEST", "2026-07-14", "2026-07-15", fetch)
        self.assertEqual([x["session"] for x in bars], ["2026-07-14","2026-07-15"])
    def test_repeated_token_fails(self):
        def fetch(symbol, start, end, token):
            return {"bars": {symbol: []}, "next_page_token":"repeat"}
        with self.assertRaisesRegex(ValueError, "pagination loop"):
            collector.collect("TEST", "2026-07-14", "2026-07-15", fetch)
    def test_duplicate_day_fails(self):
        def fetch(symbol, start, end, token):
            return {"bars": {symbol: [{"t":"2026-07-14T04:00:00Z","o":1,"c":2,"h":2,"l":1,"v":10},
                                       {"t":"2026-07-14T04:00:00Z","o":1,"c":2,"h":2,"l":1,"v":10}]}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            collector.collect("TEST", "2026-07-14", "2026-07-15", fetch)
    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"result.json"
            collector.atomic_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

if __name__ == "__main__":
    unittest.main()
