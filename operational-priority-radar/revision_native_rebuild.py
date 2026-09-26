"""Read-only native REST rebuild after a retained SIP revision.

Provider-native bars are replayed, never patched by subtracting trade volume.
This creates a candidate, not evidence that REST has incorporated a revision.
"""
from copy import deepcopy
from recovery_chronology import _utc
from native_session_recovery_batches import recover_native_session_batch
from sip_trade_revision_audit import audit_trade_revisions
from sip_semantic_digest import canonical_sha256


class RevisionRebuildUnsafe(RuntimeError):
    pass


def rebuild_revision_candidate(diagnostic, rest, leadership, *, session,
                               session_start, session_end, as_of,
                               require_not_cancelled=lambda: None):
    require_not_cancelled()
    value=deepcopy(diagnostic)
    if not isinstance(value, dict) or value.get('schema') != 'OPR_SIP_REVISION_DIAGNOSTIC_V1':
        raise RevisionRebuildUnsafe('REVISION_DIAGNOSTIC_INVALID')
    expected=value.pop('diagnostic_sha256',None)
    if not expected or canonical_sha256(value)!=expected:
        raise RevisionRebuildUnsafe('REVISION_DIAGNOSTIC_DIGEST_INVALID')
    evidence=value.get('original_trade_evidence', {})
    if not isinstance(evidence, dict) or evidence.get('matched_within_retained_window') is not True:
        raise RevisionRebuildUnsafe('REVISION_ORIGINAL_UNPROVEN')
    original=evidence.get('original_frame'); revision=value.get('frame')
    if not isinstance(original, dict) or not isinstance(revision, dict):
        raise RevisionRebuildUnsafe('REVISION_PAIR_MISSING')
    if revision.get('T') not in ('c','x') or value.get('invalid_or_oversized_fields'):
        raise RevisionRebuildUnsafe('REVISION_FRAME_INVALID')
    pair=audit_trade_revisions([original,revision],symbol=original.get('S'),max_frames=2,max_ids=2)
    if pair['observed_frame_sha256'] != evidence.get('pair_sha256'):
        raise RevisionRebuildUnsafe('REVISION_PAIR_DIGEST_MISMATCH')
    start,end,cutoff=map(_utc,(session_start,session_end,as_of))
    received=_utc(value.get('received_at_utc'))
    if received > cutoff:
        raise RevisionRebuildUnsafe('REVISION_RECEIVED_AFTER_REBUILD_CUTOFF')
    # Limit this API to same-session original trades; revision time is not
    # interpreted as original execution time or an affected-bar timestamp.
    if not start <= _utc(original['t']) < end <= cutoff or _utc(revision['t']) > cutoff:
        raise RevisionRebuildUnsafe('REVISION_WINDOW_INVALID')
    def identity():
        token=leadership.require_current()
        worker=getattr(token,'worker_instance_id',None)
        generation=getattr(token,'leader_generation',None)
        if not isinstance(worker,str) or not worker or type(generation) is not int or generation<1:
            raise RevisionRebuildUnsafe('REVISION_LEADERSHIP_INVALID')
        return worker,generation
    before=identity()
    # Fetch full bounded session plus native5 warmup: changing an earlier bar
    # can affect later frozen indicators, not just the original minute.
    signals,native=recover_native_session_batch(rest,[original['S']],session=session,
        session_start=start,session_end=end,as_of=cutoff,batch_index=0)
    require_not_cancelled()
    if identity()!=before:
        raise RevisionRebuildUnsafe('REVISION_LEADERSHIP_CHANGED')
    audit={'schema':'OPR_REVISION_NATIVE_REBUILD_CANDIDATE_V1',
           'symbol':original['S'],'session':session,
           'worker_instance_id':before[0],'leader_generation':before[1],
           'revision_pair_sha256':pair['observed_frame_sha256'],
           'revision_diagnostic_sha256':expected,
           'revision_received_at_utc':received.isoformat(),
           'rebuild_cutoff_utc':cutoff.isoformat(),
           'native_batch_evidence_sha256':native['batch_evidence_sha256'],
           'native_session_replayed':True,'signal_count':len(signals),
           'rest_revision_applied_proven':False,'sip_semantics_reconciled':False,
           'full_session_coverage_proven':False,'continuity_proven':False,
           'redis_writes':0,'capture_ack_authorized':False,
           'eb_persistence_authorized':False,'retroactive_entries_allowed':False,
           'direct_handoff_authorized':False,'shadow_deploy_authorized':False}
    audit['candidate_sha256']=canonical_sha256(audit)
    return {'signals':signals,'native_evidence':native,'audit':audit}
