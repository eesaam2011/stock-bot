from dataclasses import dataclass,field
@dataclass
class ShadowMetrics:
 rss_pct:float=0.0;cpu_pct:float=0.0;queue_depth:int=0;oldest_event_age_s:float=0.0
 processing_latency_ms:float=0.0;redis_latency_ms:float=0.0;native5m_cycle_ms:float=0.0
 reconnects:int=0;duplicate_canonical_entries:int=0;duplicate_leaders:int=0
 writer_reader_mismatches:int=0;lost_active_state:int=0;critical_recovery_bugs:int=0
 timeframe_substitutions:int=0;atomicity_failures:int=0;halt_inconsistencies:int=0
 samples:list=field(default_factory=list)
 def record(self,**x):
  for k,v in x.items():
   if hasattr(self,k):setattr(self,k,v)
  self.samples.append(dict(x))
 def live_gate_blockers(self):
  names=["duplicate_canonical_entries","duplicate_leaders","writer_reader_mismatches","lost_active_state",
         "critical_recovery_bugs","timeframe_substitutions","atomicity_failures","halt_inconsistencies"]
  return [n for n in names if getattr(self,n)>0]
