import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import rest_maturity_shadow_runner as runner

UTC=timezone.utc


class TestStep3BIRunner(unittest.TestCase):
    def test_sample_is_deterministic_and_bounded(self):
        values=[f"S{i}" for i in range(100)]
        self.assertEqual(runner._sample(values,"2026-09-30",16),
                         runner._sample(reversed(values),"2026-09-30",16))
        self.assertEqual(len(runner._sample(values,"2026-09-30",16)),16)

    def test_compare_writes_non_actionable_evidence(self):
        class Redis:
            def ping(self): return True
        class Audit:
            calls=0
            def __init__(self,*a,**k): pass
            def compare_due(self,*a,**k):
                self.calls+=1
                if self.calls==1:
                    return {"checked":2,"compared":2,"bars_changed":1,
                            "decision_flipped":0,"by_checkpoint":{
                                "90":{"compared":2,"bars_changed":1,
                                      "decision_flipped":0}}}
                return {"checked":2,"compared":0,"bars_changed":0,
                        "decision_flipped":0,"by_checkpoint":{}}
        args=SimpleNamespace(session="2026-09-30",due_hours=18,output="")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{
            "ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s","REDIS_URL":"redis://x"}), \
             patch.object(runner,"_redis_from_url",return_value=Redis()), \
             patch.object(runner,"RESTBarShadowAudit",Audit):
            args.output=os.path.join(d,"evidence.json")
            body=runner.compare(args,lambda:datetime(2026,10,1,tzinfo=UTC))
            self.assertEqual(body["compared"],2)
            self.assertFalse(body["actionable_alerts_authorized"])
            self.assertFalse(body["finality_proven"])
            with open(args.output,encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["bars_changed"],1)


if __name__ == "__main__": unittest.main()
