# COMPLETE: R4-A offline assembly scope only; live B/C integration pending.
"""Assembly kernel. R4-B supplies data/recovery; R4-C supplies sender/config.
Only one WS reader. Control mutations occur on the asyncio loop, not in ingress worker.
"""
import asyncio,json,time
from datetime import datetime,timezone
from ws_ingress import BoundedWSIngress
from .clocked import ClockedControl
from .guards import AckTrustDeadline,MemoryBounds,SafetyStop
from .durable import RedisState,SessionBudget
from dintegration.persistence import NamespacedRedis
from .durable import PREFIX

class PipelinePort:
 """Original ingress serializes work using the ORIGINAL pipeline decision lock.
 Trust/filter/decision dispatch belongs to R4-B adapter; no new decision rule here.
 """
 def __init__(self,adapter):
  self.a=adapter;self.p=adapter.bridge.p
 @property
 def _decision_lock(self):return self.p._decision_lock
 @property
 def _entry_opportunities(self):return self.p._entry_opportunities
 @property
 def _active_by_symbol(self):return self.p._active_by_symbol
 @property
 def contention_probe(self):return getattr(self.p,'contention_probe',None)
 @property
 def _confluence_lease_lost(self):return getattr(self.p,'_confluence_lease_lost',False)
 @_confluence_lease_lost.setter
 def _confluence_lease_lost(self,v):self.p._confluence_lease_lost=v
 def on_bar(self,s,msg,received,allow):self.a.on_message(msg,received)
 def on_trade(self,s,msg,received,allow):self.a.on_message(msg,received)
 def on_status(self,msg):pass # LiveAdapter processes STATUS exactly once in recovery port.
class RecoveryPort:
 def __init__(self,adapter):self.a=adapter
 def on_status(self,msg):self.a.on_message(msg,datetime.now(timezone.utc))
 def on_stream_message(self,kind,msg):pass # R4-B stream evidence is recorded at reader receipt.

