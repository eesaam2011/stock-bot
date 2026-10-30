from dataclasses import dataclass,field
import hashlib,json,math
from pathlib import Path
SPEC_SHA='ac787471d49c742051f3a2d10b2064f467c51d46d7b3fbc3fa5d05b4d5bb1a4a'
CAP=500
class Blocked(ValueError):pass
def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def clip(v,a=0.,b=1.):return max(a,min(b,v))
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
@dataclass(frozen=True)
class Root:
 session:str
 symbols:frozenset
 artifact_sha:str
 verified:bool
 def check(self):
  if not self.verified or len(self.artifact_sha)!=64 or any(c not in '0123456789abcdef' for c in self.artifact_sha):raise Blocked('ROOT_NOT_VERIFIED')
 @classmethod
 def from_bytes(cls,raw,expected_sha):
  if hashlib.sha256(raw).hexdigest()!=expected_sha:raise Blocked('ROOT_BYTES_HASH_MISMATCH')
  o=json.loads(raw);symbols=o['symbols']
  if len(symbols)!=len(set(symbols)) or not all(isinstance(x,str) and x for x in symbols):raise Blocked('INVALID_ROOT_SYMBOLS')
  return cls(o['session'],frozenset(symbols),expected_sha,True)
 def identity(self):return digest({'session':self.session,'artifact_sha':self.artifact_sha,'symbols':sorted(self.symbols)})
@dataclass(frozen=True)
class Needs:
 revision:int
 bar:frozenset=field(default_factory=frozenset)
 O:frozenset=field(default_factory=frozenset)
 A:frozenset=field(default_factory=frozenset)
 U:frozenset=field(default_factory=frozenset)
 R:frozenset=field(default_factory=frozenset)
 canonical_complete:bool=True
 @property
 def trades(self):return self.O|self.A|self.U|self.R
 @property
 def pins(self):return self.bar|self.trades
 def fingerprint(self):return digest({'revision':self.revision,'bar':sorted(self.bar),'O':sorted(self.O),'A':sorted(self.A),'U':sorted(self.U),'R':sorted(self.R),'canonical_complete':self.canonical_complete})
 def check(self,root):
  root.check()
  if not self.canonical_complete:raise Blocked('NEEDS_INCOMPLETE')
  if not self.pins<=root.symbols:raise Blocked('NEED_OUTSIDE_ROOT')
  if len(self.pins)>CAP:raise Blocked('PIN_CAP_CONFLICT')




