"""R10.10 networkless contracts; liblua executes unchanged atomic scripts.

Real 10-second stalls are wall-clock tests, not simulated time. Their guarantee
is lock ownership/ingress progress in this fixture, never a production deadline.
"""
import asyncio
import contextlib
import copy
import inspect
import hashlib
import io
import json
import threading
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from ipr import review_flags
from r4b.recovery_io import install, RecoveryReadFailed, RecoveryRun
from r4a.runtime import PipelinePort, RecoveryPort
from ws_ingress import BoundedWSIngress
from state_store import base_record, canonical_json, key_trade, key_opportunity, key_outbox
from test_direct import OP
from test_recovery_regression import CaseFixture
from test_r3 import setup, receive


def ready():
    f = setup(('X', 'Y'))
    f.a.proof.generation = 'r1010-fixed-fixture'
    f.now[0] = OP + timedelta(seconds=61)
    receive(f, 'X', 0)
    install(f.a)
    return f


def add_trade(f, state='ACTIVE_PRE_T1', session='2026-10-28'):
    t = CaseFixture().trade(session)
    t['state'] = state
    f.a.bridge.r.set(key_trade('t'), canonical_json(t))
    return t


def add_opp(f, session='2026-10-28', uncertain=False):
    o = base_record('opportunity', session, 'X', 'CONFLUENCE_VALID', OP.isoformat())
    o.update(e_id='e', b_id='b', delta_seconds=0, first_event='E', entry_trigger_ts=OP.isoformat(),
             expiry=(OP + timedelta(minutes=15)).isoformat(), terminal_reason=None,
             leader_generation=1, worker_instance_id='w')
    f.a.bridge.r.set(key_opportunity(session, 'X'), canonical_json(o))
    f.a.bridge.p._entry_opportunities['X'] = copy.deepcopy(o)
    if uncertain:
        f.a.bridge.p._entry_uncertain.add('X')
    return o


def perform(f):
    return f.a.recover(f.now[0])


def state_bytes(f):
    r = f.a.bridge.r.r
    pins = {k: sorted(v) for k, v in f.a.barrier.inventory().items()}
    return canonical_json(r.state), canonical_json(pins), canonical_json(f.epochs)


