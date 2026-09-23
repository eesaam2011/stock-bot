"""Step3N reproducible synthetic BASE_READY minute-boundary burst benchmark.

Offline CPU/RSS proxy only. No live SIP, 407 prediction, or CI timing threshold.
Run: python benchmark_base_ready_burst.py --symbols 5000 --full-evals 300
"""
import argparse,gc,json,statistics,time,tracemalloc
from collections import deque
from datetime import datetime,timedelta,timezone
from production_adapters import OperationalBaseReady
from base_ready import phase2_features

START=datetime(2026,9,22,14,30,tzinfo=timezone.utc)
def bar(i):
    return {"t":(START+timedelta(minutes=i)).isoformat(),
            "o":10+i*.01,"h":10.3+i*.01,"l":9.9+i*.01,
            "c":10.1+i*.01,"v":1000+i*50,"vw":10.05+i*.01,"n":5}
def timed(fn,n):
    gc.collect()
    t=time.perf_counter_ns()
    for _ in range(n):fn()
    return (time.perf_counter_ns()-t)/n/1000
def resident_bytes(builder):
    gc.collect();tracemalloc.start()
    rows=builder()
    _,peak=tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak,len(rows)
def run(symbols=5000,full_evals=300):
    if not 1<=symbols<=50000 or not 1<=full_evals<=5000:
        raise ValueError("bounded synthetic benchmark")
    rows=[bar(i) for i in range(60)]
    compact=deque((OperationalBaseReady._compact_bar(x) for x in rows),maxlen=60)
    end=START+timedelta(minutes=60)
    materialize=lambda:OperationalBaseReady._materialize_history(compact)
    direct=lambda:[dict(x) for x in rows]
    # Reuse identical rows to isolate the conversion from feed variability.
    raw_direct_us=timed(direct,symbols)
    compact_materialize_us=timed(materialize,symbols)
    # Full feature evaluation matters more than the isolated conversion ratio.
    direct_full_us=timed(lambda:phase2_features(direct(),end),full_evals)
    compact_full_us=timed(lambda:phase2_features(materialize(),end),full_evals)
    short=deque(list(compact)[:23],maxlen=60)
    old_short_us=timed(lambda:phase2_features(
        OperationalBaseReady._materialize_history(short),end),symbols)
    new_short_us=timed(lambda:None if len(short)<24 else phase2_features(
        OperationalBaseReady._materialize_history(short),end),symbols)
    # Resident-memory comparison on a bounded 500-symbol subset.
    n=min(symbols,500)
    tuple_peak,_=resident_bytes(lambda:[
        deque((OperationalBaseReady._compact_bar(x) for x in rows),maxlen=60)
        for _ in range(n)])
    dict_peak,_=resident_bytes(lambda:[
        deque((dict(x) for x in rows),maxlen=60) for _ in range(n)])
    report={
      "type":"SYNTHETIC_OFFLINE_BURST_NOT_LIVE_407_PROOF",
      "symbols_at_same_minute_boundary":symbols,
      "mature_full_evals_sample":full_evals,
      "bars_per_mature_symbol":60,
      "prequalification_bars":23,
      "conversion_only_microseconds_per_symbol":{
          "direct_dict_copy":round(raw_direct_us,3),
          "compact_tuple_materialization":round(compact_materialize_us,3),
          "compact_vs_direct_ratio":round(compact_materialize_us/max(raw_direct_us,.001),2)},
      "full_frozen_features_microseconds_per_symbol":{
          "direct_dict_copy_then_features":round(direct_full_us,3),
          "compact_materialize_then_features":round(compact_full_us,3)},
      "transient_23_bars_microseconds_per_symbol":{
          "old_convert_then_frozen_none":round(old_short_us,3),
          "new_preflight_skip":round(new_short_us,3),
          "saved_estimated_burst_milliseconds":round((old_short_us-new_short_us)*symbols/1000,3)},
      "peak_tracemalloc_bytes_resident_subset":{
          "symbols":n,"compact_tuple":tuple_peak,"persistent_dict":dict_peak},
      "memory_policy":"KEEP_COMPACT_TUPLES_NO_PERSISTENT_DICT_CACHE",
      "queue_limit_in_production":1024,
      "upstream_arrival_rate_measured":False,
      "real_alpaca_sip_407_reproduced":False,
      "full_session_coverage_proven":False,
      "shadow_deploy_authorized":False}
    return report
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--symbols",type=int,default=5000)
    p.add_argument("--full-evals",type=int,default=300)
    a=p.parse_args()
    print(json.dumps(run(a.symbols,a.full_evals),ensure_ascii=False,
                     indent=2,sort_keys=True))
