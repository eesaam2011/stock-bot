from dintegration.timing import timed,phase
"""Fenced A journal; local locking; explicit add/resolve case facts, not snapshot guesses."""
import json,threading
CONTROL_FIELDS={'T','S','t','oi','op','os','oc','ci','cp','cs','cc','i','p','s','c','a','o','h','l','v','vw','n','id'}
class JournalState:
 def __init__(self,state):
  self.raw=state.raw if isinstance(state,JournalState) else state
  if not hasattr(self.raw,'_r4b_lock'):self.raw._r4b_lock=threading.Lock()
  self.lock=self.raw._r4b_lock
 def __getattr__(self,key):return getattr(self.raw,key)
 def append_epoch(self,event):
  # Snapshot canonical references BEFORE taking the journal/state locks. Ingress
  # already owns the decision lock; reversing this order would deadlock on error.
  provider=getattr(self.raw,'case_refs',None)
  if provider is not None and 'case_refs' not in event:event={**event,'case_refs':provider()}
  from ipr.observability import observed_phase
  with observed_phase('journal.lock_wait'):self.lock.acquire()
  try:
   with observed_phase('journal.persist'):return self.raw.append_epoch(event)
  finally:self.lock.release()
 def restore(self):
  with self.lock:return self.raw.restore()
class ProofStore:
 def __init__(self,state,proof):self.state=state;self.proof=proof;self.pending_R=set();self.pending_U=set();self.last_control=None
 def save(self,reason,pending_R=(),control_event=None):
  self.pending_R.update(pending_R)
  event={'kind':'B_STREAM_WITNESS','case_schema':2,'reason':reason,'proof':self.proof.snapshot(),'pending_R':sorted(self.pending_R)}
  if control_event is not None:
   event['control_event']={k:v for k,v in control_event.items() if k in CONTROL_FIELDS};self.last_control=event['control_event']
   event['add_R']=[control_event['S']]
  return self.state.append_epoch(event)
 def resolve(self,resolved_R=(),resolved_U=()):
  if not resolved_R and not resolved_U:return
  self.state.append_epoch({'kind':'B_CASE_RESOLUTION','case_schema':2,'resolved_R':sorted(resolved_R),'resolved_U':sorted(resolved_U),'proof':self.proof.snapshot()})
  self.pending_R.difference_update(resolved_R);self.pending_U.difference_update(resolved_U)
 @timed('load_archive')
 def load_archive(self):
  if hasattr(self.state,'archive_state'):
   archive=self.state.archive_state();self.pending_R=set(archive['R']);self.pending_U=set(archive['U']);self.last_control=archive['control_event'];latest=archive['proof']
   if latest is None:self.proof.invalidate();return False
   latest['invalid']={s:why for s,why in latest.get('invalid',{}).items() if why not in {'CORRECTION_OR_CANCEL','PERSISTED_UNRESOLVED_CORRECTION'} or s in self.pending_R}
   for s in self.pending_R:
    if s in latest.get('scope',[]):latest['invalid'].setdefault(s,'PERSISTED_UNRESOLVED_CORRECTION')
   self.proof.restore(latest);return True
  self.state.restore();latest=None;self.pending_R=set();self.pending_U=set();self.last_control=None
  # Apply case facts in journal order. A delayed/stale witness snapshot cannot
  # resurrect a resolved correction; a NEW correction has its own explicit add_R.
  with phase('load_archive.replay'):
   for n in range(1,self.state.seq+1):
    raw=self.state.r.hget(self.state.keys[2],str(n));raw=raw.decode() if isinstance(raw,bytes) else raw
    event=json.loads(raw)['epoch']
    if not isinstance(event,dict):continue
    if event.get('case_schema')!=2:self.pending_R.update(event.get('pending_R',[])) # predecessor audit compatibility
    self.pending_R.update(event.get('add_R',[]));self.pending_U.update(event.get('add_U',[]))
    self.pending_R.difference_update(event.get('resolved_R',[]));self.pending_U.difference_update(event.get('resolved_U',[]))
    if 'control_event' in event:self.last_control=event['control_event']
    if 'proof' in event:latest=event['proof']
  if latest is not None:
   invalid=latest.get('invalid',{})
   latest['invalid']={s:reason for s,reason in invalid.items() if reason not in {'CORRECTION_OR_CANCEL','PERSISTED_UNRESOLVED_CORRECTION'} or s in self.pending_R}
   for s in self.pending_R:
    if s in latest.get('scope',[]):latest['invalid'].setdefault(s,'PERSISTED_UNRESOLVED_CORRECTION')
   self.proof.restore(latest);return True
  self.proof.invalidate();return False
