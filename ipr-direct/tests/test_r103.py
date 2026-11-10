"""R10.3: shadow equivalence, lifecycle timing, bounded output, fault isolation."""
import contextlib,copy,io,json,threading,time,unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch
from pathlib import Path
from r103_support import pipeline,trade,feed
from test_direct import OP
from test_r8 import filter_actual
from ipr.data import dt
from ipr.classifier import Classifier
from ipr.scan_health import ScanGate
from ipr.entry_shadow import EntryShadow,install_shadow,notify,ObservedOpportunities,MAX_LINE_BYTES,MAX_BYTES_DAY,MAX_EVENTS_DAY,QUEUE_SIZE
from ipr.telemetry_safe import SafeTimings,observing
from ipr.scan_timing import safe_timings
from ipr import price_policy

class Sim:
    def __init__(self):self.t=0.
    def wall(self):return OP+timedelta(seconds=self.t)
    def clock(self):return self.t

def rows(o):
    result=[]
    while not o.q.empty():result.append(json.loads(o.q.get_nowait()))
    return result

def fixture(shadow=True):
    t=Sim();p=pipeline('X',OP,10);p._confluence_retry=set()
    p.rest=NS(bars_multi=lambda *a,**kw:{})
    p.ec=NS(evaluate=lambda *a,**kw:None,evaluate_with_telemetry=lambda *a,**kw:({},None))
    p.cw=NS(evaluate_and_persist=lambda *a,**kw:(None,None))
    p.refresh_runtime_caches=lambda:None
    state={'ready':True,'scan_ok':True};state['_scan_gate']=ScanGate(state,t.clock,t.wall,log=lambda x:None)
    r=NS(a=NS(bridge=NS(p=p)),wall=t.wall,observe_stream=lambda *a:None,boot_number=2)
    if shadow:
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,state,NS(),'fixture')
        o.clock=t.clock
    else:o=None
    return t,p,state,r,o

class R103Policy(unittest.TestCase):
    def test_owner_INSE_only_and_ACTIVTY_frozen(self):
        a=lambda s:dict(name=s+' Common Stock',exchange='NASDAQ',status='active',tradable=True,id=s)
        c=Classifier({s:a(s) for s in ('INSE','INSE2','X')})
        self.assertFalse(c.eligible('INSE'));self.assertTrue(c.eligible('INSE2'));self.assertTrue(c.eligible('X'))
        import hashlib
        p=Path(__file__).resolve().parents[1]/'classifications/ACTIVITY.txt'
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),'319e4857684396441a1666bc06ae4420c8b317c7de1a093b54a5be2aa42ff50e')
    def test_shadow_flag_never_filters_oddlot_or_missing_metadata(self):
        self.assertIs(price_policy.ENTRY_PRICE_POLICY_ENFORCED,False)
        f=filter_actual(dict(prepared={'ready':True,'scan_ok':True},bundle={'eligible':['X']},cls=NS(eligible=lambda s:True),dt=dt))
        for extra in ({},{'c':['@','I'],'z':'C'},{'c':['?'],'z':None}):self.assertTrue(f('X',dict(p=2,t=OP.isoformat(),**extra),OP))
    def test_enforcement_is_one_isolated_flag_future_only(self):
        with patch.object(price_policy,'ENTRY_PRICE_POLICY_ENFORCED',True):
            self.assertFalse(price_policy.price_policy_allows_entry({'c':['@','I'],'z':'C'}))
            self.assertTrue(price_policy.price_policy_allows_entry({'c':['@'],'z':'C'}))
    def test_real_filter_excludes_INSE_even_in_cached_bundle(self):
        assets={s:dict(name=s+' Common Stock',exchange='NASDAQ',status='active',tradable=True,id=s) for s in ('INSE','X')}
        f=filter_actual(dict(prepared={'ready':True,'scan_ok':True},bundle={'eligible':['INSE','X']},cls=Classifier(assets),dt=dt))
        self.assertFalse(f('INSE',dict(p=3.5,t=OP.isoformat()),OP));self.assertTrue(f('X',dict(p=3.5,t=OP.isoformat()),OP))
    def test_official_alpaca_WS_example_is_observed_without_enforcement(self):
        m=json.loads((Path(__file__).parent/'fixtures/alpaca_ws_trade_official.json').read_text())
        _,p,s,r,o=fixture();row=o._message(m,dt(m['t']))
        self.assertFalse(any(row['schema'].values()));self.assertFalse(row['price_eligible'])
        self.assertEqual(row['z'],'C');self.assertEqual(row['c'],['@','I'])