class Runtime:
 def __init__(self,adapter,state,leadership,socket_open,authorize,policy,*,clock=time.monotonic,
              wall=lambda:datetime.now(timezone.utc),sample,observe_stream=None):
  self.a=adapter;self.c=adapter.c;self.state=state;self.leader=leadership;self.socket_open=socket_open
  self.authorize=authorize;self.policy=policy;self.clock=clock;self.wall=wall;self.sample=sample
  self.control=ClockedControl(self.c,clock)
  self.deadline=AckTrustDeadline(clock);self.memory=MemoryBounds();self.stop=asyncio.Event()
  self.ready=asyncio.Event();self.ws=None;self.auth=False;self.stopping=False;self.reason=None
  self.observe_stream=observe_stream;self.tasks=[];self.last_renew=None;self.ingress=None
 def revoke(self,reason):
  # Close entry eligibility in memory BEFORE any failing Redis operation.
  self.a.close_eligibility(reason)
 def checkpoint(self,reason):
  self.state.append_epoch({'reason':reason,'controller':self.c.epoch()})
 def check_session(self):
  self.authorize()
  if not self.policy.time_allows(self.wall().timestamp()):raise SafetyStop('SESSION_CLOSED')
 def set_desired(self,bar,needs):
  self.revoke('SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY')
  self.check_session();self.leader.require_current()
  self.c.set_desired(bar,needs)
  self.checkpoint('DESIRED_UPDATED')
 async def _writer(self):
  while not self.stop.is_set():
   self.check_session();self.control.tick()
   if self.c.failed:raise SafetyStop('CONTROL_FAILED')
   if self.auth:
    p=self.control.plan()
    if p:
     # Record intent before send; crash/timeout => reconnect and re-ACK, never restore inflight trust.
     self.checkpoint('CONTROL_INTENT')
     wire=dict(p)
     if p['action']=='subscribe':wire['statuses']=['*']
     # Unsubscribe only the named BAR/TRADE deltas; keep the status channel.
     await self.ws.send(json.dumps(wire))
   await asyncio.sleep(.05)
 async def _reader(self):
  async for raw in self.ws:
   self.check_session();received=self.wall();rows=json.loads(raw)
   if not isinstance(rows,list):raise SafetyStop('WS_ARRAY_REQUIRED')
   for msg in rows:
    if not isinstance(msg,dict):raise SafetyStop('WS_OBJECT_REQUIRED')
    kind=msg.get('T')
    if kind=='success':
     if msg.get('msg')=='authenticated':
      self.auth=self.a.authenticated_message(msg)
      if not self.auth:raise SafetyStop('AUTH_FAILED')
     continue
    if kind=='subscription':
     if not self.auth or set(msg.get('statuses',[]))!={'*'}:raise SafetyStop('UNPROVEN_ACK')
     if not self.control.ack(self.c.connection,msg.get('bars',[]),msg.get('trades',[])):raise SafetyStop('ACK_REJECTED')
     self.deadline.ack();self.checkpoint('ACK_CONFIRMED');self.ready.set();continue
    if kind=='error':raise SafetyStop('SOURCE_ERROR')
    if not self.auth:raise SafetyStop('UNAUTHENTICATED_DATA')
    if self.observe_stream is not None:self.observe_stream(msg,received)
    if kind in {'c','x','u'}:
     self.revoke('CORRECTION_REQUIRES_RECOVERY');self.a.recovering.add(msg.get('S'));raise SafetyStop('CORRECTION_REQUIRES_RECOVERY')
    mapped={'b':'BAR','t':'TRADE','s':'STATUS'}.get(kind)
    if mapped:self.ingress.submit(mapped,msg,received)
  raise ConnectionError('SOCKET_EOF')
 async def _watch(self):
  while not self.stop.is_set():
   self.check_session();self.memory.check(self.sample())
   self.deadline.tick(self.a.transport_trusted)
   if self.ingress is not None and self.ingress.failure is not None and self.ingress.failure.done():raise self.ingress.failure.result()
   if getattr(self.a.bridge.p,'_confluence_lease_lost',False):raise SafetyStop('CONFLUENCE_LEASE_LOST')
   await asyncio.sleep(.25)
 async def _renew(self):
  # Failure is fatal; do not continue market processing while Redis is unavailable.
  while not self.stop.is_set():
   await asyncio.to_thread(self.leader.renew);self.last_renew=self.clock()
   await asyncio.sleep(10)
 async def recover_after_ack(self):
  self.check_session()
  # Slow historical I/O is off the asyncio loop; no automatic positive trust invented by A.
  await self.ready.wait()
  generation=self.c.connection;revision=self.c.revision
  ok=await asyncio.to_thread(self.a.recover,self.wall())
  if generation!=self.c.connection or revision!=self.c.revision:
   self.revoke('RECOVERY_EPOCH_CHANGED');return False
  return ok
 async def connection(self):
  self.check_session();self.leader.require_current()
  self.revoke('CONNECTING');self.a.connection_started();self.auth=False;self.ready.clear()
  self.checkpoint('CONNECTION_STARTED')
  self.ingress=BoundedWSIngress(PipelinePort(self.a),RecoveryPort(self.a),lambda:self.a.transport_trusted,
    capacity=8192,max_age_seconds=5.0,on_gap=lambda:self.revoke('INGRESS_FAIL_CLOSED'))
  assert self.ingress.batch_size==32
  tasks=[];self.ws=None
  try:
   await self.ingress.begin()
   self.ws=await self.socket_open()
   tasks=[asyncio.create_task(fn()) for fn in (self._reader,self._writer)]
   done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
   for t in done:t.result()
  finally:
   self.revoke('DISCONNECTED');self.auth=False;self.ready.clear()
   for t in tasks:t.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)
   try:
    if self.ws is not None:await self.ws.close()
   finally:
    # Do not reconnect while an old decision worker can still be running.
    await self.ingress.end()
 async def connections(self):
  delay=1
  while not self.stop.is_set():
   try:await self.connection()
   except (SafetyStop,ValueError,TypeError):raise
   except Exception:
    # Network disconnect can reconnect; durable checkpoint error cannot be suppressed.
    self.revoke('CONNECTION_FAILED');self.checkpoint('CONNECTION_FAILED')
   await asyncio.sleep(delay);delay=min(30,delay*2)
 async def run(self):
  self.revoke('STARTING_UNTRUSTED')
  self.check_session();self.memory.check(self.sample())
  self.state.restore();self.revoke('RESTORED_UNTRUSTED')
  self.leader.require_current();self.checkpoint('RUNTIME_START')
  self.tasks=[asyncio.create_task(f()) for f in (self.connections,self._watch,self._renew)]
  self.tasks.append(asyncio.create_task(self.stop.wait()))
  try:
   done,_=await asyncio.wait(self.tasks,return_when=asyncio.FIRST_COMPLETED)
   for t in done:t.result()
  finally:
   self.revoke('STOPPING');self.stop.set()
   for t in self.tasks:t.cancel()
   # Existing 60-second tail ceiling. A non-cooperative thread requires a PROCESS parent.
   try:await asyncio.wait_for(asyncio.gather(*self.tasks,return_exceptions=True),timeout=60)
   except asyncio.TimeoutError:raise SafetyStop('TAIL_60_SECONDS_PARENT_KILL_REQUIRED')

