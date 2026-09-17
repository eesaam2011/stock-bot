import unittest
from redis_lua_production import *
class FakeRedis:
 def __init__(self):self.d={};self.g=0
 def eval(self,script,n,*a):
  keys=a[:n];v=a[n:]
  if script==ACQUIRE_LEASE_LUA:
   leader,gen=keys;owner,ttl=v
   if leader in self.d:return [0,self.d[leader],str(self.g)]
   self.g+=1;self.d[gen]=str(self.g);self.d[leader]=owner;return [1,owner,str(self.g)]
  if script==RENEW_LEASE_LUA:return 1 if self.d.get(keys[0])==v[0] else 0
  if script==DELETE_LEASE_LUA:
   if self.d.get(keys[0])!=v[0]:return 0
   del self.d[keys[0]];return 1
  if script==ATOMIC_ENTRY_LUA:
   leader,opp,trade,out=keys;owner,expected,new,tr,ob=v
   if self.d.get(leader)!=owner:return -10
   if self.d.get(opp)!=expected:return -20
   if trade in self.d and self.d[trade]!=tr:return -30
   if out in self.d and self.d[out]!=ob:return -40
   self.d[opp]=new;self.d[trade]=tr;self.d[out]=ob;return 1
  if script==ATOMIC_TRADE_EVENT_LUA:
   leader,trade,out=keys;owner,expected,new,ob=v
   if self.d.get(leader)!=owner:return -10
   if self.d.get(trade)!=expected:return -20
   if out in self.d and self.d[out]!=ob:return -30
   self.d[trade]=new;self.d[out]=ob;return 1
class TestStep12(unittest.TestCase):
 def setUp(self):self.f=FakeRedis();self.x=ProductionRedisLua(self.f)
 def test_acquire_generation(self):
  ok,g=self.x.acquire("A");self.assertTrue(ok);self.assertEqual(g,1)
 def test_single_leader(self):
  self.x.acquire("A");ok,g=self.x.acquire("B");self.assertFalse(ok)
 def test_takeover_increments_generation(self):
  self.x.acquire("A");self.x.release("A");ok,g=self.x.acquire("B");self.assertEqual(g,2)
 def test_compare_renew(self):
  self.x.acquire("A");self.x.renew("A")
  with self.assertRaises(LeaseLost):self.x.renew("B")
 def test_compare_delete(self):
  self.x.acquire("A");self.assertFalse(self.x.release("B"));self.assertTrue(self.x.release("A"))
 def test_atomic_entry_all_visible(self):
  self.x.acquire("A");self.f.d["opp"]="old";self.x.atomic_entry("A","opp","old","new","trade","tr","out","ob")
  self.assertEqual((self.f.d["opp"],self.f.d["trade"],self.f.d["out"]),("new","tr","ob"))
 def test_atomic_entry_expected_conflict(self):
  self.x.acquire("A");self.f.d["opp"]="other"
  with self.assertRaises(AtomicConflict):self.x.atomic_entry("A","opp","old","new","trade","tr","out","ob")
 def test_atomic_entry_stale_leader(self):
  self.x.acquire("A");self.f.d["opp"]="old"
  with self.assertRaises(LeaseLost):self.x.atomic_entry("B","opp","old","new","trade","tr","out","ob")
 def test_atomic_trade_and_outbox(self):
  self.x.acquire("A");self.f.d["trade"]="old";self.x.atomic_trade_event("A","trade","old","new","out","ob")
  self.assertEqual((self.f.d["trade"],self.f.d["out"]),("new","ob"))
 def test_atomic_trade_conflict(self):
  self.x.acquire("A");self.f.d["trade"]="other"
  with self.assertRaises(AtomicConflict):self.x.atomic_trade_event("A","trade","old","new","out","ob")
 def test_lua_entry_contains_no_partial_return_after_sets(self):
  self.assertLess(ATOMIC_ENTRY_LUA.find("return -40"),ATOMIC_ENTRY_LUA.find("redis.call('SET',opp"))
 def test_lua_trade_checks_before_sets(self):
  self.assertLess(ATOMIC_TRADE_EVENT_LUA.find("return -30"),ATOMIC_TRADE_EVENT_LUA.find("redis.call('SET',trade"))
if __name__=="__main__":unittest.main()
