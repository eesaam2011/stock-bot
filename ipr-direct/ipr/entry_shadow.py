"""Bounded, best-effort observation outside the byte-frozen engine.

No app state/record fields, prices, permission, timers or Redis calls are changed.
Every callback is isolated; stdout is never used by the asynchronous writer.
"""
import collections,datetime,functools,hashlib,json,math,os,queue,re,threading,time
from .price_policy import entry_price_eligible
from . import price_policy
from .telemetry_safe import bounded
from constants import ENTRY_PRICE_WAIT_SECONDS,MAX_ENTRY_TRADE_AGE_SECONDS

UTC=datetime.timezone.utc
MAX_OPPORTUNITIES=64
TRADE_RING=8
GATE_HISTORY=2048
MAX_LINE_BYTES=2048
QUEUE_SIZE=128
DETAILS_PER_SYMBOL_MINUTE=4
EVENTS_PER_SYMBOL_MINUTE=8
DETAILS_GLOBAL_MINUTE=60
EVENTS_GLOBAL_MINUTE=240
MAX_EVENTS_DAY=8192
MAX_BYTES_DAY=10*1024*1024

def instant(x):
    if isinstance(x,datetime.datetime):return x.astimezone(UTC)
    return datetime.datetime.fromisoformat(x.replace('Z','+00:00')).astimezone(UTC)
def text(x):return x if isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',x) else None
def number(x):return x if type(x) in (int,float) and math.isfinite(x) else None
def timestamp(x):
    try:return instant(x).isoformat()
    except BaseException:return None
def field_time(record,key):return timestamp(record.get(key))

def notify(observer,name,*a,**kw):
    try:return observer.safe(name,*a,**kw)
    except BaseException:
        if name=='gate':
            try:observer.gate_history_lost=True
            except BaseException:pass
        return None

class ObservedOpportunities(dict):
    """Publish exactly the original value first; callback cannot roll it back."""
    def __init__(self,initial,callback):super().__init__(initial);self.callback=callback
    def __setitem__(self,key,value):
        super().__setitem__(key,value)
        try:self.callback(key,value)
        except BaseException:pass

