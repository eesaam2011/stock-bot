"""Fenced chain2 snapshots. Sealed payload bytes are audited only by full_verify."""
import copy,json,threading
from r4a.durable import PREFIX,canonical,sha
from dintegration.persistence import StateConflict
SEGMENT_BYTES=16*1024*1024
RECORD_BYTES=2097152
SESSION_WARN_BYTES=150*1024*1024
FIELD_MARGIN_BYTES=512
WAIVED_SESSIONS=['2026-11-03','2026-11-04']
class ChainFailure(StateConflict):pass
class EvidenceRecordTooLarge(ChainFailure):
 def __init__(self,kind,size):
  self.kind=kind;self.size=size
  super().__init__('EVIDENCE_RECORD_TOO_LARGE kind='+kind+' bytes='+str(size)+' limit='+str(RECORD_BYTES))
def kindof(event):return str(event.get('kind') or event.get('state') or event.get('reason') or event.get('schema') or 'UNCLASSIFIED')
def empty():return {'U':[],'R':[],'proof':None,'control_event':None,'controller':None,'case_refs':{'O':[],'A':[]}}
def reduce_event(state,event):
 out=copy.deepcopy(state)
 if event.get('case_schema')!=2:out['R']=sorted(set(out['R'])|set(event.get('pending_R',[])))
 for name in ('U','R'):out[name]=sorted((set(out[name])|set(event.get('add_'+name,[])))-set(event.get('resolved_'+name,[])))
 if 'proof' in event:out['proof']=copy.deepcopy(event['proof'])
 if 'control_event' in event:out['control_event']=copy.deepcopy(event['control_event'])
 controller=event.get('controller',event.get('epoch'))
 if controller is None and event.get('schema')=='D_REVIEW_EPOCH_V1':controller=event
 if controller is not None:out['controller']=copy.deepcopy(controller)
 if 'case_refs' in event:out['case_refs']=copy.deepcopy(event['case_refs'])
 return out
def checked(raw):
 try:
  x=json.loads(raw);h=x.pop('sha256');assert sha(canonical(x))==h
 except Exception as e:raise ChainFailure('CHAIN2_DIGEST_INVALID') from e
 return {**x,'sha256':h}
def encode(data):
 payload={k:v for k,v in data.items() if k!='sha256'}
 return canonical({**payload,'sha256':sha(canonical(payload))})