_LIVE_ADAPTER_AUTHORITY=object()
class Controller:
 """One offline control plan in flight; ACK has no invented request id."""
 def __init__(self,root,control_timeout=10):
  root.check()
  if control_timeout!=10:raise Blocked('CONTROL_LIMIT_CHANGED')
  self.root=root;self.connection=0;self.revision=0;self.needs_revision=-1;self.bar=set();self.trade=set();self.desired_bar=set();self.desired_trade=set();self.inflight=None;self.failed=True;self._transport_trusted=False;self.proofs={};self.needs=None;self.journal=[]
 @property
 def transport_trusted(self):return self._transport_trusted
 def _set_transport_from_adapter(self,value,authority):
  if authority is not _LIVE_ADAPTER_AUTHORITY:raise Blocked('LIVE_ADAPTER_AUTHORITY_REQUIRED')
  self._transport_trusted=bool(value)
 def reconnect(self):
  self.connection+=1;self.bar=set();self.trade=set();self.inflight=None;self.proofs={};self.failed=False;self._transport_trusted=False;self._log('reconnect')
 def set_desired(self,bar,needs):
  try:needs.check(self.root)
  except Blocked:
   self.failed=True;self._transport_trusted=False;raise
  bar=set(bar)
  if needs.revision<self.needs_revision or (self.needs and needs.revision==self.needs_revision and needs!=self.needs):
   self.failed=True;self._transport_trusted=False;raise Blocked('NEEDS_REVISION_CONFLICT')
  if not needs.pins<=bar or not bar<=self.root.symbols or len(bar)>CAP:
   self.failed=True;self._transport_trusted=False;raise Blocked('INVALID_BAR_TARGET')
  if self.inflight and (needs.pins-self.inflight['after_bar'] or needs.trades-self.inflight['after_trade']):
   self.failed=True;self._transport_trusted=False;raise Blocked('NEW_NEED_CONFLICTS_WITH_INFLIGHT_REQUIRES_RECOVERY')
  self.revision+=1;self.needs_revision=needs.revision;self.needs=needs;self.desired_bar=bar;self.desired_trade=set(needs.trades)
  for s in list(self.proofs):
   if s not in bar:self.proofs.pop(s)
  self._log('desired')
 def plan(self,now):
  if not finite(now):raise Blocked('INVALID_CLOCK')
  if self.failed:raise Blocked('CONTROL_UNTRUSTED')
  if self.inflight:return None
  remove_bar=self.bar-self.desired_bar;remove_trade=self.trade-self.desired_trade
  if remove_bar or remove_trade:
   action='unsubscribe';bs=remove_bar;ts=remove_trade;ab=self.bar-bs;at=self.trade-ts
  else:
   action='subscribe';bs=self.desired_bar-self.bar;ts=self.desired_trade-self.trade;ab=self.bar|bs;at=self.trade|ts
  if not bs and not ts:return None
  if len(ab)>CAP:raise Blocked('TRANSIENT_BAR_CAP')
  self.inflight={'action':action,'bars':sorted(bs),'trades':sorted(ts),'after_bar':ab,'after_trade':at,'sent_at':now,'connection':self.connection,'revision':self.revision}
  self._log('control_sent',now)
  return {'action':action,'bars':sorted(bs),'trades':sorted(ts)}
 def ack(self,connection,bars,trades,now):
  if not finite(now):self.failed=True;self._transport_trusted=False;return False
  if connection!=self.connection:return False
  p=self.inflight
  if self.failed or not p:return False
  if now<p['sent_at'] or now-p['sent_at']>10 or set(bars)!=p['after_bar'] or set(trades)!=p['after_trade']:
   self.failed=True;self._transport_trusted=False;self._log('ack_rejected',now);return False
  old=self.bar;self.bar=set(bars);self.trade=set(trades);self.inflight=None
  for s in self.bar-old:self.proofs.pop(s,None)
  self._log('ack_confirmed',now)
  return True
 def tick(self,now):
  if not finite(now):self.failed=True;self._transport_trusted=False;raise Blocked('INVALID_CLOCK')
  if self.inflight and now-self.inflight['sent_at']>10:
   self.failed=True;self._transport_trusted=False;self._log('control_timeout',now)
 def attest(self,s,*,history,continuity,reconciled,first_trade=False):
  if s not in self.bar or self.failed:raise Blocked('NO_CONFIRMED_BAR')
  # Evidence flags must be supplied by audited adapters; this component cannot verify market truth.
  self.proofs[s]={'connection':self.connection,'history':history is True,'continuity':continuity is True,'reconciled':reconciled is True,'first_trade':first_trade is True}
  self._log('symbol_evidence')
 def can_entry(self,s):
  p=self.proofs.get(s,{})
  return bool(not self.failed and self.transport_trusted and self.needs and s in self.needs.O and s in self.bar and s in self.trade and p.get('connection')==self.connection and all(p.get(k) for k in ['history','continuity','reconciled','first_trade']))
 def epoch(self):
  return {'schema':'D_REVIEW_EPOCH_V1','session':self.root.session,'root_hash':self.root.identity(),'selector_spec_sha256':SPEC_SHA,'connection_generation':self.connection,'revision':self.revision,'needs_revision':self.needs_revision,'desired_bar':sorted(self.desired_bar),'confirmed_bar':sorted(self.bar),'desired_trade':sorted(self.desired_trade),'confirmed_trade':sorted(self.trade),'desired_bar_sha256':digest(sorted(self.desired_bar)),'confirmed_bar_sha256':digest(sorted(self.bar)),'desired_trade_sha256':digest(sorted(self.desired_trade)),'confirmed_trade_sha256':digest(sorted(self.trade)),'inflight':None if not self.inflight else {k:sorted(v) if isinstance(v,set) else v for k,v in self.inflight.items()},'transport_trusted':self.transport_trusted,'control_failed':self.failed,'symbol_evidence':json.loads(json.dumps(self.proofs)),'evidence_authority':'external audited adapters required; booleans alone not production trust'}

 def _log(self,kind,now=None):
  entry={'sequence':len(self.journal)+1,'event':kind,'observed_time':now,'previous_sha256':self.journal[-1]['sha256'] if self.journal else None,'epoch':self.epoch()}
  entry['sha256']=digest(entry);self.journal.append(entry)
 def journal_jsonl(self):
  return ''.join(json.dumps(x,sort_keys=True,allow_nan=False)+'\n' for x in self.journal)

 def source_error(self,code):
  self.failed=True;self._transport_trusted=False;self._log('source_error_'+str(int(code)))