class EntryShadow:
    def __init__(self,p,state,wall=lambda:datetime.datetime.now(UTC),clock=time.monotonic,sink=None,boot=None):
        self.p=p;self.state=state;self.wall=wall;self.clock=clock;self.boot=boot
        self.lock=threading.RLock();self.local=threading.local();self.ops={};self.by_symbol={}
        self.gates=collections.deque(maxlen=GATE_HISTORY);self.gate_history_lost=False;self.closed=collections.deque(maxlen=8)
        self.counts=collections.Counter();self.errors=0;self.busy=0;self.generation=0
        self.q=queue.Queue(QUEUE_SIZE);self.stop=threading.Event();self.thread=None
        self.sink=sink or (lambda data:os.write(2,data))
        self.day=None;self.day_events=0;self.day_bytes=0;self.minute=None;self.minute_symbols={};self.minute_total=0;self.minute_details=0

    def safe(self,name,*a,**kw):
        # A damaged/slow observer cannot hold up an engine/reader thread.
        if not self.lock.acquire(blocking=False):
            self.busy+=1
            if name=='gate':self.gate_history_lost=True
            return None
        try:return getattr(self,name)(*a,**kw)
        except BaseException:
            self.errors+=1
            if name=='gate':self.gate_history_lost=True
            return None
        finally:self.lock.release()

    def start(self):
        def write():
            while not self.stop.is_set() or not self.q.empty():
                try:data=self.q.get(timeout=.2)
                except queue.Empty:continue
                try:
                    n=self.sink(data)
                    if n is not None and n!=len(data):self.counts['writer_errors']+=1
                except BaseException:self.counts['writer_errors']+=1
                finally:self.q.task_done()
        self.thread=threading.Thread(target=write,name='ipr-entry-shadow-log',daemon=True);self.thread.start()
    def close(self):self.stop.set() # Never wait for a potentially blocked log sink.

    def _emit(self,stage,symbol,fields,detail=False):
        now=self.wall();day=now.date().isoformat();minute=int(now.timestamp()//60)
        if self.day is None or day>self.day:self.day=day;self.day_events=0;self.day_bytes=0
        if self.minute is None or minute>self.minute:self.minute=minute;self.minute_symbols={};self.minute_total=0;self.minute_details=0
        bucket=self.minute_symbols.setdefault(symbol,[0,0]) if symbol in self.minute_symbols or len(self.minute_symbols)<MAX_OPPORTUNITIES else None
        if bucket is None or bucket[0]>=EVENTS_PER_SYMBOL_MINUTE or self.minute_total>=EVENTS_GLOBAL_MINUTE or (detail and (bucket[1]>=DETAILS_PER_SYMBOL_MINUTE or self.minute_details>=DETAILS_GLOBAL_MINUTE)):
            self.counts['log_rate_dropped']+=1;return
        row={'stage':stage,'session':self.p.session,'boot':self.boot,'symbol':symbol,'observed_at':now.isoformat(),'policy_enforced':price_policy.ENTRY_PRICE_POLICY_ENFORCED,**fields}
        data=(json.dumps(row,allow_nan=False,separators=(',',':'))+'\n').encode()
        if len(data)>MAX_LINE_BYTES:
            if 'context' in row:
                ctx=row['context'];row['context']={kind:{k:v for k,v in values.items() if k in {
                    'fetch_seconds','evaluation_seconds','rest_wait_seconds','rest_hold_seconds','logical_cycle_at',
                    'elapsed_to_publication_seconds','canonical_seconds','canonical_started_at','canonical_returned_at','confluence_total_seconds','rest_measurement_available'}}
                    for kind,values in ctx.items() if kind in ('native','confluence')}
            row['details_truncated']=True
            data=(json.dumps(row,allow_nan=False,separators=(',',':'))+'\n').encode()
        if len(data)>MAX_LINE_BYTES:
            row={k:v for k,v in row.items() if k not in {'nearest_same_price','first_eligible'}}
            data=(json.dumps(row,allow_nan=False,separators=(',',':'))+'\n').encode()
        if len(data)>MAX_LINE_BYTES:self.counts['log_size_dropped']+=1;return
        if self.day_events>=MAX_EVENTS_DAY or self.day_bytes+len(data)>MAX_BYTES_DAY:
            self.counts['log_day_dropped']+=1;return
        self.day_events+=1;self.day_bytes+=len(data);bucket[0]+=1;self.minute_total+=1
        if detail:bucket[1]+=1;self.minute_details+=1
        try:self.q.put_nowait(data)
        except queue.Full:self.counts['log_queue_dropped']+=1

    def gate(self,row):
        at=instant(row['at'])
        if self.gates and at<self.gates[-1][0]:self.gate_history_lost=True
        self.gates.append((at,bool(row['to'])))
    def seed_gate(self):self.gates.append((self.wall(),bool(self.state.get('scan_ok'))))
    def gate_at(self,at):
        if self.gate_history_lost:return None
        for when,value in reversed(self.gates):
            if when<=at:return value
        return None
    def _contexts(self):return getattr(self.local,'contexts',[])
    def context_begin(self,kind,args):
        ctx={'kind':kind,'start':self.clock(),'wall':self.wall().isoformat()}
        if kind=='native':ctx.update(logical_cycle_at=timestamp(args[0]) if args else None,fetch_seconds=0.,evaluation_seconds=0.,rest_wait_seconds=0.,rest_hold_seconds=0.,rest_count=0,rest_measurement_available=False)
        if kind=='bar':ctx.update(symbol=text(args[0]),received_at=timestamp(args[2]))
        if kind=='confluence':ctx.update(symbol=text(args[0]),signals={},logical_argument_at=timestamp(args[1]) if len(args)>1 else None)
        if kind=='evaluate':ctx.update(symbol=text(args[0]))
        if kind=='get':
            ctx['record_type']=args[1] if len(args)>1 and args[1] in ('early_core','base_ready','opportunity') else None
            for parent in self._contexts():
                if parent['kind']=='confluence':parent.setdefault('first_get_at',ctx['wall'])
        self.local.contexts=self._contexts()[-7:]+[ctx];return ctx
    def rest_scope(self,ctx,available):
        if ctx is not None:ctx['rest_measurement_available']=available
    def context_end(self,ctx,result,failed):
        if ctx is None:return
        elapsed=max(0.,self.clock()-ctx['start']);stack=self._contexts()
        try:
            native=next((c for c in stack if c['kind']=='native'),None)
            confluence=next((c for c in stack if c['kind']=='confluence'),None)
            if ctx['kind']=='fetch' and native is not None:
                native['fetch_seconds']+=elapsed;native['last_fetch_seconds']=elapsed
            elif ctx['kind']=='evaluate' and native is not None:
                native['evaluation_seconds']+=elapsed;native['last_evaluation_seconds']=elapsed;native['last_evaluation_symbol']=ctx.get('symbol')
            elif ctx['kind']=='canonical' and confluence is not None:
                confluence['canonical_seconds']=elapsed;confluence['canonical_started_at']=ctx['wall'];confluence['canonical_returned_at']=self.wall().isoformat()
            elif ctx['kind']=='get' and confluence is not None and isinstance(result,dict) and ctx.get('record_type') in ('early_core','base_ready'):
                confluence['signals'][ctx['record_type']]={k:field_time(result,k) for k in ('decision_available_ts','received_at','bar_end_ts')}
            elif ctx['kind']=='get' and confluence is not None and isinstance(result,dict) and ctx.get('record_type')=='opportunity':
                confluence['existing_canonical_loaded']=result.get('state')=='CONFLUENCE_VALID'
            elif ctx['kind']=='confluence' and ctx.get('opid') in self.ops:
                self.ops[ctx['opid']]['context']['confluence_total_seconds']=elapsed
            elif ctx['kind']=='native':self.counts['native_cycles_observed']+=1
        finally:self.local.contexts=[c for c in stack if c is not ctx]

    def rest(self,operation,wait,hold):
        def record():
            for c in self._contexts():
                if c['kind']=='native':
                    c['rest_count']+=1;c['rest_wait_seconds']+=wait;c['rest_hold_seconds']+=hold
        if self.lock.acquire(False):
            try:record()
            except BaseException:self.errors+=1
            finally:self.lock.release()
        else:self.busy+=1

    def _context_snapshot(self):
        out={};now=self.clock()
        for c in self._contexts():
            if c['kind'] in ('native','bar','confluence'):
                row={k:v for k,v in c.items() if k not in ('start','kind','opid')}
                row['elapsed_to_publication_seconds']=max(0.,now-c['start']);out[c['kind']]=row
        return out
    def _id(self,symbol,record):
        trigger=instant(record['entry_trigger_ts'])
        key=hashlib.sha256((self.p.session+'|'+symbol+'|'+trigger.isoformat()).encode()).hexdigest()[:20]
        return key,trigger
    def publish(self,symbol,record,restored=False):
        key,trigger=self._id(symbol,record)
        if key in self.ops:self.by_symbol[symbol]=(id(record),key);return
        if len(self.ops)>=MAX_OPPORTUNITIES:self.counts['opportunity_capacity_dropped']+=1;return
        now=self.wall();self.generation+=1
        restored=restored or any(c.get('existing_canonical_loaded') for c in self._contexts() if c['kind']=='confluence')
        origin='canonical_restore_or_initial' if restored else 'engine_cache_publication'
        state={'opid':key,'symbol':symbol,'trigger':trigger,'deadline':trigger+datetime.timedelta(seconds=ENTRY_PRICE_WAIT_SECONDS),
               'published':now,'origin':origin,'gate_signal':self.gate_at(trigger),'gate_publication':bool(self.state.get('scan_ok')),
               'context':self._context_snapshot(),'ring':collections.deque(maxlen=TRADE_RING),'received':0,'rejected':0,'processed':0,
               'within_wait_received':0,'unknown':0,'first_eligible':None,'pre_first_count':None,'pre_first_rejected_pct':None,
               'errors_at_start':self.errors,'busy_at_start':self.busy,'metadata':collections.Counter()}
        self.ops[key]=state;self.by_symbol[symbol]=(id(record),key);self.counts['opportunities']+=1
        for c in self._contexts():
            if c['kind']=='confluence':c['opid']=key
        self._emit('R103_OPPORTUNITY_PUBLISHED',symbol,{'opid':key,'trigger_at':trigger.isoformat(),'deadline':state['deadline'].isoformat(),
             'publication_delay_seconds':(now-trigger).total_seconds(),'appeared_after_deadline':now>state['deadline'],
             'scan_ok_at_trigger':state['gate_signal'],'scan_ok_at_publication':state['gate_publication'],'origin':origin,'context':state['context']})
    def ensure_map(self):
        original=self.p._entry_opportunities
        if not isinstance(original,ObservedOpportunities):
            self.p._entry_opportunities=ObservedOpportunities(original,lambda s,r:self.safe('publish',s,r))
        for symbol,record in tuple(self.p._entry_opportunities.items()):
            if record.get('state')=='CONFLUENCE_VALID':self.publish(symbol,record,True)
    def _op(self,symbol):
        record=self.p._entry_opportunities.get(symbol)
        if record is None:return None
        known=self.by_symbol.get(symbol)
        if known and known[0]==id(record):return self.ops.get(known[1])
        key,_=self._id(symbol,record)
        if key not in self.ops:self.publish(symbol,record,True)
        return self.ops.get(key)

    def _message(self,msg,received):
        c=msg.get('c');z=msg.get('z');schema={}
        schema['z_missing']='z' not in msg;schema['c_missing']='c' not in msg
        schema['c_empty']=isinstance(c,list) and not c
        schema['z_unexpected']=not schema['z_missing'] and (not isinstance(z,str) or len(z)!=1 or z not in 'ABCO')
        schema['c_unexpected']=not schema['c_missing'] and (not isinstance(c,list) or len(c)>16 or any(not isinstance(v,str) or len(v)!=1 for v in c[:16]))
        if isinstance(c,list) and len(c)>16:eligible=None
        else:eligible=entry_price_eligible(msg)
        flags=[v if isinstance(v,str) and len(v)==1 and v.isprintable() else '[INVALID]' for v in c[:16]] if isinstance(c,list) else None
        identifier=msg.get('i');identifier=identifier if type(identifier) is int else None
        row={'i':identifier,'x':text(msg.get('x')),'p':number(msg.get('p')),'s':number(msg.get('s')),
             't':timestamp(msg.get('t')),'c':flags,'z':z if isinstance(z,str) and len(z)==1 and z.isprintable() else None,
             'received_at':timestamp(received),'seq':msg.get('_seq') if type(msg.get('_seq')) is int else None,
             'price_eligible':eligible,'schema':schema}
        return row
    def _same(self,a,b):return all(a.get(k)==b.get(k) for k in ('i','x','p','t','received_at'))
    def _eligible_in_wait(self,op,row):
        if row['price_eligible'] is not True or row['p'] is None or row['p']<=0 or row['t'] is None:return False
        received=instant(row['received_at']);age=(received-instant(row['t'])).total_seconds()
        return op['trigger']<=received<=op['deadline'] and 0<=age<=MAX_ENTRY_TRADE_AGE_SECONDS
    def trade(self,symbol,msg,received,source):
        op=self._op(symbol)
        if op is None:return None
        row=self._message(msg,received);row['source']=source;row['opid']=op['opid']
        within=op['trigger']<=instant(row['received_at'])<=op['deadline']
        if source=='READER':
            op['received']+=1;op['rejected']+=int(row['price_eligible'] is False);op['unknown']+=int(row['price_eligible'] is None);op['within_wait_received']+=int(within)
            self.counts['received_active_opportunity_TRADES']+=1
            self.counts['price_rejected']+=int(row['price_eligible'] is False);self.counts['price_unknown']+=int(row['price_eligible'] is None)
            for k,v in row['schema'].items():
                if v:op['metadata'][k]+=1;self.counts[k]+=1
            op['ring'].append(row)
            self._emit('R103_PRICE_SHADOW_TRADE',symbol,{'opid':op['opid'],**row,'within_wait':within},True)
        else:
            op['processed']+=1
            old=next((x for x in reversed(op['ring']) if self._same(x,row)),None)
            if old is not None:old['seq']=row['seq']
            else:op['ring'].append(row);self.counts['process_only_trade_observations']+=1
        if self._eligible_in_wait(op,row) and op['first_eligible'] is None:
            op['first_eligible']=dict(row)
            if source=='READER':
                n=op['received']-1;op['pre_first_count']=n
                op['pre_first_rejected_pct']=(100*op['rejected']/n if n else 0.) if not op['unknown'] else None
            self._emit('R103_FIRST_ELIGIBLE_TRADE',symbol,{'opid':op['opid'],'first_eligible':row,'preceding_received_count':op['pre_first_count'],
                  'preceding_rejected_pct':op['pre_first_rejected_pct'],'seconds_from_trigger':(instant(row['received_at'])-op['trigger']).total_seconds()})
        return row
    def received(self,msg,received):
        if msg.get('T')=='t':return self.trade(msg.get('S'),msg,received,'READER')

    def process_begin(self,symbol,msg,received):
        prior=getattr(self.local,'current_trade',None)
        self.local.current_trade=self.trade(symbol,msg,received,'PROCESS')
        return {'prior':prior}
    def process_end(self,token):
        if token is not None:self.local.current_trade=token['prior']

    def commit(self,symbol,trade,selected,current):
        if not isinstance(trade,dict):return
        op=self.ops.get(current.get('opid')) if isinstance(current,dict) else None
        if op is None:op=next((x for x in self.ops.values() if x['symbol']==symbol),None)
        if op is None:return
        # Canonical trade.created_at is built from entry_alert_sent_at by EntryCommitBuilder.
        price=number(trade.get('entry_alert_price'));logical=instant(trade['created_at'])
        rows=list(op['ring'])+([current] if isinstance(current,dict) else [])
        same=[x for x in rows if x['p']==price]
        nearest=min(same,key=lambda x:abs((instant(x['received_at'])-logical).total_seconds())) if same else None
        exact=None
        if selected is not None:
            exact=next((x for x in reversed(same) if x['seq']==selected.sequence and instant(x['received_at'])==selected.received_at and instant(x['t'])==selected.trade_ts),None)
        self._emit('R103_ENTRY_PRICE_SHADOW',symbol,{'opid':op['opid'],'entry_price':price,'entry_at_declared':logical.isoformat(),
                  'selected_trade_policy_eligible':exact['price_eligible'] if exact else None,
                  'triggering_trade_policy_eligible':current.get('price_eligible') if isinstance(current,dict) else None,
                  'counterfactual_ENTRY':'NOT_SIMULATED','selected_match':'EXACT_RECEIPT_SEQUENCE_MARKET_TIME_PRICE' if exact else 'UNKNOWN',
                  'selected_trade':exact,'nearest_same_price':nearest})
        self._finish(op,'ENTRY_COMMITTED',None,logical)
    def terminal(self,record,state,reason):
        key,_=self._id(record['symbol'],record);op=self.ops.get(key)
        if op is not None:self._finish(op,text(state),text(reason),None)
    def _finish(self,op,outcome,reason,logical):
        now=self.wall();gaps=self.errors!=op['errors_at_start'] or self.busy!=op['busy_at_start']
        full=op['origin']=='engine_cache_publication' and op['published']<=op['trigger'] and not gaps
        yes=op['first_eligible'] is not None
        within_result=True if yes else False if full and now>=op['deadline'] else None
        row={'opid':op['opid'],'trigger_at':op['trigger'].isoformat(),'published_at':op['published'].isoformat(),
             'publication_delay_seconds':(op['published']-op['trigger']).total_seconds(),'terminal_observed_at':now.isoformat(),
             'trigger_to_terminal_observed_seconds':(now-op['trigger']).total_seconds(),'terminal_declared_at':logical.isoformat() if logical else None,
             'outcome':outcome,'reason':reason,'scan_ok_at_trigger':op['gate_signal'],'scan_ok_at_terminal':bool(self.state.get('scan_ok')),
             'received_TRADES':op['received'],'processed_TRADES':op['processed'],'rejected_price_conditions':op['rejected'],
             'rejected_pct':100*op['rejected']/op['received'] if op['received'] and not op['unknown'] else None,'unknown_policy_count':op['unknown'],'within_wait_received':op['within_wait_received'],
             'eligible_received_in_wait':within_result,'first_eligible':op['first_eligible'],
             'preceding_first_eligible_count':op['pre_first_count'],'preceding_first_eligible_rejected_pct':op['pre_first_rejected_pct'],
             'received_coverage_starts_at':op['published'].isoformat(),'coverage_from_trigger':full,'observation_gaps':gaps,
             'appeared_after_deadline':op['published']>op['deadline'],'context':op['context']}
        self._emit('R103_OPPORTUNITY_FINAL',op['symbol'],row)
        self.closed.append({k:row[k] for k in ('opid','outcome','reason','eligible_received_in_wait','coverage_from_trigger')})
        self.ops.pop(op['opid'],None);self.counts['opportunities_finalized']+=1
        known=self.by_symbol.get(op['symbol'])
        if known and known[1]==op['opid']:self.by_symbol.pop(op['symbol'],None)
    def snapshot(self):
        row={'mode':'ENFORCED' if price_policy.ENTRY_PRICE_POLICY_ENFORCED else 'SHADOW','counts':dict(self.counts),'observer_errors':self.errors,'observations_skipped_busy':self.busy,
             'gate_history_lost':self.gate_history_lost,
             'active_tracked':len(self.ops),'log_queue':self.q.qsize(),'day_log_events_reserved':self.day_events,'day_log_bytes_reserved':self.day_bytes,
             'coverage':'opportunity-visible READER scope; PROCESS_ONLY separate; check error/gap counters'}
        result=bounded(row,1024)
        if result and result.get('truncated'):
            row['counts']={k:self.counts[k] for k in ('received_active_opportunity_TRADES','price_rejected','price_unknown','z_missing','c_missing','c_empty','z_unexpected','c_unexpected','log_rate_dropped','log_day_dropped','log_queue_dropped','writer_errors','opportunity_capacity_dropped')}
            row.pop('coverage',None);row['counts_truncated']=True;return bounded(row,1024)
        return result

def install_shadow(r,state,api,boot):
    """All installed wrappers call the original exactly once; no app exception swallowed."""
    observer=None
    try:
        p=r.a.bridge.p;observer=EntryShadow(p,state,wall=r.wall,boot=getattr(r,'boot_number',None));r.price_shadow=observer
        notify(observer,'seed_gate');gate=state['_scan_gate'];old_log=gate.log
        def logged(row):
            notify(observer,'gate',row)
            return old_log(row)
        gate.log=logged
        old_receive=r.observe_stream
        def received(msg,at):
            try:return old_receive(msg,at) if old_receive else None
            finally:
                try:
                    if msg.get('T')=='t' and msg.get('S') in p._entry_opportunities:notify(observer,'received',msg,at)
                except BaseException:pass
        r.observe_stream=received
        def context_method(obj,name,kind):
            original=getattr(obj,name,None)
            if original is None:return
            @functools.wraps(original)
            def wrapped(*a,**kw):
                if kind=='get':
                    try:tracked=any(c['kind']=='confluence' for c in observer._contexts())
                    except BaseException:tracked=False
                    if not tracked:return original(*a,**kw)
                ctx=notify(observer,'context_begin',kind,a)
                result=None;failed=True
                try:
                    if kind=='native':
                        from .telemetry_safe import observing
                        with observing(lambda:api.observe_requests(lambda *v:notify(observer,'rest',*v))) as available:
                            notify(observer,'rest_scope',ctx,available)
                            result=original(*a,**kw)
                    else:result=original(*a,**kw)
                    failed=False;return result
                finally:
                    notify(observer,'context_end',ctx,result,failed)
                    # TLS cleanup must run even when the nonblocking observer lock is busy.
                    try:observer.local.contexts=[c for c in observer._contexts() if c is not ctx][-8:]
                    except BaseException:pass
            setattr(obj,name,wrapped)
        for obj,name,kind in [(p,'on_bar','bar'),(p,'poll_native5','native'),(p,'_confluence','confluence'),(p,'_get','get'),
                              (p.rest,'bars_multi','fetch'),(p.cw,'evaluate_and_persist','canonical'),
                              (p.ec,'evaluate_with_telemetry','evaluate'),(p.ec,'evaluate','evaluate')]:context_method(obj,name,kind)
        original_refresh=p.refresh_runtime_caches
        @functools.wraps(original_refresh)
        def refresh(*a,**kw):
            # Keep original cache refresh and observer-map installation atomic
            # against original engine writers; never replace a stale copied dict.
            with p._decision_lock:
                result=original_refresh(*a,**kw);notify(observer,'ensure_map');return result
        p.refresh_runtime_caches=refresh;notify(observer,'ensure_map')
        original_trade=p.on_trade
        @functools.wraps(original_trade)
        def on_trade(symbol,msg,at,allow):
            token=notify(observer,'process_begin',symbol,msg,at)
            try:return original_trade(symbol,msg,at,allow)
            finally:
                notify(observer,'process_end',token)
                try:observer.local.current_trade=token['prior'] if token is not None else None
                except BaseException:pass
        p.on_trade=on_trade
        original_entry=p._try_entry_checked
        @functools.wraps(original_entry)
        def entry(symbol,at):
            selected=None;before=None
            try:
                rows=p.trades.get(symbol,[]);selected=rows[-1] if rows else None;before=p._active_by_symbol.get(symbol)
            except BaseException:pass
            result=original_entry(symbol,at)
            try:
                after=p._active_by_symbol.get(symbol)
                if after is not None and after is not before:notify(observer,'commit',symbol,after,selected,getattr(observer.local,'current_trade',None))
            except BaseException:observer.errors+=1
            return result
        p._try_entry_checked=entry
        original_terminal=p._terminal_opportunity
        @functools.wraps(original_terminal)
        def terminal(symbol,record,at,outcome,reason):
            result=original_terminal(symbol,record,at,outcome,reason);notify(observer,'terminal',record,outcome,reason);return result
        p._terminal_opportunity=terminal
        observer.start()
    except BaseException:
        if observer is not None:observer.errors+=1
    return observer