APPEND='''
-- chain2 commit: all structural checks before mutation, no cjson dependency.
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('GET',KEYS[2])~=ARGV[2] then return -10 end
local old=redis.call('GET',KEYS[3]) or ''
if old~=ARGV[3] then return -20 end
local function typ(k) return redis.call('TYPE',k).ok end
for _,i in ipairs({3,4,7,8}) do local t=typ(KEYS[i]);if t~='none' and t~='string' then return -30 end end
local t=typ(KEYS[5]);if t~='none' and t~='hash' then return -30 end
local mode=ARGV[4]
if mode=='rotate' then
 if typ(KEYS[6])~='none' or typ(KEYS[9])~='hash' then return -30 end
 if redis.call('HLEN',KEYS[9])~=tonumber(ARGV[14]) then return -30 end
elseif mode=='append' then
 if typ(KEYS[5])~='hash' or redis.call('HLEN',KEYS[5])~=tonumber(ARGV[14]) then return -30 end
elseif mode~='init' or typ(KEYS[5])~='none' then return -30 end
if redis.call('EXISTS',KEYS[5])==1 and redis.call('HGET',KEYS[5],ARGV[7]) then return -30 end
local latest=redis.call('GET',KEYS[7]);if latest and latest>ARGV[12] then return -30 end
if mode=='rotate' then redis.call('SET',KEYS[6],ARGV[11]) end
if mode~='append' then redis.call('HSET',KEYS[5],ARGV[5],ARGV[6]) end
redis.call('HSET',KEYS[5],ARGV[7],ARGV[8])
redis.call('SET',KEYS[4],ARGV[10]);redis.call('SET',KEYS[3],ARGV[9]);redis.call('SET',KEYS[7],ARGV[12])
local waived=0
if mode=='init' and redis.call('EXISTS',KEYS[8])==0 then redis.call('SET',KEYS[8],ARGV[13]);waived=1 end
return {1,waived}
'''
class ChainState:
 def __init__(self,client,session,root_hash,worker,generation,*,segment_bytes=SEGMENT_BYTES,emit=print):
  import re
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',session) or not re.fullmatch('[a-f0-9]{64}',root_hash):raise ChainFailure('CHAIN2_IDENTITY_INVALID')
  self.r=client;self.session=session;self.root_hash=root_hash;self.worker=worker;self.generation=generation
  self.base=PREFIX+':chain2:'+session;self.head_key=self.base+':head';self.checkpoint_key=self.base+':checkpoint'
  self.leader=PREFIX+':canonical:operational_priority_radar:v1:runtime:leader';self.gen=self.leader+'_generation'
  self.segment_bytes=segment_bytes;self.emit=emit;self.seq=0;self.previous=None;self.restored=False;self.head_raw=None;self.head=None;self.archive=empty();self.case_refs=None
  self.stats={'count':0,'bytes':0,'largest_record_bytes':0,'by_kind':{}};self.warned=False;self._metrics_cache={};self.lock=threading.RLock();self._r4b_lock=threading.RLock()
 def key(self,n):return self.base+':segment:'+str(n)
 def seal_key(self,n):return self.base+':seal:'+str(n)
 @property
 def keys(self):return [self.head_key,self.checkpoint_key,self.key(self.head['segment'] if self.head else 1)]
 def _get(self,k):
  try:return self.r.get(k)
  except Exception as e:raise ChainFailure('CHAIN2_READ_FAILED key='+k+' type='+type(e).__module__+'.'+type(e).__name__) from e
 def _fence(self):
  owner,g=self.r.mget([self.leader,self.gen])
  if owner!=self.worker or str(g)!=str(self.generation):raise ChainFailure('CHAIN2_FENCE_LOST')
 def _exists(self,k):
  # Small read-only Lua touches chain2 only; does not read sealed payload fields.
  try:return int(self.r.eval("return redis.call('EXISTS',KEYS[1])",1,k))
  except Exception as e:raise ChainFailure('CHAIN2_READ_FAILED key='+k+' type='+type(e).__name__) from e
 def _read_records(self,head):
  previous=head['anchor_sha256'];archive=None;last=None
  k=self.key(head['segment'])
  for n in range(head['first_sequence'],head['sequence']+1):
   try:raw=self.r.hget(k,str(n))
   except Exception as e:raise ChainFailure('CHAIN2_READ_FAILED key='+k+' type='+type(e).__name__) from e
   if raw is None:raise ChainFailure('CHAIN2_RECORD_MISSING key='+k+' sequence='+str(n))
   x=checked(raw)
   if x['sequence']!=n or x['previous_sha256']!=previous or x['session']!=self.session or x['root_hash']!=self.root_hash:raise ChainFailure('CHAIN2_RECORD_IDENTITY_CONFLICT')
   if n==head['first_sequence']:
    if x['epoch'].get('kind')!='CHAIN2_SEGMENT_START':raise ChainFailure('CHAIN2_ANCHOR_MISSING')
    if x['epoch'].get('previous_segment_sha256')!=head['anchor_sha256']:raise ChainFailure('CHAIN2_ANCHOR_LINK_CONFLICT')
    archive=copy.deepcopy(x['epoch']['state_before'])
    if head['segment']==1 and archive!=empty():raise ChainFailure('CHAIN2_INITIAL_STATE_CONFLICT')
   else:archive=reduce_event(archive,x['epoch'])
   previous=x['sha256'];last=x
  if previous!=head['last_sha256']:raise ChainFailure('CHAIN2_HEAD_CONFLICT')
  return archive,last
 def _frontier(self,head):
  if self._exists(self.key(head['segment']+1)) or self._get(self.seal_key(head['segment'])) is not None:raise ChainFailure('CHAIN2_PARTIAL_ROTATION')
  k=self.key(head['segment']);count=int(self.r.eval("return redis.call('HLEN',KEYS[1])",1,k))
  if count!=head['sequence']-head['first_sequence']+1:raise ChainFailure('CHAIN2_PARTIAL_COMMIT key='+k)
 def _canonical_refs(self,archive):
  from state_store import validate_record,record_key
  refs=archive.get('case_refs',{})
  for group,typ in (('O','opportunity'),('A','trade')):
   keys=refs.get(group,[])
   if not isinstance(keys,list) or not all(isinstance(k,str) and k.startswith('operational_priority_radar:v1:'+typ+':') for k in keys):raise ChainFailure('CHAIN2_CASE_REFS_INVALID')
   if not keys:continue
   physical=[PREFIX+':canonical:'+k for k in keys]
   try:values=self.r.mget(physical)
   except Exception as exc:raise ChainFailure('CHAIN2_CASE_READ_FAILED key='+self.checkpoint_key+' type='+type(exc).__name__) from exc
   if len(values)!=len(keys):raise ChainFailure('CHAIN2_CASE_READ_COUNT_INVALID')
   for key,raw in zip(keys,values):
    if raw is None:raise ChainFailure('CHAIN2_CANONICAL_CASE_MISSING key='+PREFIX+':canonical:'+key)
    try:
     row=json.loads(raw);validate_record(row,typ)
     if record_key(row)!=key:raise ValueError('identity')
    except Exception as exc:raise ChainFailure('CHAIN2_CANONICAL_CASE_INVALID key='+PREFIX+':canonical:'+key) from exc
 def restore(self):
  with self.lock:
   raw=self._get(self.head_key)
   if self.restored and raw==self.head_raw:
    if raw is not None:
     latest=self._get(PREFIX+':chain2:latest')
     if latest is None or latest<self.session:raise ChainFailure('CHAIN2_INDEX_CONFLICT key='+PREFIX+':chain2:latest')
     if self._get(PREFIX+':chain2:waiver')!=canonical({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS}):raise ChainFailure('CHAIN2_WAIVER_CONFLICT key='+PREFIX+':chain2:waiver')
     if self.head['session']!=self.session or self.head['root_hash']!=self.root_hash:raise ChainFailure('CHAIN2_ROOT_OR_SESSION_CONFLICT')
     self._frontier(self.head)
     self._canonical_refs(self.archive)
     if self.worker!='readonly':self._fence()
     if not self._exists(self.key(self.head['segment'])):raise ChainFailure('CHAIN2_SEGMENT_MISSING')
     for meta in self.head['sealed']:
      seal=checked(self._get(self.seal_key(meta['segment'])) or '{}')
      if seal['sha256']!=meta['sha256'] or not self._exists(self.key(meta['segment'])):raise ChainFailure('CHAIN2_SEAL_CONFLICT')
     if sha(self._get(self.checkpoint_key) or '')!=self.head['checkpoint_sha256']:raise ChainFailure('CHAIN2_CHECKPOINT_CONFLICT')
    return self._result()
   if raw is None:
    if self.head is not None or self._get(PREFIX+':chain2:latest')==self.session:raise ChainFailure('CHAIN2_HEAD_MISSING key='+self.head_key)
    if self._get(self.checkpoint_key) is not None or self._exists(self.key(1)):raise ChainFailure('CHAIN2_PARTIAL_INITIALIZATION')
    self.restored=True;return None
   latest=self._get(PREFIX+':chain2:latest')
   if latest is None or latest<self.session:raise ChainFailure('CHAIN2_INDEX_CONFLICT key='+PREFIX+':chain2:latest')
   if self._get(PREFIX+':chain2:waiver')!=canonical({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS}):raise ChainFailure('CHAIN2_WAIVER_CONFLICT key='+PREFIX+':chain2:waiver')
   head=checked(raw)
   if head['segment']<1 or [m['segment'] for m in head['sealed']]!=list(range(1,head['segment'])):raise ChainFailure('CHAIN2_SEGMENT_MANIFEST_INVALID')
   if head['session']!=self.session or head['root_hash']!=self.root_hash:raise ChainFailure('CHAIN2_ROOT_OR_SESSION_CONFLICT')
   prev=None
   for meta in head['sealed']:
    sk=self.seal_key(meta['segment']);seal=checked(self._get(sk) or '{}')
    if seal['sha256']!=meta['sha256'] or seal['previous_seal_sha256']!=prev or seal['session']!=self.session or seal['root_hash']!=self.root_hash:raise ChainFailure('CHAIN2_SEAL_CONFLICT key='+sk)
    if not self._exists(self.key(meta['segment'])):raise ChainFailure('CHAIN2_SEGMENT_MISSING key='+self.key(meta['segment']))
    prev=seal['sha256']
   if head['sealed']:
    if seal['last_sha256']!=head['anchor_sha256'] or seal['last_sequence']+1!=head['first_sequence']:raise ChainFailure('CHAIN2_PREVIOUS_LINK_CONFLICT')
   elif head['anchor_sha256'] is not None:raise ChainFailure('CHAIN2_INITIAL_ANCHOR_INVALID')
   self._frontier(head)
   checkpoint=self._get(self.checkpoint_key)
   if checkpoint is None or sha(checkpoint)!=head['checkpoint_sha256']:raise ChainFailure('CHAIN2_CHECKPOINT_CONFLICT')
   archive,last=self._read_records(head)
   cp=checked(checkpoint)
   if cp['sequence']!=head['sequence'] or cp['last_sha256']!=head['last_sha256'] or cp['archive']!=archive:raise ChainFailure('CHAIN2_REPLAY_CONFLICT')
   count=int(self.r.eval("return redis.call('HLEN',KEYS[1])",1,self.key(head['segment'])))
   if count!=head['sequence']-head['first_sequence']+1:raise ChainFailure('CHAIN2_PARTIAL_COMMIT')
   if self._get(self.head_key)!=raw:raise ChainFailure('CHAIN2_HEAD_CHANGED_DURING_RESTORE')
   self._canonical_refs(archive)
   self.current_checkpoint_bytes=len(checkpoint.encode());self.head=head;self.head_raw=raw;self.seq=head['sequence'];self.previous=head['last_sha256'];self.archive=archive;self.restored=True
   return self._result()
 def _result(self):
  if self.head is None:return None
  return {'checkpoint':{'sequence':self.seq,'session':self.session,'root_hash':self.root_hash,'sha256':self.previous},'transport_trusted':False,'requires_recovery':True}
 def _record(self,event,n,previous):
  raw=encode({'sequence':n,'previous_sha256':previous,'session':self.session,'root_hash':self.root_hash,'worker':self.worker,'generation':self.generation,'epoch':event})
  size=len(raw.encode())
  if size>RECORD_BYTES:raise EvidenceRecordTooLarge(kindof(event),size)
  return raw,json.loads(raw)['sha256'],size
 def append_epoch(self,event):
  event=copy.deepcopy(event)
  if self.case_refs is not None and 'case_refs' not in event:event['case_refs']=self.case_refs()
  with self.lock:
   if not self.restored:raise ChainFailure('RESTORE_REQUIRED')
   self._fence()
   raw,h,size=self._record(event,self.seq+1,self.previous)
   mode='append';firstraw='';sealraw='';start=None;head=copy.deepcopy(self.head);seq=self.seq;previous=self.previous;archive=copy.deepcopy(self.archive)
   if head is None or head['active_allocated_bytes']+size+len(str(seq+1))+FIELD_MARGIN_BYTES>self.segment_bytes:
    mode='init' if head is None else 'rotate';segment=1 if head is None else head['segment']+1
    waiver=self._get(PREFIX+':chain2:waiver')
    if waiver is not None and waiver!=canonical({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS}):raise ChainFailure('CHAIN2_WAIVER_CONFLICT key='+PREFIX+':chain2:waiver')
    sealed=[] if head is None else head['sealed'][:];total=0 if head is None else head['record_bytes'];records=0 if head is None else head['record_count']
    if head is not None:
     sealraw=encode({'session':self.session,'root_hash':self.root_hash,'segment':head['segment'],'first_sequence':head['first_sequence'],'last_sequence':head['sequence'],'last_sha256':head['last_sha256'],'previous_seal_sha256':sealed[-1]['sha256'] if sealed else None,'record_bytes':head['active_record_bytes']})
     sealed.append({'segment':head['segment'],'sha256':json.loads(sealraw)['sha256']})
    start={'kind':'CHAIN2_SEGMENT_START','previous_segment_sha256':previous,'state_before':archive,'waived_sessions':WAIVED_SESSIONS if mode=='init' and self._get(PREFIX+':chain2:waiver') is None else []}
    firstraw,firsthash,firstsize=self._record(start,seq+1,previous);seq+=1;previous=firsthash
    head={'schema':'CHAIN2_HEAD_V1','session':self.session,'root_hash':self.root_hash,'segment':segment,'first_sequence':seq,'anchor_sha256':self.previous,'sealed':sealed,'active_record_bytes':firstsize,'active_allocated_bytes':firstsize+len(str(seq))+FIELD_MARGIN_BYTES,'record_bytes':total+firstsize,'record_count':records+1,'sealed_metadata_bytes':(0 if self.head is None else self.head.get('sealed_metadata_bytes',0))+len(sealraw.encode())}
    raw,h,size=self._record(event,seq+1,previous)
   if head['active_allocated_bytes']+size+len(str(seq+1))+FIELD_MARGIN_BYTES>self.segment_bytes:raise ChainFailure('CHAIN2_ANCHOR_AND_RECORD_EXCEED_SEGMENT')
   archive=reduce_event(archive,event);seq+=1
   checkpoint=encode({'session':self.session,'root_hash':self.root_hash,'sequence':seq,'last_sha256':h,'archive':archive})
   if len(checkpoint.encode())>RECORD_BYTES:raise EvidenceRecordTooLarge('CHAIN2_CHECKPOINT',len(checkpoint.encode()))
   oldcount=0 if self.head is None else self.head['sequence']-self.head['first_sequence']+1
   head.update(sequence=seq,last_sha256=h,checkpoint_sha256=sha(checkpoint),active_record_bytes=head['active_record_bytes']+size,active_allocated_bytes=head['active_allocated_bytes']+size+len(str(seq))+FIELD_MARGIN_BYTES,record_bytes=head['record_bytes']+size,record_count=head['record_count']+1)
   head['by_kind_session']=copy.deepcopy((self.head or {}).get('by_kind_session',{}))
   for k,n in ([(kindof(start),len(firstraw.encode()))] if start else [])+[(kindof(event),size)]:
    d=head['by_kind_session'].setdefault(k,{'count':0,'bytes':0});d['count']+=1;d['bytes']+=n
   head['size_warning_crossed']=bool((self.head or {}).get('size_warning_crossed'))
   newraw=encode(head)
   total_now=head['record_bytes']+head.get('sealed_metadata_bytes',0)+len(checkpoint.encode())+len(newraw.encode())
   if total_now>SESSION_WARN_BYTES:head['size_warning_crossed']=True;newraw=encode(head)
   keys=[self.leader,self.gen,self.head_key,self.checkpoint_key,self.key(head['segment']),self.seal_key(head['segment']-1),PREFIX+':chain2:latest',PREFIX+':chain2:waiver',self.key(head['segment']-1)]
   try:
    result=self.r.eval(APPEND,9,*keys,self.worker,str(self.generation),self.head_raw or '',mode,str(head['first_sequence']),firstraw,str(seq),raw,newraw,checkpoint,sealraw,self.session,canonical({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS}),str(oldcount))
   except Exception as exc:raise ChainFailure('CHAIN2_COMMIT_FAILED key='+self.head_key+' type='+type(exc).__module__+'.'+type(exc).__name__) from exc
   if not isinstance(result,list) or result[0]!=1:raise ChainFailure('CHAIN2_COMMIT_REJECTED '+str(result))
   crossed_warning=head['size_warning_crossed'] and not bool((self.head or {}).get('size_warning_crossed'))
   self.head=head;self.head_raw=newraw;self.seq=seq;self.previous=h;self.archive=archive;self.current_checkpoint_bytes=len(checkpoint.encode())
   for e,n in ([(start,len(firstraw.encode()))] if start else [])+[(event,size)]:
    k=kindof(e);v=self.stats['by_kind'].setdefault(k,{'count':0,'bytes':0});v['count']+=1;v['bytes']+=n;self.stats['count']+=1;self.stats['bytes']+=n;self.stats['largest_record_bytes']=max(self.stats['largest_record_bytes'],n)
   if result[1]:self.emit(json.dumps({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS,'mode':'CHAIN2_BOOTSTRAP_NO_LEGACY_IMPORT'}),flush=True)
   session_bytes=head['record_bytes']+head.get('sealed_metadata_bytes',0)+len(checkpoint.encode())+len(newraw.encode())
   if crossed_warning:
    self.warned=True;self.emit(json.dumps({'stage':'EVIDENCE_SESSION_SIZE_WARNING','session':self.session,'session_payload_bytes':session_bytes,'threshold_bytes':SESSION_WARN_BYTES,'action':'CONTINUE'}),flush=True)
   return h
 def metrics(self):
  if not self.lock.acquire(blocking=False):return {**self._metrics_cache,'snapshot_stale_writer_busy':True}
  try:
   head=self.head # No Redis I/O in metrics.
   out={**copy.deepcopy(self.stats),'session':self.session,'session_record_bytes':head['record_bytes'] if head else 0,'session_payload_bytes':(head['record_bytes']+head.get('sealed_metadata_bytes',0)+getattr(self,'current_checkpoint_bytes',0)+len((self.head_raw or '').encode())) if head else 0,'by_kind_session':copy.deepcopy(head.get('by_kind_session',{})) if head else {},'session_byte_scope':'UTF8 record values + seals + current head and checkpoint; excludes Redis allocator/key overhead and shared bootstrap marker','session_records':head['record_count'] if head else 0,'active_segment_bytes':head['active_allocated_bytes'] if head else 0,'active_segment_record_bytes':head['active_record_bytes'] if head else 0,'active_segment_byte_scope':'record_bytes is actual UTF8 values; active_segment_bytes additionally reserves field names + 512 bytes/field','segment_limit_bytes':self.segment_bytes,'record_limit_bytes':RECORD_BYTES,'sealed_payload_bytes_rechecked_at_boot':False,'snapshot_stale_writer_busy':False}
   self._metrics_cache=out;return out
  finally:self.lock.release()
 def archive_state(self):self.restore();return copy.deepcopy(self.archive)

