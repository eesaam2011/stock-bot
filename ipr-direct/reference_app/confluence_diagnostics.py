"""Boundary-only, thread-safe counters. No exception messages or symbol logging."""
import json,threading,time
from redis_lua_production import LeaseLost
from leadership import LeadershipError

def raise_if_leadership_lost(exc):
    if isinstance(exc,(LeaseLost,LeadershipError)):raise exc
    # Existing production facade uses these exact RuntimeError contracts.
    if type(exc) is RuntimeError and str(exc) in {'NOT_CURRENT_LEADER','STALE_LEADER_GENERATION'}:
        raise LeaseLost() from None

class ConfluenceDiagnostics:
    def __init__(self,clock=time.monotonic,emit=None):
        self.clock=clock;self.emit=emit or (lambda row:print(json.dumps(row),flush=True))
        self.lock=threading.Lock();self.types={};self.total=0;self.sources={};self.log_failures=0
    def record(self,error_type,source):
        # Type names only; bounded cardinality, even for adversarial dynamic types.
        if not isinstance(error_type,str) or not error_type.isidentifier():error_type='OtherError'
        with self.lock:
            if error_type not in self.types and len(self.types)>=64:error_type='OtherError'
            row=self.types.setdefault(error_type,{'count':0,'last_report':self.clock(),'last_count':0})
            row['count']+=1;self.total+=1;self.sources[source]=self.sources.get(source,0)+1
            if row['count']<=5 or self.clock()-row['last_report']>=60:
                try:
                    self.emit({'stage':'CONFLUENCE_ERROR','error_type':error_type,'count':row['count'],
                               'since_last_report':row['count']-row['last_count'],'source':source})
                except Exception:self.log_failures+=1
                row['last_report']=self.clock();row['last_count']=row['count']
    def snapshot(self):
        with self.lock:
            return {'total':self.total,'by_type':{k:v['count'] for k,v in self.types.items()},'by_source':dict(self.sources),'log_failures':self.log_failures}
