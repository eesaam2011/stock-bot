import unittest
from datetime import datetime,timedelta,timezone

from rest_bar_shadow_audit import RESTBarShadowAudit
from test_step2n_real_redis import local_test_redis

UTC=timezone.utc


def rows(start,close=10.0):
    return [{"t":(start+timedelta(minutes=i)).isoformat(),"o":close,"h":close+.1,
             "l":close-.1,"c":close,"v":1000+i,"vw":close,"n":10}
            for i in range(30)]


class REST:
    def __init__(self,value):self.value=value
    def bars(self,*args):return list(self.value)


class TestStep3BBRealRedis(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis();self.session="2099-09-26"
        self.pattern=f"operational_priority_radar:v1.1:bar_audit:{self.session}:*"
        keys=list(self.r.scan_iter(match=self.pattern))
        if keys:self.r.delete(*keys)
        self.start=datetime(2026,9,25,13,30,tzinfo=UTC)
        self.value=rows(self.start);self.rest=REST(self.value)
        self.audit=RESTBarShadowAudit(self.r,self.rest,self.session)
    def tearDown(self):
        keys=list(self.r.scan_iter(match=self.pattern))
        if keys:self.r.delete(*keys)
    def record(self):
        observed=self.start+timedelta(minutes=31)
        return self.audit.record("BASE_1MIN","AAA",self.value,
            self.start+timedelta(minutes=30),observed,False),observed
    def test_observation_is_durable_and_expiring(self):
        key,_=self.record()
        self.assertTrue(self.r.exists(key));self.assertGreater(self.r.ttl(key),0)
    def test_due_comparison_records_mutation_without_finality(self):
        key,observed=self.record();self.rest.value=rows(self.start,11.0)
        result=self.audit.compare(key,observed+timedelta(hours=19))
        self.assertTrue(result["bars_changed"]);self.assertNotIn("finality_proven",result)
    def test_compare_due_is_bounded(self):
        _,observed=self.record()
        result=self.audit.compare_due(observed+timedelta(hours=19),1)
        self.assertEqual(result["checked"],1);self.assertEqual(result["compared"],1)
        self.assertFalse(result["finality_proven"])


if __name__=="__main__":unittest.main()
