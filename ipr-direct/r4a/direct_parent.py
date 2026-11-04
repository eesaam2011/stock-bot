"""R6 direct parent: lease wait before child; child atomically acquires before claim.
Original ProcessGuard remains the monitor, restart policy and 120s cooldown.
"""
from .parent import ProcessGuard
from .lease_wait import wait_for_lease
from .durable import PREFIX
from shadow_guard_main import startup_claim

class DirectProcessGuard(ProcessGuard):
 def __init__(self,redis,*args,**kw):
  super().__init__(redis,*args,**kw);self.redis=redis
 def _read_boot(self):
  value=self.redis.hget(PREFIX+':budget:'+self.controller.policy.key,'boot:'+self.controller.boot_id)
  return None if value is None else int(value)
 def run(self):
  self.authorize()
  key=PREFIX+':budget:'+self.controller.policy.key
  # Read-only early ceiling. The original atomic Lua remains the final authority.
  boots=self.redis.hget(key,'boots')
  if boots is not None:
   if not 0<=int(boots)<=4:raise ValueError('BUDGET_RESPONSE_INVALID')
   if int(boots)==4:return {'action':'PARK','reason':'BUDGET_EXHAUSTED','spawned':False}
  if not wait_for_lease(self.redis,self.authorize,stopped=lambda:self.external_stop,clock=self.clock,sleep=self.sleep):
   return {'action':'EXTERNAL_STOP','spawned':False}
  # Suppress ONLY the parent's pre-spawn claim. The child makes the same original
  # idempotent claim AFTER acquiring the canonical lease. Monitor code is reused.
  original_spawn=self.spawn
  original_preflight=self.controller.preflight
  def checked_spawn(*args):
   try:return original_spawn(*args)
   except Exception:
    # OS/spawn failures were counted by R5's pre-spawn claim too. Keep that
    # ceiling without moving the normal claim ahead of atomic leadership.
    self.controller.preflight=original_preflight
    startup_claim(self.controller,sleeper=self.sleep,monotonic=self.clock)
    raise
  self.spawn=checked_spawn
  self.controller.preflight=lambda:'ALLOW'
  original_after=self.controller.after_worker_exit
  def after(external=False):
   if external:return original_after(True)
   self.controller.boot_number=self._read_boot()
   if self.controller.boot_number is None:
    # A real pre-claim startup failure still consumes one boot. NotLeader cannot
    # reach here: acquisition waits in the child and never claims on that path.
    self.controller.preflight=original_preflight
    status=startup_claim(self.controller,sleeper=self.sleep,monotonic=self.clock)
    if status!='ALLOW':return status
   return original_after(False)
  self.controller.after_worker_exit=after
  try:return super().run()
  finally:self.controller.preflight=original_preflight;self.controller.after_worker_exit=original_after;self.spawn=original_spawn