class Factory:
 """Dependency injection assembly, no env credentials or implicit connections.
 Real data adapter and sender are intentionally separate review blocks B/C.
 """
 @staticmethod
 def build(adapter,redis,leadership,socket_open,authorize,policy,**ports):
  authorize()
  state=RedisState(redis,policy.session,adapter.c.root.identity(),leadership.worker_instance_id,
                   leadership.require_current().leader_generation)
  adapter.checkpoint=state
  return Runtime(adapter,state,leadership,socket_open,authorize,policy,**ports)


def compose(*,redis,root,worker,boot_id,policy,session_policy,rest,recovery_factory,clock_check,filter_check,root_check,measure,socket_open,authorize,sample,clock=time.monotonic,wall=lambda:datetime.now(timezone.utc),observe_stream=None):
 """Construct originals in isolated namespace only AFTER all pre-I/O gates.
 Data/recovery ports are R4-B inputs, not default stubs. No token/env reader here.
 """
 from .redis_ports import leadership
 from redis_lua_production import ProductionRedisLua
 from dintegration.bridge import OriginalBridge
 from dintegration.live import LiveAdapter
 from dreview.core import Controller
 authorize();root.check()
 if not policy.time_allows(wall().timestamp()) or root.session!=policy.session:raise SafetyStop('SESSION_ROOT_MISMATCH')
 if root_check() is not True or clock_check() is not True:raise SafetyStop('ROOT_OR_CLOCK_UNPROVEN')
 MemoryBounds().check(sample())
 status,number=SessionBudget(redis).claim(policy,boot_id)
 if status not in {'ALLOW','INITIALIZED_BY_FIRST_CLAIM'} or not 1<=number<=4:raise SafetyStop('BOOT_CLAIM_DENIED')
 lead=leadership(redis,worker);lead.acquire()
 canonical=NamespacedRedis(redis,PREFIX+':canonical:');lua=ProductionRedisLua(canonical)
 bridge=OriginalBridge(canonical,lua,lead,policy.session,root.symbols,rest,worker,session_policy)
 state=RedisState(redis,policy.session,root.identity(),worker,lead.require_current().leader_generation)
 state.restore();controller=Controller(root)
 rec=recovery_factory(bridge)
 adapter=LiveAdapter(controller,bridge,rest,rec,clock_check,filter_check,root_check,state,measure)
 runtime=Runtime(adapter,state,lead,socket_open,authorize,policy,clock=clock,wall=wall,sample=sample,observe_stream=observe_stream)
 runtime.boot_number=number;return runtime
