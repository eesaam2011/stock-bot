"""Explicit full payload audit, never invoked by production startup."""
import copy
from .chain import checked,empty,reduce_event,StateConflict

def full_verify(state):
 state.restore();head=state.head
 if head is None:return {'records':0,'record_bytes':0,'sealed_payloads_checked':True}
 previous=None;archive=empty();sequence=0;total=0
 for segment in range(1,head['segment']+1):
  info=head if segment==head['segment'] else checked(state._get(state.seal_key(segment)))
  first=info['first_sequence'];last=info['sequence'] if segment==head['segment'] else info['last_sequence'];part=0
  for n in range(first,last+1):
   raw=state.r.hget(state.key(segment),str(n))
   if raw is None:raise StateConflict('CHAIN2_AUDIT_RECORD_MISSING')
   x=checked(raw)
   if x['sequence']!=sequence+1 or x['previous_sha256']!=previous or x['session']!=state.session or x['root_hash']!=state.root_hash:raise StateConflict('CHAIN2_AUDIT_LINK_CONFLICT')
   event=x['epoch']
   if n==first:
    if event.get('kind')!='CHAIN2_SEGMENT_START' or event['state_before']!=archive or event['previous_segment_sha256']!=previous:raise StateConflict('CHAIN2_AUDIT_ANCHOR_CONFLICT')
   else:archive=reduce_event(archive,event)
   previous=x['sha256'];sequence=n;part+=len(raw.encode())
  expected=info['active_record_bytes'] if segment==head['segment'] else info['record_bytes']
  if part!=expected or previous!=info['last_sha256']:raise StateConflict('CHAIN2_AUDIT_SEAL_BYTES_CONFLICT')
  total+=part
 if total!=head['record_bytes'] or sequence!=head['sequence'] or archive!=state.archive:raise StateConflict('CHAIN2_AUDIT_FINAL_CONFLICT')
 return {'records':sequence,'record_bytes':total,'sealed_payloads_checked':True,'U':archive['U'],'R':archive['R']}
