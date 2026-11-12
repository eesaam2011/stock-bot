import json
import unittest
from datetime import datetime, timedelta, timezone

from rest_bar_maturity import MaturedREST1MinCoordinator
from rest_bar_shadow_audit import RESTBarShadowAudit


UTC=timezone.utc


def bar(ts,close=10.0):
    return {"t":ts.isoformat(),"o":close,"h":close+.1,"l":close-.1,
            "c":close,"v":1000,"vw":close,"n":10}


class REST:
    def __init__(self,rows):self.rows=rows
    def bars_multi(self,symbols,*args,**kwargs):
        return {s:list(self.rows.get(s,[])) for s in symbols}
    def bars(self,symbol,*args):return list(self.rows.get(symbol,[]))


class Redis:
    def __init__(self):self.data={}
    def setex(self,key,ttl,value):self.data[key]=value
    def get(self,key):return self.data.get(key)
    def scan(self,cursor=0,match=None,count=10):
        prefix=match[:-1] if match and match.endswith("*") else match
        keys=[k for k in self.data if not prefix or k.startswith(prefix)]
        return 0,keys[:count]


class MaturityStudyTests(unittest.TestCase):
    def test_checkpoint_observation_does_not_make_early_decision(self):
        bar_start=datetime(2026,9,29,14,1,tzinfo=UTC)
        rest=REST({"AAA":[bar(bar_start)]});decisions=[];observations=[]
        coordinator=MaturedREST1MinCoordinator(
            rest,["AAA"],lambda *x:decisions.append(x),grace_seconds=90,
            study_observer=lambda s,r,e,c,n:observations.append(c),
            study_symbols=["AAA"])
        first=coordinator.poll(bar_start+timedelta(minutes=1,seconds=35),True)
        self.assertEqual(decisions,[])
        self.assertEqual(observations,[30])
        self.assertEqual(first["accepted"],0)
        second=coordinator.poll(bar_start+timedelta(minutes=2,seconds=35),True)
        self.assertEqual(len(decisions),1)
        self.assertEqual(observations,[30,60,90])
        self.assertEqual(second["accepted"],1)

    def test_each_checkpoint_has_separate_durable_observation(self):
        start=datetime(2026,9,29,13,30,tzinfo=UTC)
        rows=[bar(start+timedelta(minutes=i)) for i in range(30)]
        redis=Redis();audit=RESTBarShadowAudit(redis,REST({"AAA":rows}),"2026-09-29")
        eval_ts=start+timedelta(minutes=30)
        k30=audit.record_checkpoint("BASE_1MIN","AAA",rows,eval_ts,
                                    eval_ts+timedelta(seconds=30),30)
        k90=audit.record_checkpoint("BASE_1MIN","AAA",rows,eval_ts,
                                    eval_ts+timedelta(seconds=90),90)
        self.assertNotEqual(k30,k90)
        self.assertEqual(json.loads(redis.get(k30))["checkpoint_seconds"],30)
        result=audit.compare_due(eval_ts+timedelta(hours=20),10)
        self.assertEqual(result["compared"],2)
        self.assertIn("30",result["by_checkpoint"])
        self.assertIn("90",result["by_checkpoint"])
        self.assertIsNone(result["recommended_grace_seconds"])
        self.assertFalse(result["actionable_alerts_authorized"])


if __name__ == "__main__":unittest.main()
