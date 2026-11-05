"""Offline restore measurement; not Render/Upstash latency."""
import sys,json,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent;sys.path[:0]=[str(ROOT),str(ROOT/'reference_app'),str(ROOT/'tests')];sys.dont_write_bytecode=True
from day_backend import DiskRedis
from r9core.chain import ChainState
from datetime import datetime,timedelta,timezone
with tempfile.TemporaryDirectory() as td:
 r=DiskRedis(td);s=ChainState(r,'2026-11-05','a'*64,'w',1,emit=lambda *a,**k:None);r.values[s.leader]='w';r.values[s.gen]='1';s.restore()
 op=datetime(2026,11,5,14,30,tzinfo=timezone.utc);symbols=['S%05d'%i for i in range(500)];generation='g'*32
 proof={'schema':'D_BAR_WITNESS_V2','generation':generation,'started':op.isoformat(),'valid':True,'scope':symbols,'invalid':{},'capacity_per_symbol':8,'rows':{symbol:[{'bar':{'t':(op+timedelta(minutes=i)).isoformat(),'o':10.12,'h':10.25,'l':10.0,'c':10.2,'v':100000},'received_at':(op+timedelta(minutes=i+1,microseconds=123456)).isoformat(),'generation':generation} for i in range(8)] for symbol in symbols}}
 for i in range(20):s.append_epoch({'kind':'B_INCREMENTAL_ADMISSION','accepted_symbols':symbols[:100],'proof':proof})
 before=len(r.fields_read);t=ChainState(r,s.session,s.root_hash,'w',1);start=time.monotonic();t.restore();elapsed=time.monotonic()-start
 result={'test':'OFFLINE_RESTORE_FULL_SNAPSHOT_ACTIVE_SEGMENT','active_record_bytes':s.head['active_record_bytes'],'active_segment_allocated_bytes':s.head['active_allocated_bytes'],'segments':s.head['segment'],'HGET':len(r.fields_read)-before,'restore_seconds':elapsed,'scope':'Disk contract fake; uncapped local CPU; no Redis RTT or 512Mi/0.5CPU claim'}
 Path('RESTORE_BENCHMARK_R9_CORE.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
