"""Bounded passive estimates. No trust, Redis, network, or decision changes."""
import itertools,sys,time,os
from datetime import datetime,timezone

def capture_sample(p,limit=200):
    """Caller holds decision lock for bounded SHALLOW copies only. No seen set."""
    limit=min(200,max(0,int(limit)))
    histories=getattr(p.br,'history',{})
    packed=[]
    for symbol,h in itertools.islice(histories.items(),limit):
        # PackedHistory payloads are immutable bytes: never unpack for telemetry.
        rows=getattr(h,'rows',())
        packed.append((symbol,sys.getsizeof(h)+sys.getsizeof(vars(h))+sys.getsizeof(rows),tuple(rows)))
    structure=[(s,sys.getsizeof(v),tuple(v))for s,v in itertools.islice(p.bars.items(),limit)]
    trades=[(s,sys.getsizeof(v),tuple(v))for s,v in itertools.islice(p.trades.items(),limit)]
    return {'sampled_utc':datetime.now(timezone.utc).isoformat(),'capture_monotonic':time.monotonic(),
            'limit':limit,'stores':{'base_ready_packed_history':(len(histories),sys.getsizeof(histories),tuple(packed)),
                                  'structure_bars':(len(p.bars),sys.getsizeof(p.bars),tuple(structure)),
                                  'entry_trades':(len(p.trades),sys.getsizeof(p.trades),tuple(trades))}}

def estimate_sample(snapshot):
    """Occurrence sizes: may count shared leaves more than once. NOT deep heap bytes.
    Three samples x<=200symbols x(60B/20structure/1trade normally). No identity set,
    gc.get_objects(), materialized full key list, or unpickling. Prefix sample is
    deterministic/possibly biased; extrapolation is explicitly NOT a safety limit.
    """
    started=time.monotonic()
    def occurrence_size(value,depth=0):
        n=sys.getsizeof(value)
        if depth>=8:return n
        if isinstance(value,(tuple,list)):return n+sum(occurrence_size(x,depth+1)for x in value)
        if isinstance(value,dict):return n+sum(occurrence_size(k,depth+1)+occurrence_size(v,depth+1)for k,v in value.items())
        if type(value).__name__=='SIPTrade':return n+occurrence_size(vars(value),depth+1)
        return n
    result={}
    for name,(population,header,rows)in snapshot['stores'].items():
        sample_bytes=sum(sys.getsizeof(symbol)+container_bytes+sum(occurrence_size(v)for v in values)for symbol,container_bytes,values in rows)
        result[name]={'population_symbols':population,'sampled_symbols':len(rows),'sample_occurrence_bytes':sample_bytes,
                      'estimated_total_occurrence_bytes':header+(sample_bytes*population/len(rows)if rows else 0),
                      'mapping_shallow_bytes':header}
    return {'status':'SAMPLED_ESTIMATE','sampled_utc':snapshot['sampled_utc'],'sample_interval_min_seconds':900,
            'sample_limit_per_store':snapshot['limit'],'stores':result,'estimate_seconds':time.monotonic()-started,
            'scope':'Deterministic first<=200symbols/store. Logical occurrence sizes with shared-leaf duplication, no global identity dedup; extrapolation may be biased. Not RSS/PSS, exact heap, or a safety decision.'}

def read_worker_resources():
    # Optional passive worker-only evidence; no resource limits or decisions here.
    rss=None
    try:
        with open('/proc/self/statm','r',encoding='ascii')as f:rss=int(f.read(128).split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,IndexError):pass
    return rss,time.process_time()