def previous_cases(redis,day):
 k=PREFIX+':chain2:latest'
 try:session=redis.get(k)
 except Exception as e:raise ChainFailure('CHAIN2_READ_FAILED key='+k+' type='+type(e).__name__) from e
 if session is None:
  marker_key=PREFIX+':chain2:waiver'
  try:marker=redis.get(marker_key)
  except Exception as exc:raise ChainFailure('CHAIN2_READ_FAILED key='+marker_key+' type='+type(exc).__name__) from exc
  if marker is not None:raise ChainFailure('CHAIN2_LATEST_MISSING key='+k)
  return set(),set()
 if session>day:raise ChainFailure('CHAIN2_FUTURE_SESSION')
 key=PREFIX+':chain2:'+session+':head'
 try:raw=redis.get(key)
 except Exception as e:raise ChainFailure('CHAIN2_READ_FAILED key='+key+' type='+type(e).__name__) from e
 if raw is None:raise ChainFailure('CHAIN2_HEAD_MISSING key='+key)
 try:
  head=checked(raw);s=ChainState(redis,session,head['root_hash'],'readonly',0);s.restore()
 except Exception as exc:raise ChainFailure('CHAIN2_PREVIOUS_FAILED key='+key+' type='+type(exc).__module__+'.'+type(exc).__name__) from exc
 return set(s.archive['U']),set(s.archive['R'])

