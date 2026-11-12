"""Offline review examples; no Redis/API/environment access."""
import sys,json,socket,contextlib,io,copy,hashlib
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path[:0]=[str(root),str(root/'reference_app'),str(root/'tests')];sys.dont_write_bytecode=True
socket.socket.connect=lambda *a:(_ for _ in ()).throw(AssertionError('NETWORK_FORBIDDEN'))
from unittest.mock import patch
from ipr import review_flags
from ipr.outbox_mget import outbox_store
from dintegration.native_timing import NativeTiming
from r108_support import native_fixture,AT,POLICY,box,packed
matrix=[];last=None
for timing in (False,True):
 for grouped in (False,True):
  with patch.object(review_flags,'NATIVE_TIMING_ENABLED',timing),patch.object(review_flags,'OUTBOX_MGET_ENABLED',grouped):
   syms,http,api,rest,p,store,bridge=native_fixture();lines=[];m=None
   if timing:m=NativeTiming(p,rest,POLICY,wall=lambda:AT,emit=lines.append);m.install(bridge)
   with api.operation('native'),contextlib.redirect_stdout(io.StringIO()):bridge.native_cycle(syms,AT,True)
   if m:m.q.join();last=m.snapshot();m.close()
   b=box(27);out=outbox_store(b).pending()
   matrix.append({'NATIVE_TIMING_ENABLED':timing,'OUTBOX_MGET_ENABLED':grouped,'HTTP_count':len(http.calls),'HTTP_peak':http.peak,'outbox_GET':sum(c[0]=='GET' for c in b.calls),'outbox_MGET':sum(c[0]=='MGET' for c in b.calls),'outbox_SCAN':sum(c[0]=='SCAN' for c in b.calls),'decision_sha256':hashlib.sha256(packed(store)).hexdigest(),'pending_output_sha256':hashlib.sha256(packed(out)).hexdigest(),'observer_lines':len(lines)})
assert len({v['decision_sha256'] for v in matrix})==len({v['pending_output_sha256'] for v in matrix})==1
(root/'FLAGS_MATRIX_R108.json').write_text(json.dumps(matrix,indent=2)+'\n')
(root/'MEASUREMENTS_EXAMPLE_R108.json').write_text(json.dumps({'scope':'synthetic offline fixture, not production timing','native_fast_timing':last},indent=2)+'\n')
def size(x,seen=None):
 seen=seen if seen is not None else set()
 if id(x) in seen:return 0
 seen.add(id(x));n=sys.getsizeof(x)
 if isinstance(x,dict):n+=sum(size(k,seen)+size(v,seen) for k,v in x.items())
 elif isinstance(x,(list,tuple,set,frozenset)):n+=sum(size(v,seen) for v in x)
 return n
syms,http,api,rest,p,store,bridge=native_fixture(1);m=NativeTiming(p,rest,POLICY,wall=lambda:AT)
row=copy.deepcopy(last['last']);row['request_samples']=[{'operation':'native','wait_seconds':.1,'hold_seconds':2.2} for _ in range(8)]
resident=size([json.loads(json.dumps(row)) for _ in range(32)]+[m.boundaries,last]);m.close()
(root/'MEMORY_ESTIMATE_R108.json').write_text(json.dumps({'bounded_data_example_bytes':resident,'MiB':resident/1048576,'queue_rows':32,'request_samples_per_cycle':8,'max_boundaries':96,'actual_fixture_boundaries':78,'scope':'recursive sys.getsizeof for buffered structured telemetry; shared pipeline/HTTP rows excluded; allocator, thread stack, live RSS/cgroup NOT measured','ASSUMPTION_expected_extra_resident_data_MiB_range':[.2,1.0],'live_extra_memory':'UNKNOWN'},indent=2)+'\n')
from test_r9_core import ChainTests
from r9core.chain import ChainState
r,l,s=ChainTests().fixture();s.append_epoch({'kind':'NOTE','value':'unchanged'});reader=ChainState(r,s.session,s.root_hash,'readonly',0);reader.restore();segment=s.key(s.head['segment']);field=str(s.seq)
r.state['hashes'][segment][field]=r.state['hashes'][segment][field].replace('unchanged','tampered!')
try:reader.restore();fast='ACCEPTED_UNCHANGED_HEAD_FAST_PATH'
except Exception as e:fast=type(e).__name__
try:ChainState(r,s.session,s.root_hash,'readonly',0).restore();fresh='ACCEPTED'
except Exception as e:fresh=type(e).__name__
assert fast=='ACCEPTED_UNCHANGED_HEAD_FAST_PATH' and fresh!='ACCEPTED'
(root/'PARENT_REUSE_COUNTEREXAMPLE_R108.json').write_text(json.dumps({'scope':'local in-memory fixture/liblua; no Redis service or connection; unchanged R10.4 chain code','head_unchanged':True,'tail_payload_changed_without_head_change':True,'reused_reader':fast,'fresh_reader':fresh,'conclusion':'reusing readonly ChainState alone does not preserve fresh-reader active-tail checks; not implemented'},indent=2)+'\n')
print(json.dumps({'matrix':matrix,'observer_buffer_example_bytes':resident,'parent_reuse_counterexample':{'reused':fast,'fresh':fresh}}))
