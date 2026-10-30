"""Bounded per-symbol BAR witness. Never proves TRADE completeness.
Sparse symbols: absent minutes are acceptable only if absent from BOTH REST and stream.
"""
from collections import deque
from datetime import timedelta,datetime,timezone
from threading import RLock
def instant(s):
 d=datetime.fromisoformat(s.replace("Z","+00:00"))
 if d.utcoffset() is None:raise ValueError("TIMEZONE_REQUIRED")
 return d.astimezone(timezone.utc)
import math,json
class StreamProof:
 PER_SYMBOL=8;MAX_SYMBOLS=500
 def __init__(self,capacity=8):
  if type(capacity) is not int or not 2<=capacity<=self.PER_SYMBOL:raise ValueError('EVIDENCE_CAP_RANGE')
  self.capacity=capacity;self.rows={};self.generation=None;self.valid=False;self.invalid={};self.scope=set();self.started=None
  self.lock=RLock();self.last_reason={};self.archived=None
 def connected(self,generation=None,started=None):
  import uuid
  with self.lock:
   self.generation=generation or uuid.uuid4().hex;self.started=started;self.rows={};self.invalid={};self.valid=True;self.last_reason={}
 def set_scope(self,symbols):
  with self.lock:
   ss=set(symbols)
   if len(ss)>self.MAX_SYMBOLS:raise ValueError('EVIDENCE_SYMBOL_CAP')
   self.scope=ss
   for s in list(self.rows):
    if s not in ss:self.rows.pop(s);self.invalid.pop(s,None)
 def invalidate(self,symbol=None,reason='DISCONNECT'):
  with self.lock:
   if symbol is None:self.valid=False
   else:self.invalid[symbol]=reason;self.rows.pop(symbol,None)
 def observe(self,msg,received):
  with self.lock:
   if not self.valid:return False
   s=msg.get('S')
   if msg.get('T')=='error':self.invalidate();return False
   if s not in self.scope:return False
   if msg.get('T') in {'u','c','x'}:self.invalidate(s,'CORRECTION_OR_CANCEL');return True
   if msg.get('T')!='b' or s in self.invalid:return False
   try:
    ts=instant(msg['t'])
    if ts>received or (self.started is not None and received<self.started):raise ValueError()
    vals=[msg[k] for k in ('o','h','l','c','v')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vals):raise ValueError()
    if msg['l']<=0 or not msg['l']<=msg['o']<=msg['h'] or not msg['l']<=msg['c']<=msg['h'] or msg['v']<0:raise ValueError()
   except (KeyError,ValueError,TypeError):self.invalidate(s,'INVALID_BAR');return False
   if self.started is not None and ts+timedelta(minutes=1)<self.started:return False
   rs=self.rows.setdefault(s,deque(maxlen=self.capacity))
   normalized={k:msg[k] for k in ('t','o','h','l','c','v')};normalized['t']=ts.isoformat()
   row={'bar':normalized,'received_at':received.isoformat(),'generation':self.generation}
   for old in rs:
    if instant(old['bar']['t'])==ts:
     if old['bar']!=row['bar']:self.invalidate(s,'CONFLICTING_BAR');return True
     return False
   if rs and ts<instant(rs[-1]['bar']['t']):self.invalidate(s,'OUT_OF_ORDER_BAR');return False
   rs.append(row);return True # deque evicts ONLY this symbol's oldest evidence.
 def rearm_symbol(self,symbol):
  # Successful original recovery removes the negative latch, NOT the need for
  # a new live BAR witness. Old revised data is never silently certified.
  with self.lock:self.invalid.pop(symbol,None);self.rows.pop(symbol,None)
 def evidence(self,s,one,now):
  with self.lock:
   def fail(r):self.last_reason[s]=r;return {'proven':False,'reason':r,'TRADE_completeness':False}
   if not self.valid:return fail('CONNECTION_UNTRUSTED')
   if s not in self.scope:return fail('OUTSIDE_ACTIVE_BAR_SCOPE')
   if s in self.invalid:return fail(self.invalid[s])
   live=[r for r in self.rows.get(s,[]) if r['generation']==self.generation and instant(r['received_at'])<=now and instant(r['bar']['t'])+timedelta(minutes=1)<=now]
   if not live:return fail('NO_COMPLETED_LIVE_BAR_WITNESS')
   hist={}
   for b in one:
    t=instant(b['t'])
    if t+timedelta(minutes=1)>now:return fail('REST_INCOMPLETE_OR_FUTURE_BAR')
    if t in hist and any(hist[t].get(k)!=b.get(k) for k in ('o','h','l','c','v')):return fail('REST_CONFLICTING_BAR')
    hist[t]=b
   first=instant(live[0]['bar']['t']);last=instant(live[-1]['bar']['t']);seen={instant(r['bar']['t']):r['bar'] for r in live}
   # A REST bar absent from the connected stream is a gap; silence in BOTH is not.
   expected={t:b for t,b in hist.items() if first<=t<=now-timedelta(minutes=1)}
   if set(expected)!=set(seen):return fail('REST_STREAM_BAR_SET_MISMATCH')
   for t,b in seen.items():
    if t not in hist or any(b.get(k)!=hist[t].get(k) for k in ('o','h','l','c','v')):return fail('REST_STREAM_BAR_VALUE_MISMATCH')
   self.last_reason[s]='BAR_OVERLAP_OBSERVED'
   return {'proven':True,'reason':'BAR_OVERLAP_OBSERVED','witness_count':len(live),'first':first.isoformat(),'last':last.isoformat(),'generation':self.generation,'TRADE_completeness':False}
 def __call__(self,s,one,five,now):return self.evidence(s,one,now)['proven']
 def snapshot(self):
  with self.lock:return {'schema':'D_BAR_WITNESS_V2','generation':self.generation,'started':None if self.started is None else self.started.isoformat(),'valid':self.valid,'scope':sorted(self.scope),'invalid':dict(self.invalid),'capacity_per_symbol':self.capacity,'rows':{s:list(r) for s,r in self.rows.items()}}
 def restore(self,raw):
  # Stored bytes are audit evidence only; NEVER revive a live connection or its trust.
  if raw.get('schema')!='D_BAR_WITNESS_V2' or len(raw.get('scope',[]))>500:raise ValueError('PROOF_SNAPSHOT_INVALID')
  if not set(raw.get('rows',{}))<=set(raw.get('scope',[])) or not set(raw.get('invalid',{}))<=set(raw.get('scope',[])):raise ValueError('PROOF_SNAPSHOT_SCOPE_INVALID')
  if any(len(rs)>self.capacity for rs in raw.get('rows',{}).values()):raise ValueError('PROOF_SNAPSHOT_OVER_CAP')
  with self.lock:self.archived=json.loads(json.dumps(raw));self.valid=False;self.rows={};self.generation=None;self.scope=set();self.invalid={}