def validate_bootstrap(redis,day):
 """Production parent only: an empty new namespace is not a generic future waiver."""
 key=PREFIX+':chain2:latest'
 try:latest=redis.get(key)
 except Exception as exc:raise ChainFailure('CHAIN2_READ_FAILED key='+key+' type='+type(exc).__name__) from exc
 if latest is None:
  marker_key=PREFIX+':chain2:waiver'
  try:marker=redis.get(marker_key)
  except Exception as exc:raise ChainFailure('CHAIN2_READ_FAILED key='+marker_key+' type='+type(exc).__name__) from exc
  if marker is not None:raise ChainFailure('CHAIN2_LATEST_MISSING key='+key)
 if latest is None and day!='2026-11-05':raise ChainFailure('CHAIN2_BOOTSTRAP_DAY_NOT_AUTHORIZED key='+key)
 if latest is not None:
  waiver_key=PREFIX+':chain2:waiver'
  try:waiver=redis.get(waiver_key)
  except Exception as exc:raise ChainFailure('CHAIN2_READ_FAILED key='+waiver_key+' type='+type(exc).__name__) from exc
  if waiver!=canonical({'stage':'U_R_CARRY_WAIVED','sessions':WAIVED_SESSIONS}):raise ChainFailure('CHAIN2_WAIVER_CONFLICT key='+waiver_key)
 if latest is not None and latest>day:raise ChainFailure('CHAIN2_FUTURE_SESSION key='+key)
