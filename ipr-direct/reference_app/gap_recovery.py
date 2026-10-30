from dataclasses import dataclass
@dataclass(frozen=True)
class GapOutcome:
 status:str
 new_entry_allowed:bool
def dedup_chronological(rows):
 seen=set();out=[]
 for r in sorted(rows,key=lambda x:(x["bar_start_ts"],x.get("sequence",0))):
  k=(r["symbol"],r["timeframe"],r["bar_start_ts"])
  if k in seen:continue
  seen.add(k);out.append(r)
 return out
def reconcile_opportunity(e_time,b_time,gap_start,gap_end):
 # If both causal decisions completed entirely while stream was untrusted, no retroactive Entry.
 if e_time is not None and b_time is not None and gap_start<=e_time<=gap_end and gap_start<=b_time<=gap_end:
  return GapOutcome("OPPORTUNITY_MISSED_DURING_DATA_GAP",False)
 return GapOutcome("RECONCILED",False)
def active_trade_recovery_order(items):
 # Active trade risk/halt reconciliation always precedes ordinary opportunities.
 return sorted(items,key=lambda x:0 if x.get("active_trade") else 1)
def recovery_path_from_ohlc(stop_touched,target_touched,chronology_proven):
 if stop_touched and target_touched and not chronology_proven:return "RECOVERY_PATH_AMBIGUOUS"
 return "CHRONOLOGY_PROVEN" if chronology_proven else "NO_CONFLICT"
