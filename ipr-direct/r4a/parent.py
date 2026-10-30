# COMPLETE: R4-A process guard kernel; approved live CLI remains deferred to R4-C.
"""Process boundary for non-cancellable ingress/native worker threads.
No process is spawned by import or construction. Real stage launch is deferred to C.
"""
import time,uuid
from shadow_guard_main import ParentController,restart_cooldown,startup_claim
from restart_budget import EXIT_RESTART_REQUESTED
from .durable import SessionBudget
from .guards import MemoryBounds
class ProcessGuard:
 def __init__(self,redis,policy,authorize,spawn,sample,*,clock=time.monotonic,wall=time.time,sleep=time.sleep,boot_id=None):
  self.controller=ParentController(policy,SessionBudget(redis),boot_id or uuid.uuid4().hex,wall)
  self.authorize=authorize;self.spawn=spawn;self.sample=sample
  self.clock=clock;self.wall=wall;self.sleep=sleep;self.external_stop=False;self.child=None;self.reason=None;self.termination_started=None
 def stop(self):self.external_stop=True
 def run(self):
  started=self.clock();self.authorize();MemoryBounds().check(self.sample())
  status=startup_claim(self.controller,sleeper=self.sleep,monotonic=self.clock)
  if status=='TRANSIENT_STARTUP_EXIT':return {'action':'WORKER_EXIT','exit_code':EXIT_RESTART_REQUESTED,'reason':status,'spawned':False}
  if status!='ALLOW':return {'action':'PARK','reason':status,'spawned':False}
  # Original budget counts the attempt before child construction. An ambiguous spawn
  # consumes its boot, never grants an uncounted replacement.
  self.child=self.spawn(self.controller.boot_id,self.controller.boot_number)
  while self.child.poll() is None:
   reason=None
   try:
    self.authorize();MemoryBounds().check(self.sample())
    if not self.controller.policy.time_allows(self.wall()):reason='SESSION_CLOSED'
   except Exception as exc:reason=type(exc).__name__
   if self.external_stop:reason='EXTERNAL_STOP'
   if reason and self.termination_started is None:
    self.reason=reason;self.termination_started=self.clock();self.child.terminate()
   if self.termination_started is not None and self.clock()-self.termination_started>=60:self.child.kill()
   self.sleep(.25)
  decision=self.controller.after_worker_exit(self.external_stop)
  if decision=='RESTART_REQUESTED':
   # Original 120 sec minimum guard lifetime, not an extension of shutdown tail.
   if restart_cooldown(self.controller.policy,started,stopped=lambda:self.external_stop,wall=self.wall,sleeper=self.sleep,monotonic=self.clock):
    return {'action':'WORKER_EXIT','exit_code':EXIT_RESTART_REQUESTED,'reason':self.reason or 'CHILD_EXIT','boot':self.controller.boot_number}
   decision='SESSION_CLOSED_OR_EXTERNAL_STOP'
  return {'action':'PARK' if not self.external_stop else 'EXTERNAL_STOP','reason':decision,'boot':self.controller.boot_number}
