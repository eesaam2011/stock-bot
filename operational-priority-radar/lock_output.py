"""Finite FIFO output buffer. Normal lock-held lines may drop on saturation.
Known security producers run outside decision lock and use backpressure, never eviction.
No wall-clock delivery guarantee exists with a permanently blocked OS stdout.
"""
import builtins,threading,queue,io,time,json,ast
from contention_probe import TLS
SECURITY={'WS_INGRESS_FAIL_CLOSED','FAIL_CLOSED_SELF_STOP','SELF_STOP_OUTCOME','GUARD_CHILD_EXIT','GUARD_BOOT_CLAIMED','SHADOW_WEBSOCKET_DISCONNECT','WORKER_FATAL'}
class Output:
 def __init__(self,emit,capacity=64,max_bytes=131072):
  self.emit=emit;self.q=queue.Queue(maxsize=capacity);self.max_bytes=max_bytes;self.dropped=0;self.errors=0;self.written=0;self.security_delivered=0;self.security_unconfirmed_lost=0;self.flush_timeouts=0
  self.state=threading.Condition();self.pending={};self.sequence=0;self.closed=False
  self.thread=threading.Thread(target=self.run,name='v6-output-writer',daemon=True);self.thread.start()
 def print(self,*a,**kw):
  # Explicit file destinations keep original semantics.
  if kw.get('file') is not None:return self.emit(*a,**kw)
  p=getattr(TLS,'decision_probe',None);t=time.monotonic();s=io.StringIO();opts={k:v for k,v in kw.items() if k not in {'file','flush'}}
  self.emit(*a,file=s,**opts);line=s.getvalue();stage=a[0].get('stage') if a and isinstance(a[0],dict) else None
  if stage is None:
   try:
    try:stage=json.loads(line.strip()).get('stage')
    except Exception:stage=ast.literal_eval(line.strip()).get('stage')
   except Exception:pass
  security=stage in SECURITY or (isinstance(stage,str) and stage.startswith(('GUARD','SELF_STOP')))
  try:
   with self.state:
    if self.closed:
     if security:self.security_unconfirmed_lost+=1
     else:self.dropped+=1
     return
    seq=self.sequence;self.sequence+=1;self.pending[seq]=security
   item=(seq,line,security)
   if len(line.encode('utf8'))>self.max_bytes and not security:
    self.dropped+=1;self._discard(seq);return
   if p is not None:
    # Known security emitters must never be moved inside decision lock.
    if security:
     self._discard(seq,lost=True);raise RuntimeError('SECURITY_OUTPUT_INSIDE_DECISION_LOCK')
    if len(line.encode('utf8'))>self.max_bytes:self.dropped+=1;self._discard(seq);return
    try:self.q.put_nowait(item)
    except queue.Full:self.dropped+=1;self._discard(seq)
   else:self.q.put(item) # bounded backpressure outside lock, preserves FIFO
  finally:
   if p is not None:p.record('print_enqueue',getattr(TLS,'operation',None) or 'other',time.monotonic()-t)
 def _discard(self,seq,lost=False):
  with self.state:
   security=self.pending.pop(seq,None)
   if lost and security:self.security_unconfirmed_lost+=1
   self.state.notify_all()
 def run(self):
  while True:
   seq,line,security=self.q.get()
   try:
    with self.state:active=seq in self.pending
    if active:
     self.emit(line,end='',flush=True)
     with self.state:
      if seq in self.pending:
       self.written+=1
       if security:self.security_delivered+=1
   except Exception:
    self.errors+=1;self._discard(seq,lost=True)
   finally:self._discard(seq);self.q.task_done()
 def flush(self):
  # Fixed exit-tail budget. Never join a daemon blocked in an OS write.
  deadline=time.monotonic()+2.0
  with self.state:
   while self.pending:
    remaining=deadline-time.monotonic()
    if remaining<=0:
     self.flush_timeouts+=1;self.closed=True
     self.security_unconfirmed_lost+=sum(self.pending.values())
     self.dropped+=sum(not v for v in self.pending.values())
     self.pending.clear();self.state.notify_all();return False
    self.state.wait(remaining)
   return True
 def snapshot(self):
  with self.state:
   return {'queue_length':self.q.qsize(),'capacity':self.q.maxsize,'max_lock_line_bytes':self.max_bytes,'normal_dropped':self.dropped,'write_errors':self.errors,'written':self.written,'security_delivered':self.security_delivered,'security_unconfirmed_lost':self.security_unconfirmed_lost,'flush_timeouts':self.flush_timeouts,'closed':self.closed,'delivery_scope':'writer write+flush returned; no downstream receipt; timed-out pending/inflight security counted lost/unconfirmed'}
ACTIVE=None