class R103Equivalence(unittest.TestCase):
    def setUp(self):self.out=io.StringIO();self.capture=contextlib.redirect_stdout(self.out);self.capture.__enter__()
    def tearDown(self):self.capture.__exit__(None,None,None)
    def test_oddlot_commits_identically_but_hypothetical_reject_is_logged(self):
        t,p,s,r,o=fixture();_,base,_,_,_=fixture(False)
        m=trade('X',OP,10,('@','I'))
        r.observe_stream(m,OP);feed(p,m,True);feed(base,m,True)
        self.assertEqual(p.lua.entries,base.lua.entries);self.assertEqual(p._active_by_symbol,base._active_by_symbol)
        entry=next(x for x in rows(o) if x['stage']=='R103_ENTRY_PRICE_SHADOW')
        self.assertFalse(entry['selected_trade_policy_eligible']);self.assertFalse(entry['triggering_trade_policy_eligible']);self.assertEqual(entry['selected_trade']['c'],['@','I'])
        self.assertEqual(entry['selected_match'],'EXACT_RECEIPT_SEQUENCE_MARKET_TIME_PRICE')
    def test_followup_STOP_T1_T2_identical(self):
        for event in ('STOP','T1_T2'):
            t,p,s,r,o=fixture();_,base,_,_,_=fixture(False)
            for target in (p,base):feed(target,trade('X',OP,10),True)
            level=p._active_by_symbol['X']
            prices=[level['structural_stop']-.01] if event=='STOP' else [level['t1'],level['t2']]
            for i,price in enumerate(prices,1):
                m=trade('X',OP+timedelta(seconds=i),price,('@','I'))
                for target in (p,base):feed(target,m,True)
            self.assertEqual(p.lua.followups,base.lua.followups);self.assertEqual(p._active_by_symbol,base._active_by_symbol)
    def test_broken_observer_does_not_change_commit(self):
        _,p,_,_,o=fixture();_,base,_,_,_=fixture(False)
        def fail(*a,**kw):raise SystemExit('observer only')
        o.safe=fail
        for target in (p,base):feed(target,trade('X',OP,10),True)
        self.assertEqual(p.lua.entries,base.lua.entries)
    def test_original_exception_identity_survives_broken_observer(self):
        _,p,_,_,o=fixture();error=RuntimeError('application')
        p._monitor_trade=lambda *a:(_ for _ in ()).throw(error)
        o.safe=lambda *a,**kw:(_ for _ in ()).throw(SystemExit('observer'))
        with self.assertRaises(RuntimeError) as cm:feed(p,trade('X',OP,10),True)
        self.assertIs(cm.exception,error)
    def test_original_terminal_reason_and_sink_identical(self):
        t,p,s,r,o=fixture();_,base,_,_,_=fixture(False)
        for target in (p,base):target.trades.clear();target._fixture_now=OP+timedelta(seconds=11)
        t.t=11
        for target in (p,base):target.expire_entry_opportunities(target._fixture_now)
        self.assertEqual(p.lua.terminals,base.lua.terminals)
        final=next(x for x in rows(o) if x['stage']=='R103_OPPORTUNITY_FINAL')
        self.assertEqual(final['reason'],'ENTRY_EXPIRED_STALE_MARKET_DATA');self.assertIsNone(final['eligible_received_in_wait'])
    def test_original_reader_exception_not_suppressed(self):
        t,p,s,r,o=fixture(False);error=ValueError('original reader')
        r.observe_stream=lambda *a:(_ for _ in ()).throw(error)
        with patch.object(EntryShadow,'start',lambda self:None):install_shadow(r,s,NS(),'fixture')
        with self.assertRaises(ValueError) as cm:r.observe_stream(trade('X',OP,10),OP)
        self.assertIs(cm.exception,error)
    def test_refresh_reinstalls_observer_without_changing_values(self):
        t,p,s,r,o=fixture();initial=dict(p._entry_opportunities)
        self.assertIsInstance(p._entry_opportunities,ObservedOpportunities)
        p.refresh_runtime_caches();self.assertEqual(p._entry_opportunities,initial)
    def test_cache_replacement_cannot_race_an_original_engine_writer(self):
        t,p,s,r,o=fixture(False);initial=dict(p._entry_opportunities)
        def original_refresh():p._entry_opportunities=dict(initial);return 42
        p.refresh_runtime_caches=original_refresh
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,s,NS(),'fixture')
        original_observe=o.ensure_map;seen=[]
        def observed():
            seen.append(p._decision_lock.lock._is_owned());original_observe()
        o.ensure_map=observed
        self.assertEqual(p.refresh_runtime_caches(),42);self.assertEqual(seen,[True])
        self.assertEqual(p._entry_opportunities,initial);self.assertIsInstance(p._entry_opportunities,ObservedOpportunities)

