"""Local diagnostic overhead only. No network, no market load, no CPU quota claim."""
import sys,time,threading,json,statistics
from datetime import datetime,timezone
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path[:0]=[str(root),str(root/'reference_app')];sys.dont_write_bytecode=True
from types import SimpleNamespace as NS
from ipr.wave import HeavyWindow,WaveDiagnostics
from r4a.ingress_observed import ObservedIngress
from ws_ingress import BoundedWSIngress
from dintegration.measure import Measurements
from r4a.decision_metrics import MeasuredDecisionLock
from contextlib import nullcontext

def trial(observed,n=5000):
 at=datetime.now(timezone.utc);measure=Measurements();lock=threading.RLock()
 if observed:lock=MeasuredDecisionLock(lock)
 p=NS(_decision_lock=lock,_entry_opportunities={},_active_by_symbol={},contention_probe=None,on_bar=lambda *a:None)
 rec=NS(on_stream_message=lambda *a:None)
 a=NS(c=NS(bar={'X'}),recovering=set(),bridge=NS(p=p))
 d=WaveDiagnostics(a,measure,HeavyWindow(lambda:at))
 ingress=ObservedIngress(p,rec,lambda:True,diagnostics=d) if observed else BoundedWSIngress(p,rec,lambda:True)
 ingress.accepting=True
 start=time.perf_counter();cpu=time.process_time()
 for i in range(n):
  item=('BAR',{'S':'X'},at,time.monotonic())
  if observed:d.received('BAR',at)
  ingress._work(item)
 elapsed=time.perf_counter()-start;cpu=time.process_time()-cpu
 return {'bars':n,'elapsed_seconds':elapsed,'cpu_seconds':cpu,'elapsed_us_per_BAR':elapsed/n*1e6}

if __name__=='__main__':
 pairs=[]
 for i in range(7):
  baseline=trial(False);candidate=trial(True);pairs.append({'baseline':baseline,'R6':candidate,'added_us_per_BAR':candidate['elapsed_us_per_BAR']-baseline['elapsed_us_per_BAR']})
 print(json.dumps({'scope':'fake no-op decision path, local machine, no 0.5CPU constraint; NOT production load or stability proof','trials':pairs,'median_added_us_per_BAR':statistics.median(x['added_us_per_BAR'] for x in pairs)},indent=2))
