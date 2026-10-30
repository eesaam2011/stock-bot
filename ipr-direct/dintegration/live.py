"""Single-reader integration state machine. No connection is opened on construction.
Transport truth is read-only publicly and can only be established by this adapter checks.
"""
import json,time
from datetime import datetime,timezone
from dreview.core import Blocked,_LIVE_ADAPTER_AUTHORITY
class LiveAdapter:
 def __init__(self,controller,bridge,rest,recovery,clock_check,filter_check,root_check,checkpoint,measure):
  self.c=controller;self.bridge=bridge;self.rest=rest;self.recovery=recovery;self.clock_check=clock_check;self.filter_check=filter_check;self.root_check=root_check;self.checkpoint=checkpoint;self.measure=measure
  self.__trusted=False;self.authenticated=False;self.recovering=set();self.last=None;self.failure=None;self.history={};self.received_sequence=0
 @property
 def transport_trusted(self):return self.__trusted
 def checks(self):
  self.bridge.leadership.require_current()
  if not self.clock_check() or not self.root_check():raise Blocked('ROOT_OR_CLOCK_UNPROVEN')
 def close_eligibility(self,reason):
  self.__trusted=False;self.c._set_transport_from_adapter(False,_LIVE_ADAPTER_AUTHORITY);self.failure=reason
 def revoke(self,reason):
  self.__trusted=False;self.c._set_transport_from_adapter(False,_LIVE_ADAPTER_AUTHORITY);self.failure=reason
  self.checkpoint.append_epoch({'state':'UNTRUSTED','reason':reason,'epoch':self.c.epoch()})
 def connection_started(self):
  self.revoke('CONNECTING');self.c.reconnect();self.authenticated=False;self.history={};self.recovering=set(self.c.desired_bar)
 def authenticated_message(self,msg):
  if msg.get('T')=='success' and msg.get('msg')=='authenticated':self.authenticated=True;return True
  self.revoke('AUTH_NOT_PROVEN');return False
 def recover(self,now):
  self.__trusted=False;start=time.monotonic()
  try:
   self.checks()
   if not self.authenticated or self.c.inflight or self.c.failed:raise Blocked('ACK_AUTH_INCOMPLETE')
   if self.c.bar!=self.c.desired_bar or self.c.trade!=self.c.desired_trade:raise Blocked('SUBSCRIPTIONS_INCOMPLETE')
   one,five=self.rest.warmup(self.c.bar,now,self.bridge.p.ec.required_history_minutes())
   # Source availability alone is not continuity. Existing recovery must prove reconciliation.
   result=self.recovery.run()
   if not result.get('gap_recovered') or not result.get('reconciled'):raise Blocked('GAP_OR_CANONICAL_RECONCILIATION_UNPROVEN')
   self.bridge.recover_caches();self.bridge.preload(one)
   for s in self.c.bar:
    if not one.get(s) or not five.get(s):raise Blocked('NATIVE_HISTORY_UNAVAILABLE')
    # A live implementation must supply an actual continuity verifier, not len(rows).
    verifier=getattr(self.recovery,'verify_symbol_continuity',None)
    if not callable(verifier) or verifier(s,one[s],five[s],now) is not True:raise Blocked('CONTINUITY_UNPROVEN')
    self.c.attest(s,history=True,continuity=True,reconciled=True,first_trade=False)
   self.checks();self.__trusted=True;self.c._set_transport_from_adapter(True,_LIVE_ADAPTER_AUTHORITY);self.recovering.clear();self.last=now
   self.checkpoint.append_epoch(self.c.epoch());return True
  except Exception as exc:self.revoke(type(exc).__name__);return False
  finally:self.measure.observe('recovery',time.monotonic()-start)
 def on_message(self,msg,received):
  kind=msg.get('T');s=msg.get('S');self.received_sequence+=1
  if kind=='subscription':
   if set(msg.get('statuses',[]))!={'*'}:self.revoke('STATUS_SUBSCRIPTION_UNPROVEN');return False
   ok=self.c.ack(self.c.connection,msg.get('bars',[]),msg.get('trades',[]),time.monotonic())
   if not ok:self.revoke('ACK_MISMATCH')
   return ok
  if kind=='error':self.revoke('SOURCE_ERROR');return False
  if kind in {'c','x','u'}:
   # No invented inverse of an already committed ENTRY/STOP. Must reconcile or stay untrusted.
   self.recovering.add(s);self.revoke('CORRECTION_CANCEL_OR_REVISION_REQUIRES_RECONCILIATION');return False
  mapped={'b':'BAR','t':'TRADE','s':'STATUS'}.get(kind)
  if mapped is None:return False
  try:
   self.checks()
   if mapped=='STATUS':
    self.recovery.on_status(msg);self.bridge.event(mapped,msg,received,False);return True
   if not self.__trusted or self.c.failed:return False
   if s not in self.c.bar or (mapped=='TRADE' and s not in self.c.trade):return False
   allow=True
   if mapped=='TRADE':
    proof=self.c.proofs.get(s,{})
    if not all(proof.get(k) for k in ['history','continuity','reconciled']):return False
    # Final original IPR filter predicate is supplied by verified engine adapter.
    if not self.filter_check(s,msg,received):allow=False
    else:self.c.attest(s,history=True,continuity=True,reconciled=True,first_trade=True)
   self.bridge.event(mapped,{**msg,'_seq':self.received_sequence},received,allow);self.last=received;return True
  except Exception as exc:self.revoke(type(exc).__name__);return False
 async def read_socket(self,ws):
  self.connection_started()
  try:
   async for raw in ws:
    now=datetime.now(timezone.utc);messages=json.loads(raw)
    if not isinstance(messages,list):raise Blocked('WS_ARRAY_EXPECTED')
    for m in messages:
     if m.get('T')=='success':self.authenticated_message(m)
     else:self.on_message(m,now)
  except Exception:self.revoke('SOCKET_DISCONNECTED')