class R103Lifecycle(unittest.TestCase):
    def setUp(self):self.out=io.StringIO();self.capture=contextlib.redirect_stdout(self.out);self.capture.__enter__()
    def tearDown(self):self.capture.__exit__(None,None,None)
    def test_every_received_trade_counted_even_when_detail_sample_is_full(self):
        t,p,s,r,o=fixture();op=next(iter(o.ops.values()))
        for i in range(100):r.observe_stream(trade('X',OP+timedelta(milliseconds=i),10,('@','I')),OP+timedelta(milliseconds=i))
        r.observe_stream(trade('X',OP+timedelta(seconds=1),10),OP+timedelta(seconds=1))
        self.assertEqual(op['received'],101);self.assertEqual(op['rejected'],100);self.assertEqual(op['pre_first_count'],100)
        self.assertEqual(op['pre_first_rejected_pct'],100)
        logged=rows(o);self.assertEqual(sum(x['stage']=='R103_PRICE_SHADOW_TRADE' for x in logged),4)
        self.assertTrue(any(x['stage']=='R103_FIRST_ELIGIBLE_TRADE' for x in logged))
    def test_missing_and_malformed_z_c_counters(self):
        _,p,s,r,o=fixture()
        messages=[dict(T='t',S='X',p=10,s=1,t=OP.isoformat()),dict(T='t',S='X',p=10,s=1,t=OP.isoformat(),z=7,c='@')]
        for m in messages:r.observe_stream(m,OP)
        c=o.safe('snapshot')['counts']
        for k in ('z_missing','c_missing','z_unexpected','c_unexpected'):self.assertEqual(c[k],1)
    def test_empty_and_overlong_conditions_are_visible_without_false_ratio(self):
        _,p,s,r,o=fixture();op=next(iter(o.ops.values()))
        m=trade('X',OP,10);m['c']=[];r.observe_stream(m,OP)
        m['c']=['@']*17;r.observe_stream(m,OP)
        self.assertEqual(o.counts['c_empty'],1);self.assertEqual(op['unknown'],1)
        self.assertEqual(o.counts['price_unknown'],1)
    def test_sanitization_and_size_do_not_leak_non_trade_credentials(self):
        _,p,s,r,o=fixture();m=trade('X',OP,10);m.update(c=['very-secret-token'],z='https://secret.invalid',api_key='must-not-copy')
        r.observe_stream(m,OP);logged=json.dumps(rows(o));self.assertNotIn('very-secret-token',logged);self.assertNotIn('secret.invalid',logged);self.assertNotIn('must-not-copy',logged)
    def test_exact_deadline_and_five_second_source_age(self):
        _,p,s,r,o=fixture();op=next(iter(o.ops.values()))
        r.observe_stream(trade('X',OP+timedelta(seconds=5),10),OP+timedelta(seconds=10))
        self.assertIsNotNone(op['first_eligible']);self.assertEqual(op['first_eligible']['received_at'],(OP+timedelta(seconds=10)).isoformat())
    def test_over_deadline_is_not_first_eligible(self):
        _,p,s,r,o=fixture();op=next(iter(o.ops.values()))
        r.observe_stream(trade('X',OP+timedelta(seconds=10),10),OP+timedelta(seconds=10.000001))
        self.assertIsNone(op['first_eligible'])
    def test_publish_hook_precedes_slow_original_print_and_measures_native_context(self):
        t,p,s,r,o=fixture(False);record=copy.deepcopy(p._entry_opportunities['X']);p._entry_opportunities={}
        p._get=lambda key,kind=None:None if kind=='opportunity' else {'bar_end_ts':OP.isoformat(),'decision_available_ts':OP.isoformat(),'received_at':OP.isoformat()}
        p.store=NS(read=lambda *a:record)
        def canonical(*a):t.t+=3;return ('fixture','opportunity')
        p.cw.evaluate_and_persist=canonical
        def fetched(*a,**kw):t.t+=30;return {}
        p.rest.bars_multi=fetched
        def evaluated(*a):t.t+=2;return ({},None)
        p.ec.evaluate_with_telemetry=evaluated
        def native(now,allow):
            p.rest.bars_multi(['X'],now,now,'5Min');p.ec.evaluate_with_telemetry('X',[],now);p._confluence('X',now)
        p.poll_native5=native
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,s,NS(),'fixture')
        o.clock=t.clock
        original_print=print
        def slow_print(*a,**kw):
            if a and isinstance(a[0],dict) and a[0].get('stage')=='SHADOW_CONFLUENCE':t.t+=7
            return original_print(*a,**kw)
        with patch('builtins.print',slow_print):p.poll_native5(OP,True)
        op=next(iter(o.ops.values()));self.assertEqual((op['published']-OP).total_seconds(),35)
        self.assertEqual(t.t,42);self.assertEqual(op['context']['native']['fetch_seconds'],30)
        self.assertEqual(op['context']['native']['evaluation_seconds'],2)
        self.assertEqual(op['context']['confluence']['canonical_seconds'],3)
    def test_gate_history_at_trigger_and_at_terminal(self):
        t,p,s,r,o=fixture();t.t=2;s['_scan_gate'].invalidate('fixture')
        p._fixture_now=OP+timedelta(seconds=11);t.t=11;p.expire_entry_opportunities(p._fixture_now)
        final=next(x for x in rows(o) if x['stage']=='R103_OPPORTUNITY_FINAL')
        self.assertTrue(final['scan_ok_at_trigger']);self.assertFalse(final['scan_ok_at_terminal'])
    def test_existing_canonical_opportunity_is_not_claimed_as_new_creation(self):
        t,p,s,r,o=fixture(False);record=copy.deepcopy(p._entry_opportunities['X']);p._entry_opportunities={}
        p._get=lambda *a,**kw:record
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,s,NS(),'fixture')
        o.clock=t.clock;p._confluence('X',OP)
        op=next(iter(o.ops.values()));self.assertEqual(op['origin'],'canonical_restore_or_initial')
    def test_old_trigger_outside_retained_history_is_unknown(self):
        _,p,s,r,o=fixture();self.assertIsNone(o.gate_at(OP-timedelta(seconds=1)))
    def test_gate_observation_precedes_original_log_callback(self):
        t,p,s,r,o=fixture(False);seen=[];holder=[]
        s['_scan_gate'].log=lambda row:seen.append(holder[0].gate_at(dt(row['at'])))
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,s,NS(),'fixture')
        holder.append(o);t.t=2;s['_scan_gate'].invalidate('fixture')
        self.assertEqual(seen,[False])
    def test_missed_gate_transition_makes_historical_value_unknown(self):
        t,p,s,r,o=fixture();ready=threading.Event();release=threading.Event()
        def hold():
            with o.lock:ready.set();release.wait(2)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(ready.wait(.5))
        try:t.t=2;s['_scan_gate'].invalidate('fixture')
        finally:release.set();thread.join()
        self.assertIsNone(o.gate_at(t.wall()));self.assertFalse(s['scan_ok'])