class RecoveryIsolation(unittest.TestCase):
    def setUp(self):
        self.flag = patch.object(review_flags, 'RECOVERY_IO_OUTSIDE_LOCK_ENABLED', True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def test_real_10s_SCAN_MGET_EVAL_stalls_keep_actual_ingress_running(self):
        async def run():
            fixtures = []
            for name in ('scan', 'mget', 'eval'):
                f = ready()
                add_trade(f)
                if name == 'eval':
                    f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
                entered, release = threading.Event(), threading.Event()
                original = getattr(f.a.bridge.r, name)
                row = {'f': f, 'entered': entered, 'release': release, 'errors': [], 'start': None}
                def stall(*args, _r=row, _old=original, **kwargs):
                    if not _r['entered'].is_set():
                        self.assertFalse(_r['f'].a.bridge.p._decision_lock._is_owned())
                        self.assertFalse(_r['f'].a.control_lock._is_owned())
                        _r['start'] = time.monotonic()
                        _r['entered'].set()
                        if not _r['release'].wait(20):
                            raise AssertionError('fixture release timed out')
                        _r['stall_seconds'] = time.monotonic() - _r['start']
                    return _old(*args, **kwargs)
                setattr(f.a.bridge.r, name, stall)
                def recover(_r=row):
                    try:
                        _r['result'] = perform(_r['f'])
                    except BaseException as exc:
                        _r['errors'].append(exc)
                row['thread'] = threading.Thread(target=recover)
                row['thread'].start()
                port = BoundedWSIngress(PipelinePort(f.a), RecoveryPort(f.a), lambda: f.a.transport_trusted,
                                        capacity=8192, max_age_seconds=5.)
                row['port'] = port
                await port.begin()
                fixtures.append(row)
            try:
                limit = time.monotonic() + 5
                while not all(r['entered'].is_set() for r in fixtures):
                    self.assertLess(time.monotonic(), limit)
                    await asyncio.sleep(.01)
                start = time.monotonic()
                while time.monotonic() - start < 10.05:
                    for row in fixtures:
                        row['port'].submit('BAR', {'S': 'X', 'T': 'b'}, row['f'].now[0])
                    await asyncio.sleep(.05)
                for row in fixtures:
                    self.assertGreater(row['port'].processed['BAR'], 100)
                    self.assertEqual(row['port'].failure_count, 0)
                    self.assertLess(row['port'].max_delay, 5.)
            finally:
                for row in fixtures:
                    row['release'].set()
                for row in fixtures:
                    await asyncio.to_thread(row['thread'].join, 10)
                    await row['port'].end()
            for row in fixtures:
                self.assertFalse(row['thread'].is_alive())
                self.assertEqual(row['errors'], [])
                self.assertGreaterEqual(row['stall_seconds'], 10.)
                self.assertTrue(row['result'], row['f'].a.report)
            print(json.dumps({'test': 'R1010_REAL_STALLS', 'results': [
                {'io': n, 'stall_seconds': r['stall_seconds'], 'processed_BAR': r['port'].processed['BAR'],
                 'max_queue_delay_seconds': r['port'].max_delay, 'fail_closed': r['port'].failure_count}
                for n, r in zip(('SCAN', 'MGET', 'EVAL'), fixtures)]}))
        asyncio.run(run())

    def test_all_recovery_I_O_and_leadership_reads_outside_both_live_locks(self):
        f = ready()
        add_trade(f)
        f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
        seen = []
        for target, name in ((f.a.bridge.r, 'scan'), (f.a.bridge.r, 'mget'), (f.a.bridge.r, 'get'),
                             (f.a.bridge.r, 'eval'), (f.a.bridge.leadership, 'require_current'),
                             (f.a.checkpoint, 'append_epoch')):
            original = getattr(target, name)
            def checked(*args, _old=original, _name=name, **kw):
                self.assertFalse(f.a.bridge.p._decision_lock._is_owned(), _name)
                self.assertFalse(f.a.control_lock._is_owned(), _name)
                seen.append(_name)
                return _old(*args, **kw)
            setattr(target, name, checked)
        self.assertTrue(perform(f), f.a.report)
        self.assertTrue({'scan', 'mget', 'get', 'eval', 'require_current', 'append_epoch'} <= set(seen))

    def changed(self, fn):
        f = ready()
        original = f.src.warmup
        hot = copy.deepcopy(dict(f.a.bridge.p._entry_opportunities))
        proofs = copy.deepcopy(f.a.c.proofs)
        def change(*args):
            result = original(*args)
            fn(f)
            return result
        f.src.warmup = change
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)
        self.assertEqual(f.a.bridge.p._entry_opportunities, hot)
        self.assertEqual(f.a.c.proofs, proofs)
        self.assertFalse(any(x.get('kind') == 'B_EVIDENCE_READY' for x in f.epochs))
        return f

    def test_connection_changes_cancel_before_canonical_publication(self):
        f = self.changed(lambda f: setattr(f.a.c, 'connection', f.a.c.connection + 1))
        self.assertIn('RECOVERY_CONNECTION_CHANGED', f.a.measure.recovery_io.snapshot()['cancellations'])

    def test_revision_changes_cancel(self):
        self.changed(lambda f: setattr(f.a.c, 'revision', f.a.c.revision + 1))

    def test_proof_generation_changes_cancel(self):
        self.changed(lambda f: setattr(f.a.proof, 'generation', 'new-generation'))

    def test_leadership_loss_cancels(self):
        f = self.changed(lambda f: f.a.bridge.r.r.set(f.a.bridge.r.prefix + f.a.bridge.p.lua.leader_key, 'other'))
        self.assertFalse(f.a.transport_trusted)

    def test_leadership_generation_changed_cancels(self):
        f = self.changed(lambda f: f.a.bridge.r.r.set(f.a.bridge.r.prefix + f.a.bridge.p.lua.generation_key, '2'))
        self.assertEqual(f.a.measure.recovery_io.snapshot()['cancellations']['RECOVERY_LEADERSHIP_CHANGED'], 1)

    def test_cancel_token_changes_cancel(self):
        self.changed(lambda f: f.a.cancel_event.set())

    def test_invalid_evidence_during_fetch_cancels(self):
        self.changed(lambda f: f.a.proof.invalidate('X', 'CONFLICTING_BAR'))

    def test_timer_during_warmup_keeps_exact_instant_and_original_expiry(self):
        f = ready()
        o = add_opp(f)
        due = f.now[0] + timedelta(seconds=10)
        original = f.src.warmup
        def warm(*args):
            f.a.bridge.p.expire_entry_opportunities(due)
            self.assertEqual(f.a.bridge.r.get(key_opportunity(o['session'], 'X')), canonical_json(o))
            return original(*args)
        f.src.warmup = warm
        self.assertTrue(perform(f), f.a.report)
        row = json.loads(f.a.bridge.r.get(key_opportunity(o['session'], 'X')))
        self.assertEqual((row['state'], row['updated_at']), ('ENTRY_EXPIRED', due.isoformat()))
        self.assertEqual(f.a.bridge.p._entry_opportunities, {})
        self.assertEqual(f.a._recovery_io.timers, [])

    def test_timer_after_evidence_persist_cancels_publish_not_deadline(self):
        f = ready()
        original = f.a.checkpoint.append_epoch
        def append(row):
            result = original(row)
            if row.get('kind') == 'B_EVIDENCE_READY':
                f.a.bridge.p.expire_entry_opportunities(f.now[0] + timedelta(seconds=1))
            return result
        f.a.checkpoint.append_epoch = append
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)
        self.assertEqual(f.a.report['reason'], 'RECOVERY_TIMER_CHANGED')

    def test_hot_state_changed_during_IO_not_overwritten(self):
        f = ready()
        original = f.src.warmup
        def warm(*args):
            value = original(*args)
            f.a.bridge.p.halted.add('Y')
            return value
        f.src.warmup = warm
        self.assertFalse(perform(f))
        self.assertEqual(f.a.bridge.p.halted, {'Y'})
        self.assertFalse(f.a.transport_trusted)

    def test_missing_scanned_key_is_failure_not_empty_inventory(self):
        f = ready()
        t = add_trade(f)
        original = f.a.bridge.r.mget
        def missing(keys):
            return [None] * len(keys) if key_trade(t['trade_id']) in keys else original(keys)
        f.a.bridge.r.mget = missing
        self.assertFalse(perform(f))
        self.assertIn('DISAPPEARED', f.a.report['reason'])
        self.assertIsNotNone(f.a.bridge.r.get(key_trade('t')))
        self.assertFalse(f.a.transport_trusted)

    def test_duplicate_SCAN_keys_are_deduplicated_without_evidence_loss(self):
        f = ready()
        add_trade(f)
        original = f.a.bridge.r.scan
        def duplicate(*args, **kw):
            cursor, keys = original(*args, **kw)
            return cursor, keys + keys
        f.a.bridge.r.scan = duplicate
        self.assertTrue(perform(f), f.a.report)
        self.assertEqual(list(f.a.bridge.p._active_by_symbol), ['X'])

    def test_corrupt_JSON_fails_closed(self):
        f = ready()
        f.a.bridge.r.set(key_trade('broken'), '{broken')
        self.assertFalse(perform(f))
        self.assertEqual(f.a.report['reason'], 'RECOVERY_CANONICAL_INVALID')

    def test_record_identity_mismatch_fails_closed(self):
        f = ready()
        t = add_trade(f)
        t['trade_id'] = 'different'
        f.a.bridge.r.set(key_trade('t'), canonical_json(t))
        self.assertFalse(perform(f))
        self.assertEqual(f.a.report['reason'], 'RECOVERY_CANONICAL_INVALID')

    def test_SCAN_failure_does_not_replace_existing_pins(self):
        f = ready()
        f.a.barrier.persisted_R.add('X')
        f.a.bridge.r.scan = lambda **kw: (_ for _ in ()).throw(TimeoutError('fixture-do-not-print-secret'))
        self.assertFalse(perform(f))
        self.assertEqual(f.a.barrier.persisted_R, {'X'})
        self.assertNotIn('fixture-do-not-print-secret', json.dumps(f.a.report))

    def test_MGET_failure_keeps_trust_down(self):
        f = ready()
        add_trade(f)
        f.a.bridge.r.mget = lambda keys: (_ for _ in ()).throw(ConnectionError('fixture'))
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)

    def test_short_MGET_response_fails_closed(self):
        f = ready()
        add_trade(f)
        f.a.bridge.r.mget = lambda keys: []
        self.assertFalse(perform(f))
        self.assertEqual(f.a.report['reason'], 'RECOVERY_MGET_LENGTH')

    def test_CAS_conflict_never_publishes_private_hot_state(self):
        f = ready()
        t = add_trade(f)
        f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
        original = f.a.bridge.r.eval
        def conflict(script, n, *args):
            if "local trade=KEYS[2]" in script:
                return -20
            return original(script, n, *args)
        f.a.bridge.r.eval = conflict
        self.assertFalse(perform(f))
        self.assertEqual(f.a.bridge.r.get(key_trade('t')), canonical_json(t))
        self.assertFalse(f.a.bridge.p._active_by_symbol)
        self.assertIsNone(f.a.bridge.r.get(key_outbox('STOP:t')))

    def test_unknown_commit_re_read_is_idempotent_no_duplicate_outbox(self):
        f = ready()
        add_trade(f)
        f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
        original = f.a.bridge.r.eval
        lost = []
        def unknown(script, n, *args):
            value = original(script, n, *args)
            if "local trade=KEYS[2]" in script and not lost:
                lost.append(True)
                raise TimeoutError('fixture-lost-reply')
            return value
        f.a.bridge.r.eval = unknown
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)
        self.assertEqual(json.loads(f.a.bridge.r.get(key_trade('t')))['state'], 'CLOSED_STOP')
        out = f.a.bridge.r.get(key_outbox('STOP:t'))
        f.a.bridge.r.eval = original
        self.assertTrue(perform(f), f.a.report)
        self.assertEqual(f.a.bridge.r.get(key_outbox('STOP:t')), out)

    def test_retry_after_read_failure_replays_deferred_timer(self):
        f = ready()
        add_opp(f)
        original = f.src.warmup
        def fail(*args):
            f.a.bridge.p.expire_entry_opportunities(f.now[0])
            raise TimeoutError('fixture')
        f.src.warmup = fail
        self.assertFalse(perform(f))
        self.assertTrue(f.a._recovery_io.timers)
        f.src.warmup = original
        self.assertTrue(perform(f), f.a.report)
        self.assertFalse(f.a.bridge.p._entry_opportunities)

    def test_observer_exception_cannot_change_application_success(self):
        f = ready()
        with patch.object(f.a.measure.recovery_io, 'finish', side_effect=RuntimeError('fixture')):
            self.assertTrue(perform(f), f.a.report)
        self.assertEqual(f.a.measure.recovery_io.observer_errors, 1)

    def test_metrics_counts_durations_and_sample(self):
        f = ready()
        self.assertTrue(perform(f))
        snap = f.a.measure.sample()['recovery_io']
        self.assertEqual((snap['recoveries'], snap['successes'], snap['failures'], snap['observer_errors']), (1, 1, 0, 0))
        self.assertGreater(snap['last_recoveries'][0]['seconds'], 0)
        self.assertIn('redis.scan', snap['io_phases'])
        self.assertGreaterEqual(snap['max_lock_hold_seconds'], 0)

    def test_flag_false_does_not_install_timer_or_metrics(self):
        with patch.object(review_flags, 'RECOVERY_IO_OUTSIDE_LOCK_ENABLED', False):
            f = setup(('X',))
            old = f.a.bridge.p.expire_entry_opportunities
            self.assertIsNone(install(f.a))
            self.assertEqual(f.a.bridge.p.expire_entry_opportunities, old)
            self.assertFalse(hasattr(f.a.measure, 'recovery_io'))

    def test_flag_false_existing_coordinator_delegates_original_timer(self):
        f = ready()
        o = add_opp(f)
        with patch.object(review_flags, 'RECOVERY_IO_OUTSIDE_LOCK_ENABLED', False):
            f.a.bridge.p.expire_entry_opportunities(f.now[0])
        self.assertEqual(json.loads(f.a.bridge.r.get(key_opportunity(o['session'], 'X')))['state'], 'ENTRY_EXPIRED')

    def test_false_branch_original_recover_body_is_literal_R109(self):
        from r4b.adapter import EvidenceAdapter
        source = inspect.getsource(EvidenceAdapter.recover)
        old = "  self.close_eligibility('RECOVERY_PENDING');start=time.monotonic()\n"
        body = old + source.split(old, 1)[1]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(),
                         '86ec86caf751f7e32149327fca184a7d56dd136fb70ab53cb0fe03a2b33d4848')

    def test_connection_change_during_SCAN_keeps_live_caches_unpublished(self):
        f = ready()
        add_trade(f)
        original = f.a.bridge.r.scan
        def scan(*args, **kw):
            result = original(*args, **kw)
            f.a.c.connection += 1
            return result
        f.a.bridge.r.scan = scan
        self.assertFalse(perform(f))
        self.assertFalse(f.a.bridge.p._active_by_symbol)
        self.assertFalse(f.a.transport_trusted)

    def test_generation_change_during_MGET_prevents_trade_commit(self):
        f = ready()
        t = add_trade(f)
        original = f.a.bridge.r.mget
        def mget(keys):
            result = original(keys)
            f.a.proof.generation = 'changed-during-mget'
            return result
        f.a.bridge.r.mget = mget
        self.assertFalse(perform(f))
        self.assertEqual(f.a.bridge.r.get(key_trade('t')), canonical_json(t))
        self.assertFalse(f.a.transport_trusted)

    def test_revoke_during_inflight_committed_CAS_never_grants_trust_or_hot_publish(self):
        # Remote atomic writes may complete before a locally observed revocation.
        # They cannot be rolled back. Only the hot-cache/trust publish is cancelled.
        f = ready()
        add_trade(f)
        f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
        original = f.a.bridge.r.eval
        def eval(script, n, *args):
            result = original(script, n, *args)
            if "local trade=KEYS[2]" in script:
                f.a.cancel_event.set()
            return result
        f.a.bridge.r.eval = eval
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)
        self.assertFalse(f.a.bridge.p._active_by_symbol)
        self.assertIsNotNone(f.a.bridge.r.get(key_outbox('STOP:t')))

    def test_ambiguous_opportunity_commit_retained_and_retry_resolves_canonical(self):
        f = ready()
        o = add_opp(f)
        original = f.a.bridge.r.eval
        lost = []
        def eval(script, n, *args):
            value = original(script, n, *args)
            if "KEYS[3])~=ARGV[3]" in script and not lost:
                lost.append(True)
                raise TimeoutError('fixture-lost-terminal-reply')
            return value
        f.a.bridge.r.eval = eval
        self.assertFalse(perform(f))
        self.assertFalse(f.a.transport_trusted)
        self.assertEqual(f.a.bridge.p._entry_opportunities['X'], o)
        f.a.bridge.r.eval = original
        self.assertTrue(perform(f), f.a.report)
        self.assertFalse(f.a.bridge.p._entry_opportunities)
        self.assertEqual(json.loads(f.a.bridge.r.get(key_opportunity(o['session'], 'X')))['state'], 'ENTRY_EXPIRED')

    def test_failed_read_followed_by_original_timer_remains_deferred(self):
        f = ready()
        o = add_opp(f)
        original = f.a.bridge.r.scan
        f.a.bridge.r.scan = lambda **kw: (_ for _ in ()).throw(TimeoutError('fixture'))
        self.assertFalse(perform(f))
        f.a.bridge.p.expire_entry_opportunities(f.now[0])
        self.assertEqual(f.a.bridge.r.get(key_opportunity(o['session'], 'X')), canonical_json(o))
        f.a.bridge.r.scan = original
        self.assertTrue(perform(f), f.a.report)
        self.assertFalse(f.a.bridge.p._entry_opportunities)

    def test_daily_log_limit_does_not_limit_reconciliation(self):
        f = ready()
        metrics = f.a.measure.recovery_io
        metrics.day = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()
        metrics.lines = 4096
        self.assertTrue(perform(f))
        snap = metrics.snapshot()
        self.assertEqual((snap['recoveries'], snap['log_dropped'], snap['observer_errors']), (1, 1, 0))

    def test_native_sender_outbox_entry_paths_unchanged_by_install(self):
        f = setup(('X',))
        p = f.a.bridge.p
        before = (p.on_bar, p.on_trade, p.poll_native5, p._try_entry_checked, f.a.bridge.native_cycle)
        install(f.a)
        self.assertEqual(before, (p.on_bar, p.on_trade, p.poll_native5, p._try_entry_checked, f.a.bridge.native_cycle))

    def test_canonical_scan_empty_remains_distinct_from_failed_scan(self):
        f = ready()
        self.assertTrue(perform(f))
        self.assertEqual(f.a.barrier.inventory()['A'], set())
        self.assertEqual(f.a.measure.recovery_io.snapshot()['failures'], 0)

    def test_ChainFailure_from_journal_remains_fatal(self):
        from r9core.chain import ChainFailure
        f = ready()
        f.a.checkpoint.append_epoch = lambda row: (_ for _ in ()).throw(ChainFailure('CHAIN2_FIXTURE'))
        with self.assertRaises(ChainFailure):
            perform(f)
        self.assertFalse(f.a.transport_trusted)

    def test_supposedly_readonly_caches_not_mutated_on_invalid_JSON(self):
        f = ready()
        f.a.bridge.p._entry_uncertain.add('Y')
        before = copy.deepcopy(f.a.bridge.p._entry_uncertain)
        f.a.bridge.r.set(key_trade('bad'), '{}')
        self.assertFalse(perform(f))
        self.assertEqual(f.a.bridge.p._entry_uncertain, before)

    def test_original_reference_timer_used_on_private_pipeline(self):
        from production_pipeline import ProductionDecisionPipeline
        f = ready()
        add_opp(f)
        original = ProductionDecisionPipeline.expire_entry_opportunities
        owners = []
        def timer(p, at):
            owners.append(p is f.a.bridge.p)
            return original(p, at)
        with patch.object(ProductionDecisionPipeline, 'expire_entry_opportunities', timer):
            self.assertTrue(perform(f))
        self.assertEqual(owners, [False])

    def test_snapshot_does_not_invoke_live_opportunity_observer(self):
        from ipr.entry_shadow import ObservedOpportunities
        f = ready()
        add_opp(f)
        calls = []
        f.a.bridge.p._entry_opportunities = ObservedOpportunities(f.a.bridge.p._entry_opportunities,
                                                                 lambda *a: calls.append(a))
        self.assertTrue(perform(f))
        self.assertEqual(calls, [])

    def test_journal_guard_rechecks_context_after_waiting_for_writer(self):
        f = ready()
        f.a.checkpoint.lock.acquire()
        results = []
        t = threading.Thread(target=lambda: results.append(perform(f)))
        t.start()
        try:
            limit = time.monotonic() + 2
            while not f.calls:
                self.assertLess(time.monotonic(), limit)
                time.sleep(.001)
            # Recovery will reach the evidence journal while this lock is held.
            f.a.cancel_event.set()
        finally:
            f.a.checkpoint.lock.release()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(results, [False])
        self.assertFalse(any(x.get('kind') == 'B_EVIDENCE_READY' for x in f.epochs))

    def test_correction_during_resolution_journal_is_ordered_after_resolution(self):
        f = ready()
        f.a.barrier.persisted_R.add('X')
        entered, release = threading.Event(), threading.Event()
        original = f.a.checkpoint.raw.append_epoch
        def append(row):
            if row.get('kind') == 'B_CASE_RESOLUTION':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('release fixture')
            return original(row)
        f.a.checkpoint.raw.append_epoch = append
        t = threading.Thread(target=lambda: perform(f))
        t.start()
        self.assertTrue(entered.wait(3))
        # Revocation is immediate; its evidence must wait for the already
        # executing resolution write, so replay cannot clear the NEW add_R.
        f.a.proof.invalidate('X', 'CORRECTION_OR_CANCEL')
        f.a.cancel_event.set()
        correction = threading.Thread(target=lambda: f.a.checkpoint.append_epoch(
            {'kind': 'B_STREAM_WITNESS', 'case_schema': 2, 'add_R': ['X'], 'proof': f.a.proof.snapshot()}))
        correction.start()
        release.set()
        t.join(5)
        correction.join(5)
        self.assertFalse(t.is_alive() or correction.is_alive())
        kinds = [row['kind'] for row in f.epochs]
        self.assertLess(kinds.index('B_CASE_RESOLUTION'), kinds.index('B_STREAM_WITNESS'))
        pending = set()
        for row in f.epochs:
            pending.update(row.get('add_R', []))
            pending.difference_update(row.get('resolved_R', []))
        self.assertIn('X', pending)
        self.assertFalse(f.a.transport_trusted)

    def test_concurrent_live_witness_not_lost_from_evidence_snapshot(self):
        f = ready()
        original = f.src.warmup
        def warm(*args):
            result = original(*args)
            receive(f, 'X', 1)  # not yet a completed bar at the attempt's as-of
            return result
        f.src.warmup = warm
        self.assertTrue(perform(f), f.a.report)
        evidence = next(x for x in f.epochs if x.get('kind') == 'B_EVIDENCE_READY')
        self.assertEqual(len(evidence['proof']['rows']['X']), 2)
        self.assertEqual(f.a.proof.stored_count('X'), 2)


