import json,tempfile,time,unittest
from collections import deque
from datetime import datetime,timedelta,timezone
from pathlib import Path
from day_backend import DiskRedis
from r9core.chain import ChainState,SEGMENT_BYTES,RECORD_BYTES,canonical
from r9core.audit import full_verify
class WholeDay(unittest.TestCase):
 def test_500_symbols_390_minutes_60_admissions_per_hour_disconnects(self):
  with tempfile.TemporaryDirectory() as td:
   r=DiskRedis(td);logs=[];s=ChainState(r,'2026-11-05','a'*64,'w',1,emit=lambda x,**kw:logs.append(json.loads(x)))
   r.values[s.leader]='w';r.values[s.gen]='1';s.restore();start=time.monotonic();op=datetime(2026,11,5,14,30,tzinfo=timezone.utc)
   symbols=['S%05d'%i for i in range(500)];gen=0;disconnects=0
   expected_U=set();expected_R=set()
   for minute in range(390):
    if minute%30==0:gen+=1
    generation=('%032d'%gen);rows={}
    for symbol in symbols:
     rows[symbol]=[{'bar':{'t':(op+timedelta(minutes=minute-7+j)).isoformat(),'o':10.12,'h':10.25,'l':10.0,'c':10.2,'v':100000+minute},'received_at':(op+timedelta(minutes=minute-6+j,microseconds=123456)).isoformat(),'generation':generation} for j in range(8)]
    proof={'schema':'D_BAR_WITNESS_V2','generation':generation,'started':op.isoformat(),'valid':True,'scope':symbols,'invalid':{},'capacity_per_symbol':8,'rows':rows}
    if minute%30==0:
     if minute:disconnects+=1;s.append_epoch({'kind':'B_STREAM_WITNESS','case_schema':2,'reason':'CONNECTION_ENDED_UNTRUSTED','proof':proof,'pending_R':sorted(expected_R)})
     s.append_epoch({'kind':'B_EVIDENCE_READY','proof':proof,'report':{'good':{},'quarantine':{},'cases':{'inventory':{'O':[],'A':[],'U':sorted(expected_U),'R':sorted(expected_R)}}}})
    batch=[symbols[(minute*100+j)%500] for j in range(100)]
    evidence={x:{'proven':True,'reason':'BAR_OVERLAP_OBSERVED','witness_count':8,'first':op.isoformat(),'last':(op+timedelta(minutes=minute)).isoformat(),'generation':generation,'TRADE_completeness':False} for x in batch}
    s.append_epoch({'kind':'B_INCREMENTAL_ADMISSION','connection':gen,'revision':minute+1,'accepted_symbols':batch,'evidence':evidence,'proof':proof,'transport_trusted':False})
    if minute%12==0:
     expected_U.add('S00000');expected_R.add('S00001');s.append_epoch({'kind':'B_NEEDS_INTENT','case_schema':2,'add_U':['S00000'],'add_R':['S00001']})
    if minute%12==5:
     expected_U.discard('S00000');expected_R.discard('S00001');s.append_epoch({'kind':'B_CASE_RESOLUTION','case_schema':2,'resolved_U':['S00000'],'resolved_R':['S00001'],'proof':proof})
   self.assertEqual(set(s.archive['U']),expected_U);self.assertEqual(set(s.archive['R']),expected_R)
   self.assertLessEqual(s.stats['largest_record_bytes'],RECORD_BYTES);self.assertLessEqual(s.head['active_allocated_bytes'],SEGMENT_BYTES)
   before=len(r.fields_read);restarted=ChainState(r,s.session,s.root_hash,'w',1,emit=lambda *a,**kw:None);restarted.restore();boot_reads=len(r.fields_read)-before
   self.assertEqual(restarted.archive,s.archive);self.assertTrue(all(k==s.key(s.head['segment']) for k,f in r.fields_read[before:]))
   audit_start=time.monotonic();audit=full_verify(restarted);audit_seconds=time.monotonic()-audit_start
   physical=sum(p.stat().st_size for h in r.hashes.values() for p in h.values())+sum(len(v.encode()) for k,v in r.values.items() if k.startswith(s.base+':'))
   self.assertEqual(physical,s.metrics()['session_payload_bytes']);self.assertEqual(audit['records'],s.seq)
   self.assertTrue(any(x['stage']=='EVIDENCE_SESSION_SIZE_WARNING' for x in logs))
   result={'test':'R9_CORE_FULL_DAY_SNAPSHOT_JOURNAL','symbols':500,'minutes':390,'incremental_admissions':390,'admissions_per_hour':60,'disconnects':disconnects,'records':s.seq,'segments':s.head['segment'],'session_payload_bytes':physical,'session_payload_MiB':physical/1048576,'record_values_bytes':s.head['record_bytes'],'largest_record_bytes':s.stats['largest_record_bytes'],'active_segment_allocated_bytes':s.head['active_allocated_bytes'],'boot_active_HGET_count':boot_reads,'full_audit_seconds':audit_seconds,'total_test_seconds':time.monotonic()-start,'by_kind':s.stats['by_kind'],'size_target':'NONE; prints actual full-snapshot bytes','scope':'Disk contract fake, real Python serialization/hash/replay. Small tests execute Lua via liblua. No Upstash, live API, full engine, 512Mi/0.5CPU claim.'}
   Path('DAY_FULL_SNAPSHOT_RESULT_R9_CORE.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
