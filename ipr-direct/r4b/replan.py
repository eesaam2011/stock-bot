"""Current canonical needs -> safe desired set -> original plan/ACK controller."""
from dreview.core import Blocked,CAP

def shape(n):return None if n is None else (n.bar,n.O,n.A,n.U,n.R,n.canonical_complete)
class UnionPlanner:
 def __init__(self,runtime):self.r=runtime;self.pending=None;self.last=None
 def propose(self,needs,candidate_bar=None):
  r=self.r;c=r.c
  try:needs.check(c.root)
  except Blocked:
   r.revoke('NEEDS_INVALID');r.checkpoint('REPLAN_BLOCKED');raise
  target=set(c.desired_bar if candidate_bar is None else candidate_bar)|set(needs.pins)
  if len(target)>CAP or not target<=c.root.symbols:
   r.revoke('PIN_CAP_OR_ROOT_CONFLICT');r.checkpoint('REPLAN_BLOCKED');raise Blocked('PIN_CAP_OR_ROOT_CONFLICT')
  if shape(needs)==shape(c.needs) and target==c.desired_bar:
   return {'status':'UNCHANGED','bar':sorted(target)}
  try:
   r.state.append_epoch({'kind':'B_NEEDS_INTENT','case_schema':2,'add_U':sorted(needs.U),'add_R':sorted(needs.R)})
  except Exception:
   r.revoke('NEEDS_INTENT_PERSIST_FAILED');raise
  if c.inflight and (not needs.pins<=c.inflight['after_bar'] or not needs.trades<=c.inflight['after_trade']):
   # Keep the already-sent plan immutable. Its ACK is consumed, then this target is
   # applied before another ordinary recovery. Never ignore a newly arriving need.
   r.revoke('NEEDS_DURING_PENDING_CONTROL');self.pending=(target,needs)
   r.state.append_epoch({'kind':'B_NEEDS_PENDING','bar':sorted(target),'O':sorted(needs.O),'A':sorted(needs.A),'U':sorted(needs.U),'R':sorted(needs.R),'add_U':sorted(needs.U)})
   self.last={'status':'WAIT_PENDING_ACK_THEN_RECOVER','bar':sorted(target)};return self.last
  same_channels=target==c.desired_bar and set(needs.trades)==c.desired_trade
  if same_channels and not (needs.U or needs.R):
   # O -> A retains exactly the same channel set; do not create an artificial gap.
   with r.a.control_lock:c.set_desired(target,needs);r.checkpoint('NEEDS_CHANGED_SAME_CHANNELS')
  else:r.set_desired(target,needs)
  self.pending=None;self.last={'status':'APPLIED','bar':sorted(target),'requires_recovery':not same_channels};return self.last
 def after_ack(self):
  if self.pending is None or self.r.c.inflight:return None
  target,needs=self.pending
  # The needs loop supplies a newer canonical snapshot if one arrives meanwhile.
  return self.propose(needs,target)
