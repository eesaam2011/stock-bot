import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from dynamic_trade_scope import DynamicTradeScope, DynamicTradeScopeUnsafe
from rest_bar_maturity import MaturedREST1MinCoordinator
from rest_bar_shadow_audit import RESTBarShadowAudit
from websocket_runtime import WebSocketRuntime
from alpaca_production_market import AlpacaSIPProtocol

UTC = timezone.utc


def bar(ts, close=10.0, volume=1000):
    return {"t": ts.isoformat(), "o": close, "h": close + .1,
            "l": close - .1, "c": close, "v": volume,
            "vw": close, "n": 10}


class FakeREST:
    def __init__(self, rows=None): self.rows = rows or {}
    def bars_multi(self, symbols, start, end, timeframe, **kwargs):
        return {symbol: list(self.rows.get(symbol, [])) for symbol in symbols}
    def bars(self, symbol, start, end, timeframe):
        return list(self.rows.get(symbol, []))


class FakeRedis:
    def __init__(self): self.data = {}
    def setex(self, key, ttl, value): self.data[key] = value
    def get(self, key): return self.data.get(key)


class DynamicTradeScopeTests(unittest.TestCase):
    def test_scope_is_epoch_fenced_bounded_and_ack_gated(self):
        changes=[]; scope=DynamicTradeScope(2,changes.append)
        scope.start_epoch(7)
        self.assertTrue(scope.require("AAA"))
        self.assertFalse(scope.authorized(7,"AAA"))
        scope.acknowledge(7,["AAA"])
        self.assertTrue(scope.authorized(7,"AAA"))
        scope.require("BBB")
        with self.assertRaisesRegex(DynamicTradeScopeUnsafe,"SCOPE_LIMIT"):
            scope.require("CCC")
        scope.invalidate()
        self.assertFalse(scope.authorized(7,"AAA"))
        self.assertEqual(changes,[('AAA',),('AAA','BBB')])

    def test_wrong_epoch_ack_fails_closed(self):
        scope=DynamicTradeScope();scope.start_epoch(3,["AAA"])
        with self.assertRaisesRegex(DynamicTradeScopeUnsafe,"EPOCH_MISMATCH"):
            scope.acknowledge(2,["AAA"])


class RESTMaturityTests(unittest.TestCase):
    def test_only_mature_bars_are_delivered_once(self):
        now=datetime(2026,9,25,14,2,tzinfo=UTC)
        rows={"AAA":[bar(now-timedelta(minutes=3)),
                     bar(now-timedelta(minutes=1))]}
        accepted=[]
        c=MaturedREST1MinCoordinator(FakeREST(rows),["AAA"],
                                     lambda s,b,r:accepted.append((s,b["t"])))
        first=c.poll(now,True);second=c.poll(now,True)
        self.assertEqual(first["accepted"],1)
        self.assertEqual(second["accepted"],0)
        self.assertEqual(len(accepted),1)

    def test_untrusted_poll_makes_no_rest_decisions(self):
        rest=FakeREST({"AAA":[]});called=[]
        c=MaturedREST1MinCoordinator(rest,["AAA"],lambda *x:called.append(x))
        self.assertFalse(c.poll(datetime.now(UTC),False)["decision_allowed"])
        self.assertFalse(called)


class RESTShadowAuditTests(unittest.TestCase):
    def test_unchanged_bundle_has_no_flip(self):
        start=datetime(2026,9,25,13,30,tzinfo=UTC)
        rows=[bar(start+timedelta(minutes=i),10+i*.01,1000+i) for i in range(30)]
        redis=FakeRedis();rest=FakeREST({"AAA":rows})
        audit=RESTBarShadowAudit(redis,rest,"2026-09-25",due_hours=18)
        observed=start+timedelta(minutes=31)
        key=audit.record("BASE_1MIN","AAA",rows,start+timedelta(minutes=30),observed,False)
        pending=audit.compare(key,observed+timedelta(hours=1))
        self.assertEqual(pending["status"],"PENDING")
        compared=audit.compare(key,observed+timedelta(hours=19))
        self.assertEqual(compared["status"],"COMPARED")
        self.assertFalse(compared["bars_changed"])
        self.assertFalse(compared["decision_flipped"])

    def test_mutation_is_recorded_without_finality_claim(self):
        start=datetime(2026,9,25,13,30,tzinfo=UTC)
        original=[bar(start+timedelta(minutes=i),10,1000) for i in range(30)]
        redis=FakeRedis();rest=FakeREST({"AAA":original})
        audit=RESTBarShadowAudit(redis,rest,"2026-09-25")
        observed=start+timedelta(minutes=31)
        key=audit.record("BASE_1MIN","AAA",original,start+timedelta(minutes=30),observed,False)
        rest.rows["AAA"]=[*original[:-1],bar(start+timedelta(minutes=29),11,5000)]
        compared=audit.compare(key,observed+timedelta(hours=19))
        self.assertTrue(compared["bars_changed"])
        self.assertNotIn("finality_proven",compared)


class DynamicRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_broad_bars_then_ack_gated_dynamic_trade(self):
        class WS:
            def __init__(self):
                self.controls=[json.dumps([{"T":"success","msg":"connected"}]),
                               json.dumps([{"T":"success","msg":"authenticated"}]),
                               json.dumps([{"T":"subscription","trades":[],
                                            "bars":["AAA","BBB"],"statuses":["*"]}])]
                self.events=asyncio.Queue();self.sent=[]
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            async def recv(self):return self.controls.pop(0)
            async def send(self,value):self.sent.append(json.loads(value))
            def __aiter__(self):return self
            async def __anext__(self):return await self.events.get()
        ws=WS();seen=[]
        rt=WebSocketRuntime(lambda _:ws,AlpacaSIPProtocol,
            lambda msg:(seen.append(msg),asyncio.sleep(0))[1],lambda:asyncio.sleep(0),
            dispatch_queue_max=8,dynamic_trade_mode=True)
        task=asyncio.create_task(rt.run_once("u","k","s",["AAA","BBB"]))
        await asyncio.wait_for(rt.connected_event.wait(),1)
        self.assertEqual(ws.sent[1]["bars"],["AAA","BBB"])
        self.assertEqual(ws.sent[1]["trades"],[])
        rt.require_trade_symbol("AAA",True)
        for _ in range(50):
            if any(x.get("trades")==["AAA"] for x in ws.sent[2:]):break
            await asyncio.sleep(.01)
        self.assertFalse(rt.trade_authorized("AAA"))
        await ws.events.put(json.dumps([{"T":"subscription","trades":["AAA"],
                                        "bars":["AAA","BBB"],"statuses":["*"]}]))
        for _ in range(50):
            if rt.trade_authorized("AAA"):break
            await asyncio.sleep(.01)
        self.assertTrue(rt.trade_authorized("AAA"))
        await ws.events.put(json.dumps([{"T":"t","S":"AAA","p":10,"s":1,
                                        "t":"2026-09-25T14:00:00Z"}]))
        for _ in range(50):
            if seen:break
            await asyncio.sleep(.01)
        self.assertEqual(seen[0]["S"],"AAA")
        rt.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)


if __name__ == "__main__": unittest.main()
