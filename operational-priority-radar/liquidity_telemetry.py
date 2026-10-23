"""First E/B log enrichment ONLY. Never calls the liquidity filter or changes records."""
from pathlib import Path
import hashlib,json,math,threading
PIN='855c87dd567e31695465fe8037c5c4214b7db3d620fabf26da2ab84cc442e869'
class LiquidityTelemetry:
    def __init__(self,path=None,pin=PIN,*,symbols=None):
        self.records={};self.failure=None;self.seen=set();self.missing=set();self.lock=threading.Lock()
        self.first_session=None;self.last_session=None;self.pin=pin
        try:
            raw=Path(path or Path(__file__).parent/'research_liquidity/ADV20_20261016.json').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=pin:raise ValueError('PIN')
            x=json.loads(raw)
            if x['currency']!='USD' or x['required_sessions']!=20 or not isinstance(x['records'],dict):raise ValueError('SCHEMA')
            self.records=x['records'];self.first_session=x.get('first_session');self.last_session=x.get('last_session')
        except Exception as exc:self.failure=type(exc).__name__
        def complete(symbol):
            r=self.records.get(symbol,{})
            if not isinstance(r,dict):return False
            v=r.get('mean_dollar_volume')
            return self.failure is None and r.get('valid_days')==20 and type(v) in (int,float) and math.isfinite(v) and v>=0
        self.scope_missing_at_init=None if symbols is None else sum(not complete(s) for s in set(symbols))
    def first(self,symbol,kind,event_id):
        with self.lock:
            if (symbol,kind) in self.seen:return {'liquidity_first_signal':False}
            self.seen.add((symbol,kind));r=self.records.get(symbol,{})
            if not isinstance(r,dict):r={}
            value=r.get('mean_dollar_volume');days=r.get('valid_days')
            complete=days==20 and type(value) in (int,float) and math.isfinite(value) and value>=0 and self.failure is None
            if not complete:self.missing.add(symbol)
            return {'liquidity_first_signal':True,'signal_event_id':event_id,
                'adv20_usd':value if complete else None,'adv20_valid_days':days,
                'adv20_history_incomplete':not complete,'adv20_missing_symbols_seen':len(self.missing),
                'adv20_missing_history_scope_at_init':self.scope_missing_at_init,
                'adv20_file_sha256':self.pin,'adv20_first_session':self.first_session,
                'adv20_last_session':self.last_session,'adv20_load_error_type':self.failure,
                'liquidity_filter_applied':False}
    def snapshot(self):
        with self.lock:return {'first_signals':len(self.seen),'missing_symbols_seen':len(self.missing),
                              'scope_missing_at_init':self.scope_missing_at_init,
                              'file_error_type':self.failure,'last_session':self.last_session,'filter_applied':False}