class RecoveryEquivalence(unittest.TestCase):
    def compare(self, configure):
        values = []
        for enabled in (False, True):
            with patch.object(review_flags, 'RECOVERY_IO_OUTSIDE_LOCK_ENABLED', enabled):
                f = ready()
                configure(f)
                result = perform(f)
                p = f.a.bridge.p
                values.append((result, state_bytes(f), copy.deepcopy(p._active_by_symbol),
                               copy.deepcopy(p._entry_opportunities), set(p._entry_uncertain),
                               set(p._structure_watch_symbols), dict(p.terminal_counts), dict(p.entry_exit_counts),
                               copy.deepcopy(f.a.c.proofs), copy.deepcopy(f.a.report)))
        self.assertEqual(values[0], values[1])

    def test_empty_cases_byte_identical(self):
        self.compare(lambda f: None)

    def test_O_expiry_byte_identical(self):
        self.compare(add_opp)

    def test_A_T1_then_STOP_byte_identical(self):
        def config(f):
            add_trade(f)
            f.src.ts = [{'t': OP.isoformat(), 'p': 10.6}, {'t': (OP + timedelta(seconds=1)).isoformat(), 'p': 9.4}]
        self.compare(config)

    def test_A_timeout_byte_identical(self):
        def config(f):
            t = add_trade(f)
            t['monitoring_deadline'] = (OP + timedelta(seconds=1)).isoformat()
            f.a.bridge.r.set(key_trade('t'), canonical_json(t))
        self.compare(config)

    def test_A_no_transition_byte_identical(self):
        self.compare(add_trade)

    def test_A_halted_status_byte_identical(self):
        self.compare(lambda f: add_trade(f, 'HALTED_ACTIVE'))

    def test_U_no_commit_byte_identical(self):
        self.compare(lambda f: add_opp(f, uncertain=True))

    def test_U_missing_canonical_byte_identical(self):
        self.compare(lambda f: f.a.bridge.p._entry_uncertain.add('X'))

    def test_R_recovery_byte_identical(self):
        self.compare(lambda f: f.a.barrier.persisted_R.add('X'))

    def test_prior_session_active_trade_byte_identical(self):
        def config(f):
            add_trade(f, session='2026-10-27')
            f.src.ts = [{'t': OP.isoformat(), 'p': 9.4}]
        self.compare(config)

    def test_prior_session_opportunity_byte_identical(self):
        self.compare(lambda f: add_opp(f, session='2026-10-27'))

    def test_CanonicalConflict_not_overwritten_and_no_duplicate_opportunity(self):
        def config(f):
            o = add_opp(f)
            o.update(state='ENTRY_EXPIRED', terminal_reason='ENTRY_EXPIRED_STALE_MARKET_DATA')
            f.a.bridge.r.set(key_opportunity(o['session'], 'X'), canonical_json(o))
        self.compare(config)

    def test_production_case_refs_evidence_byte_identical(self):
        def config(f):
            add_opp(f)
            p = f.a.bridge.p
            f.a.checkpoint.raw.case_refs = lambda: {
                'O': [key_opportunity(v['session'], s) for s, v in p._entry_opportunities.items()],
                'A': [key_trade(v['trade_id']) for v in p._active_by_symbol.values()]}
        self.compare(config)
