import asyncio,json,time,uuid,threading,os
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from . import http as requests
from dreview.core import Root,SPEC_SHA
from r4a.runtime import compose
from r4a.guards import cgroup_sample
from r4b.adapter import EvidenceAdapter
from r4b.cases import CaseBarrier
from r4b.runtime import EvidenceRuntime
from dintegration.stream_proof import StreamProof
from dintegration.clock import ClockProof
from dintegration.measure import Measurements
from dintegration.rest import MarketREST
from dintegration.transport import SocketTransport
from restart_budget import SessionPolicy
from production_halt_status import ProductionStatusTracker
from .diagnostics import reason
from .data import dt,digest,prepare_history,SessionVolumes
from .preparation import save,load,carried
from .classifier import Classifier
from .connectors import FloatReader
from .news_batch import BatchNews
from .ranking import base_score,Selection
from .sender import Sender

class DirectEvidence(EvidenceAdapter):
 def recover(self,now):
  ok=super().recover(now)
  print(json.dumps({'stage':'TRANSPORT_TRUSTED' if ok else 'RECOVERY_PENDING','at':self.wall().isoformat(),'reason':self.report.get('reason'),'good_symbols':len(self.report.get('good',{})),'quarantined_symbols':len(self.quarantine),'TRADE_completeness':'UNKNOWN'}),flush=True)
  return ok
class DirectRuntime(EvidenceRuntime):
 def acknowledged(self):
  super().acknowledged()
  print(json.dumps({'stage':'SUBSCRIPTION_ACK','at':self.wall().isoformat(),'connection':self.c.connection,'revision':self.c.revision,'BAR_confirmed':len(self.c.bar),'TRADE_confirmed':len(self.c.trade),'statuses_ACK_proven':True}),flush=True)
 def checkpoint(self,why):
  super().checkpoint(why)
  if why=='CONTROL_INTENT' and self.c.inflight:
   f=self.c.inflight
   print(json.dumps({'stage':'SUBSCRIPTION_INTENT','at':self.wall().isoformat(),'action':f['action'],'bars':f['bars'],'trades':f['trades'],'statuses_unsubscribe_requested':False}),flush=True)

def policy_for(bundle):
 day=bundle['session']
 return SessionPolicy(day,day+'T00:00:00Z',day+'T23:59:59Z',day,digest({'symbols':bundle['symbols'],'assets':bundle['assets'],'discovery_spec_sha256':SPEC_SHA}),len(bundle['symbols']))

async def fixed_connect(uri):
 from websockets.asyncio.client import connect
 import logging
 if uri!=SocketTransport.URL:raise ValueError('WS_HOST_FORBIDDEN')
 class NoRedirect(connect):
  def process_redirect(self,exc):return exc
 logger=logging.getLogger('IPR_WS_REDACTED');logger.handlers=[logging.NullHandler()];logger.propagate=False;logger.setLevel(logging.CRITICAL)
 return await NoRedirect(uri,proxy=None,logger=logger)

