from .core import Blocked

def phase2_intersection(events,intervals):
 """Approximate event-time membership, not recall or ENTRY reference.
 Intervals must be explicit [start,end), complete logging, with selected+confirmed sets.
 """
 ids=[e['candidate_id'] for e in events]
 if len(ids)!=len(set(ids)):raise Blocked('DUPLICATE_PHASE2_ID')
 xs=sorted(intervals,key=lambda i:i['start'])
 for i in xs:
  if i['end']<=i['start']:raise Blocked('INVALID_INTERVAL')
 for a,b in zip(xs,xs[1:]):
  if a['end']>b['start']:raise Blocked('OVERLAPPING_INTERVALS')
 result=[]
 for e in events:
  ts=e['candidate_ts'];i=next((x for x in xs if x['start']<=ts<x['end']),None)
  complete=i is not None and i.get('log_complete') is True and i.get('session')==e['session']
  result.append({'candidate_id':e['candidate_id'],'symbol':e['symbol'],'candidate_ts':ts,'selected_member':e['symbol'] in i['selected'] if complete else None,'confirmed_bar_member':e['symbol'] in i['confirmed_bar'] if complete else None,'kind':'PHASE2_PRESELECTED_PROXY_INTERSECTION_NOT_RECALL','entry_reference':False,'quality_inference':False})
 return result