class R103BoundsAndFailures(unittest.TestCase):
    def test_line_queue_global_day_and_per_symbol_limits(self):
        t,p,s,r,o=fixture();rows(o)
        for i in range(400):
            t.t=i*60;o.safe('_emit','R103_TEST','X',{'blob':'x'*100},True)
        self.assertLessEqual(o.q.qsize(),QUEUE_SIZE);self.assertGreater(o.counts['log_queue_dropped'],0)
        for data in list(o.q.queue):self.assertLessEqual(len(data),MAX_LINE_BYTES)
        o.day_events=MAX_EVENTS_DAY;t.t+=60;o.safe('_emit','R103_TEST','X',{})
        self.assertGreater(o.counts['log_day_dropped'],0)
        self.assertLessEqual(o.day_bytes,MAX_BYTES_DAY)
    def test_opportunity_capacity_and_snapshot_bytes_bounded(self):
        _,p,s,r,o=fixture();template=copy.deepcopy(p._entry_opportunities['X'])
        for i in range(200):
            record={**template,'symbol':'X'+str(i)};o.safe('publish',record['symbol'],record)
        self.assertEqual(len(o.ops),64);self.assertGreater(o.counts['opportunity_capacity_dropped'],0)
        self.assertLessEqual(len(json.dumps(o.safe('snapshot'),separators=(',',':')).encode()),1024)
    def test_blocked_log_sink_does_not_block_application(self):
        t,p,s,r,o=fixture();release=threading.Event();entered=threading.Event()
        def blocked(data):entered.set();release.wait(2)
        o.sink=blocked;o.start();self.assertTrue(entered.wait(.5));started=time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()):feed(p,trade('X',OP,10),True)
            self.assertEqual(len(p.lua.entries),1);self.assertLess(time.monotonic()-started,.5)
        finally:release.set();o.close()
    def test_observer_lock_busy_is_visible_and_does_not_block(self):
        _,p,s,r,o=fixture();ready=threading.Event();release=threading.Event()
        def hold():
            with o.lock:ready.set();release.wait(2)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(ready.wait(.5))
        try:
            with contextlib.redirect_stdout(io.StringIO()):feed(p,trade('X',OP,10),True)
            self.assertEqual(len(p.lua.entries),1);self.assertGreater(o.busy,0)
        finally:release.set();thread.join()
    def test_broken_context_cannot_suppress_or_replace_application_exception(self):
        class Broken:
            def __enter__(self):return self
            def __exit__(self,*a):raise SystemExit('observer')
        error=ValueError('application')
        with self.assertRaises(ValueError) as cm:
            with observing(lambda:Broken()):raise error
        self.assertIs(cm.exception,error)
    def test_suppression_request_from_observer_ignored(self):
        class Bad:
            def __enter__(self):pass
            def __exit__(self,*a):return True
        with self.assertRaisesRegex(RuntimeError,'application'):
            with observing(lambda:Bad()):raise RuntimeError('application')
    def test_broken_scan_timer_constructor_and_clock_keep_body_running(self):
        with patch('ipr.scan_timing.ScanTimings',side_effect=RuntimeError('observer')):timer=safe_timings()
        seen=[]
        with timer.phase('volume_fetch'):seen.append(1)
        self.assertEqual(seen,[1]);self.assertIsNone(timer.snapshot())
        timer=safe_timings(clock=lambda:(_ for _ in ()).throw(SystemExit('timer')))
        error=ValueError('body')
        with self.assertRaises(ValueError) as cm:
            with timer.phase('volume_fetch'):raise error
        self.assertIs(cm.exception,error)
    def test_TLS_contexts_do_not_leak_when_end_observation_fails(self):
        t,p,s,r,o=fixture(False);p.poll_native5=lambda *a,**kw:42
        with patch.object(EntryShadow,'start',lambda self:None):o=install_shadow(r,s,NS(),'fixture')
        original=o.safe
        def sometimes(name,*a,**kw):
            if name=='context_end':raise SystemExit('observer end')
            return original(name,*a,**kw)
        o.safe=sometimes
        for i in range(100):self.assertEqual(p.poll_native5(OP,True),42)
        self.assertEqual(o._contexts(),[])
    def test_TLS_current_trade_cleared_when_cleanup_observation_fails(self):
        _,p,s,r,o=fixture();original=o.safe
        def sometimes(name,*a,**kw):
            if name=='process_end':raise SystemExit('observer end')
            return original(name,*a,**kw)
        o.safe=sometimes
        with contextlib.redirect_stdout(io.StringIO()):feed(p,trade('X',OP,10),True)
        self.assertEqual(len(p.lua.entries),1);self.assertIsNone(getattr(o.local,'current_trade',None))