def build(bundle,api,redis,env,boot):
 import runtime_provenance
 runtime_provenance.RUNTIME_MODE='ALERTS_ONLY';runtime_provenance.SHADOW_MODE=False;runtime_provenance.RELEASE_ID='IPR_DIRECT_R1'
 policy=policy_for(bundle);root=Root(bundle['session'],frozenset(bundle['symbols']),policy.frozen_manifest_sha256,True)
 cls=Classifier(bundle['assets']);clock=ClockProof(redis.time)
 if not clock.refresh():raise ValueError('CLOCK_UNPROVEN')
 measure=Measurements();rest=MarketREST(api.get,root,measure);prepared={'ready':False,'history':None,'scan_ok':False}
 def authorize():
  if not policy.time_allows(datetime.now(timezone.utc).timestamp()):raise ValueError('SESSION_CLOSED')
 def final_filter(s,m,now):
  # New entry price admission only; the original monitor still receives allow=False.
  if not prepared['ready'] or not prepared['scan_ok'] or s not in bundle['eligible']:return False
  try:return cls.eligible(s) and 1<=float(m['p'])<=50 and 0<=(now-dt(m['t'])).total_seconds()<=60
  except Exception:return False
 class Status:
  def __init__(self):self.original=ProductionStatusTracker()
  def on_status(self,m):return self.original.ingest(m)
 market=bundle['market'];schedule={'session':bundle['session'],'open_utc':market['open'],'close_utc':market['close']} if market else None
 base=compose(redis=redis,root=root,worker=uuid.uuid4().hex,boot_id=boot,policy=policy,session_policy=schedule,rest=rest,recovery_factory=lambda b:Status(),clock_check=clock.check,filter_check=final_filter,root_check=lambda:True,measure=measure,socket_open=SocketTransport(authorize,lambda:(env['APCA_API_KEY_ID'],env['APCA_API_SECRET_KEY']),connect=fixed_connect).open,authorize=authorize,sample=cgroup_sample)
 proof=StreamProof();barrier=CaseBarrier(base.a.bridge,proof)
 a=DirectEvidence(base.c,base.a.bridge,rest,base.a.recovery,base.a.clock_check,base.a.filter_check,base.a.root_check,base.state,measure,proof=proof,barrier=barrier,wall=base.wall)
 r=DirectRuntime(a,base.state,base.leader,base.socket_open,authorize,policy,clock=base.clock,wall=base.wall,sample=base.sample)
 r.store.load_archive();a.bridge.p._entry_uncertain.update(r.store.pending_U);barrier.persisted_R.update(r.store.pending_R)
 if base.state.seq==0:
  u,rec=carried(redis,bundle['session']);a.checkpoint.append_epoch({'kind':'DIRECT_CARRIED_CASES','case_schema':2,'add_U':sorted(u),'add_R':sorted(rec)})
  a.bridge.p._entry_uncertain.update(u);barrier.persisted_R.update(rec)
 r.boot_number=base.boot_number
 return r,prepared,clock,measure

async def pause(stop,seconds):
 try:await asyncio.wait_for(stop.wait(),timeout=max(0.,seconds))
 except asyncio.TimeoutError:pass

