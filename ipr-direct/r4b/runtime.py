# COMPLETE: R4-B REV1 review scope; live deployment and R4-C are not authorized.
"""Opt-in R4-B assembly. No executable production CLI or sender; R4-C untouched."""
import asyncio
from r4a.runtime import Runtime
from r4a.clocked import ClockedControl
from .store import ProofStore
from .replan import UnionPlanner
class LockedControl:
 def __init__(self,base,lock,ack):self.base=base;self.lock=lock;self.notify_ack=ack
 def plan(self):
  with self.lock:return self.base.plan()
 def tick(self):
  with self.lock:return self.base.tick()
 def ack(self,*args):
  with self.lock:
   ok=self.base.ack(*args)
   if ok:self.notify_ack()
   return ok
class EvidenceRuntime(Runtime):
 RETRY_SPACING=1.0 # engineering coalescing, NOT a safety timeout; T120 is unchanged.
 def __init__(self,adapter,*a,**kw):
  args=list(a)
  if args:args[0]=adapter.checkpoint
  else:kw["state"]=adapter.checkpoint
  super().__init__(adapter,*args,**kw)
  self.proof=adapter.proof;self.store=ProofStore(self.state,self.proof);adapter.store=self.store
  self.wake=asyncio.Event();self.attempts=0;self.last_attempt=None;self.recovery_running=False
  self.control=LockedControl(ClockedControl(self.c,self.clock),adapter.control_lock,self.acknowledged)
  self.observe_stream=self.received
  self.planner=UnionPlanner(self);self.case_event=asyncio.Event();self.loop=None
  self.a.status_changed=self.notify_status_changed
  self.a.prepare_needs=self.prepare_needs;self.a.needs_changed=self.notify_cases_changed
  original_native=self.a.bridge.native_cycle
  def native_with_notification(*args,**kw):
   try:return original_native(*args,**kw)
   finally:self.notify_cases_changed()
  self.a.bridge.native_cycle=native_with_notification
 def acknowledged(self):
  self.proof.set_scope(self.c.bar)
  applied=self.planner.after_ack()
  self.case_event.set()
  if self.c.bar==self.c.desired_bar and self.c.trade==self.c.desired_trade:self.wake.set()
 def prepare_needs(self):
  # Called after the original controller reconnect reset; an old pending request
  # cannot receive an ACK on the new socket. Canonical cases remain authoritative.
  self.planner.pending=None
  needs=self.a.bridge.needs(recovery_symbols=self.a.barrier.persisted_R)
  return self.planner.propose(needs)
 def notify_status_changed(self):
  if self.loop is not None:self.loop.call_soon_threadsafe(self.wake.set)
  else:self.wake.set()
 def notify_cases_changed(self):
  if self.loop is not None:self.loop.call_soon_threadsafe(self.case_event.set)
  else:self.case_event.set()
 async def case_loop(self):
  while not self.stop.is_set():
   await self.case_event.wait();self.case_event.clear()
   def read_needs():
    with self.a.bridge.p._decision_lock:
     return self.a.bridge.needs(recovery_symbols=self.a.barrier.persisted_R)
   needs=await asyncio.to_thread(read_needs)
   with self.a.control_lock:result=self.planner.propose(needs)
   if result['status']=='APPLIED' and not self.a.transport_trusted and self.auth and not self.c.inflight and self.c.bar==self.c.desired_bar and self.c.trade==self.c.desired_trade:self.wake.set()
 def received(self,msg,received):
  if msg.get('T') in {'c','x','u','error'}:self.revoke('SOURCE_REVISION_OR_ERROR')
  changed=self.proof.observe(msg,received)
  if msg.get('T') in {'c','x','u'}:
   if not isinstance(msg.get('S'),str):raise ValueError('CORRECTION_SYMBOL_MISSING')
   self.a.barrier.persisted_R.add(msg.get('S'));self.store.save('CORRECTION_NO_INVERSE',self.a.barrier.persisted_R,msg)
  if msg.get('T')=='b' and msg.get('S') in self.proof.invalid:
   from r4a.guards import SafetyStop
   self.revoke('INVALID_OR_CONFLICTING_BAR');self.a.barrier.persisted_R.add(msg['S'])
   self.store.save('INVALID_OR_CONFLICTING_BAR',self.a.barrier.persisted_R,msg)
   raise SafetyStop('INVALID_OR_CONFLICTING_BAR')
  if changed:self.wake.set()
 def set_desired(self,*a):
  with self.a.control_lock:return super().set_desired(*a)
 async def recovery_loop(self):
  while not self.stop.is_set():
   await self.wake.wait();self.wake.clear()
   if not self.auth or self.c.failed or self.c.inflight:continue
   if self.last_attempt is not None:
    await asyncio.sleep(max(0,self.RETRY_SPACING-(self.clock()-self.last_attempt)))
   # Recheck AFTER await; do not recover an obsolete ACK/connection.
   if not self.auth or self.c.failed or self.c.inflight:continue
   self.check_session();self.attempts+=1;self.last_attempt=self.clock();self.recovery_running=True
   try:await asyncio.to_thread(self.a.recover,self.wall())
   finally:self.recovery_running=False;self.notify_cases_changed()
 async def connection(self):
  try:return await super().connection()
  finally:
   self.proof.invalidate();self.wake.clear()
   self.store.save('CONNECTION_ENDED_UNTRUSTED',self.a.barrier.persisted_R)
 async def connections(self):
  self.loop=asyncio.get_running_loop()
  tasks=[asyncio.create_task(super().connections()),asyncio.create_task(self.recovery_loop()),asyncio.create_task(self.case_loop())]
  try:
   done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
   for t in done:t.result()
  finally:
   for t in tasks:t.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)

def attach_B(runtime):
 """Require an EvidenceAdapter, not an unrestricted legacy boolean verifier."""
 from .adapter import EvidenceAdapter
 if not isinstance(runtime.a,EvidenceAdapter):raise TypeError('EVIDENCE_ADAPTER_REQUIRED')
 return EvidenceRuntime(runtime.a,runtime.state,runtime.leader,runtime.socket_open,runtime.authorize,runtime.policy,
    clock=runtime.clock,wall=runtime.wall,sample=runtime.sample)