async def run(bundle,api,redis,env,boot):
 r,state,clock,measure=build(bundle,api,redis,env,boot);p=r.a.bridge.p
 sender=Sender(r,redis,env);floats=FloatReader(env,requests.get);news=BatchNews(env,requests.post);selection=Selection()
 print(json.dumps({'stage':'GUARD_BOOT_CLAIMED','boot':r.boot_number,'max_boots':4,'mode':'TELEGRAM_ALERTS_ONLY'}),flush=True)
 async def prepare():
  def work():
   floats.refresh();news.count()
   count=sum(floats.value(s)['status']=='VALID' for s in bundle['eligible'])
   summary_key='sources_reported:'+bundle['session']
   # NX suppresses repeated session summaries across child restarts.
   if redis.set('ipr_direct:v1:'+summary_key,'1',nx=True):print(json.dumps({'stage':'IPR_SESSION_SOURCES','session':bundle['session'],'float_valid':count,'float_unknown':len(bundle['eligible'])-count,'float_load_reason':floats.reason,'news_records':news.present,'news_read_reason':news.error}),flush=True)
   if not bundle['new_entries_prepared']:raise ValueError('ROOT_PREPARATION_FAILED')
   h=load(redis,'history:'+bundle['session'])
   if not h:
    previous=load(redis,'rolling_history')
    h=prepare_history(api,bundle['eligible'],bundle['calendar'],bundle['session'],r.wall(),lambda row:print(json.dumps(row),flush=True),previous=previous);save(redis,'rolling_history',h['daily_aggregates']);h['symbols_hash']=digest(bundle['eligible']);save(redis,'history:'+bundle['session'],h)
   if h.get('session')!=bundle['session'] or h.get('symbols_hash')!=digest(bundle['eligible']):raise ValueError('HISTORY_CACHE_SCOPE_MISMATCH')
   state['history']=h;state['ready']=True
   print(json.dumps({'stage':'PREP_READY','symbols_with_history':len(h['features']),'symbols_without_history':len(bundle['eligible'])-len(h['features'])}),flush=True)
  while not r.stop.is_set() and not state['ready']:
   try:await asyncio.to_thread(work)
   except Exception as e:
    state['ready']=False;state['scan_ok']=False;print(json.dumps({'stage':'PREP_FAILED_NEW_ENTRIES_DISABLED','reason':reason(e)}),flush=True);await pause(r.stop,300.)
  await r.stop.wait()
 async def scan():
  volumes=None
  while not r.stop.is_set():
   start=time.monotonic();market=bundle['market'];now=r.wall()
   if state['ready'] and market and dt(market['open'])<=now<dt(market['close']):
    try:
     def work():
      nonlocal volumes
      if volumes is None:volumes=SessionVolumes('/tmp/ipr_rth_'+bundle['session']+'.sqlite',bundle['calendar'],bundle['session'])
      totals=volumes.update(api,bundle['eligible'],r.wall());snaps=r.a.rest.snapshot_envelopes(bundle['eligible'],r.wall);at=r.wall();scores={}
      for s,e in snaps.items():
       row=base_score(s,e['data'],state['history']['features'].get(s),totals.get(s),at,dt(market['open']),floats)
       if row:scores[s]=row
      top=sorted(scores,key=lambda s:(-scores[s]['score95'],s))[:750];news.refresh(top)
      for s,row in scores.items():
       n=news.read(s) if s in top else {};pts=5 if n.get('positive') and n.get('bonus',0)>0 else 0
       row['score']=round(row['score95']+pts,12);row['parts']['news']=pts
      # Source price freshness is rechecked AFTER possibly slow HMGET requests.
      end=r.wall()
      if (end-volumes.watermark).total_seconds()>60:raise ValueError('RTH_VOLUME_STALE')
      scores={s:v for s,v in scores.items() if 0<=(end-dt(v['snapshot_ts'])).total_seconds()<=60}
      with p._decision_lock:needs=r.a.bridge.needs(recovery_symbols=r.a.barrier.persisted_R)
      target=selection.update(scores,needs.pins,time.monotonic())
      with r.a.control_lock:r.planner.propose(needs,set(target))
      save(redis,'scan:'+bundle['session'],{'at':end.isoformat(),'selected':target,'root_symbols':len(bundle['eligible']),'scores':{s:scores[s] for s in target if s in scores},'rth_watermark':volumes.watermark.isoformat(),'linear_approximation':True})
      state['scan_ok']=True;state['scan_at']=r.wall()
      print(json.dumps({'stage':'IPR_SCAN','at':end.isoformat(),'ranked':len(scores),'BAR_desired':len(target),'news_candidates':len(top),'api_requests':api.requests,'duration_seconds':time.monotonic()-start}),flush=True)
     await asyncio.to_thread(work)
    except Exception as e:
     state['scan_ok']=False;print(json.dumps({'stage':'SCAN_FAILED_NEW_ENTRIES_DISABLED','reason':reason(e)}),flush=True)
   await pause(r.stop,max(1.,60-(time.monotonic()-start)))
 async def native():
  while not r.stop.is_set():
   market=bundle['market'];now=r.wall()
   if state['ready'] and state['scan_ok'] and market and dt(market['open'])<=now<dt(market['close']) and r.a.transport_trusted:
    with r.a.control_lock:syms=[s for s in r.c.bar if s not in r.a.recovering and all(r.c.proofs.get(s,{}).get(k) for k in ('history','continuity','reconciled'))]
    if syms:await asyncio.to_thread(r.a.bridge.native_cycle,syms,now,True)
   await pause(r.stop,5.)
 async def upkeep():
  while not r.stop.is_set():
   await asyncio.to_thread(p.expire_entry_opportunities,r.wall());r.notify_cases_changed()
   if state.get('scan_at') and (r.wall()-state['scan_at']).total_seconds()>60:state['scan_ok']=False
   await pause(r.stop,1.)
 async def clock_loop():
  while not r.stop.is_set():
   if not await asyncio.to_thread(clock.refresh):r.revoke('CLOCK_UNPROVEN')
   await pause(r.stop,5.)
 async def metrics():
  while not r.stop.is_set():
   print(json.dumps({'stage':'IPR_METRICS','at':r.wall().isoformat(),'resource':measure.sample(),'ws_ingress':r.ingress.snapshot() if r.ingress else None,'trusted':r.a.transport_trusted,'prep_ready':state['ready'],'scan_ok':state['scan_ok']}),flush=True)
   await pause(r.stop,60.)
 import signal
 loop=asyncio.get_running_loop()
 for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,r.stop.set)
 tasks=[asyncio.create_task(f()) for f in (r.run,prepare,scan,native,upkeep,clock_loop,metrics,sender.run,lambda:measure.loop_lag(r.stop))]
 try:
  done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
  for t in done:t.result()
 finally:
  r.revoke('DIRECT_STOPPING');r.stop.set()
  for t in tasks:t.cancel()
  try:await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),timeout=60.)
  except asyncio.TimeoutError:raise RuntimeError('PARENT_KILL_REQUIRED')
