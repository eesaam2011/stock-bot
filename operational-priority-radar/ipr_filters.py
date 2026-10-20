"""Isolated, opt-in nomination filter. Never mutates active trades or trust."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass,asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json, math, pathlib, re
from copy import deepcopy
from frozen_pins import PINS

UTC=timezone.utc
def utc(value):
    x=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('NAIVE_TIMESTAMP')
    return x.astimezone(UTC)

class FrozenFiles:
    def __init__(self,root):
        self.root=pathlib.Path(root);self.errors=[];self.data={}
        for name,pin in PINS.items():
            try:
                raw=(self.root/name).read_bytes()
                if sha256(raw).hexdigest()!=pin:raise ValueError('SHA256_MISMATCH')
                self.data[name]=raw
            except (OSError,ValueError) as exc:self.errors.append('FROZEN_FILE_INVALID:'+name+':'+type(exc).__name__)
        if not self.errors:
            try:
                self.activity=frozenset(self.data['ACTIVITY.txt'].decode().splitlines())
                self.manual=frozenset(self.data['ADR_MANUAL.txt'].decode().splitlines())
                self.rules=json.loads(self.data['NAME_RULES.json'])
                self.calendar=json.loads(self.data['CALENDAR.json'])
                self.day=json.loads(self.data['DAY_LIST.json'])
                assert len(self.activity)==656 and len(self.manual)==10
                assert self.rules['version']=='IPR-NAME-RULES-4.0.0-REVIEW'
                manual_data=json.loads(self.data['TYPE_MANUAL.json'])
                assert manual_data['version']=='IPR-TYPE-MANUAL-20261020-1'
                self.type_manual={r['symbol']:r for r in manual_data['records']}
                assert len(self.type_manual)==25 and len(manual_data['records'])==25
                assert all(isinstance(r['asset_id'],str) and r['asset_id'] and isinstance(r['name'],str) and r['name'] for r in self.type_manual.values())
                self.patterns=[(r['rule_id'],r['type'],re.compile(r['regex'],re.I)) for r in self.rules['rules']]
                dates=self.calendar['sessions'];assert dates==sorted(set(dates))
            except (ValueError,KeyError,AssertionError,re.error):self.errors.append('FROZEN_SCHEMA_INVALID')
        # Release duplicate raw blobs, especially the full raw-asset body.
        self.data.clear()
    @property
    def ok(self):return not self.errors

def classify_name(name,patterns):
    if not isinstance(name,str) or not name.strip():return 'TYPE_UNKNOWN',(),()
    rx=lambda p:bool(re.search(p,name,re.I))
    hits=[(i,t) for i,t,p in patterns if p.search(name)];suppressed=[]
    def remove(i,reason):
        nonlocal hits
        if any(x[0]==i for x in hits):suppressed.append((i,reason));hits=[h for h in hits if h[0]!=i]
    if rx(r'\b(?:common|corporate)\s+units?\b') and not rx(r'\b(?:acquisition|SPAC)\b'):remove('N03','OPERATING_UNITS')
    if rx(r'\bcommon\s+(?:stock|shares)\b'):
        for i in ('N04','N05','N08','N09','N12','N13'):remove(i,'COMMON_STOCK_OR_SHARES')
    if rx(r'^\s*Invesco\s+(?:LTD\.?|Limited|Inc\.?)\s*$'):remove('N08','ISSUER_COMPANY')
    if rx(r'\b(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited)\b') and not rx(r'\b(?:Funds?|ETFs?|ETNs?)\b') and not (rx(r'\bTrust\b') and rx(r'\b(?:Shares|Series|Portfolio)\b')):remove('N08','OPERATING_ISSUER_CONTEXT')
    if rx(r'\b(?:Mortgage|Realty|REIT|Bank|Bancorp|Properties|Property)\b|\breal\s+estate\b'):remove('N11','PROPERTY_OR_BANK')
    if rx(r'\bpreferred\b|\bpreference\s+shares?\b'):remove('N11','PREFERRED_UNDERLYING_TRUST')
    if any(i=='N10' for i,t in hits):remove('N03','CRYPTO_PRODUCT_UNIT')
    types={t for i,t in hits}
    if 'UNIT' in types:
        for i in ('N01','N02','N04'):remove(i,'UNIT_COMPONENT')
    if 'ADR' in types:remove('N02','ADR_ENTITLEMENT')
    if 'ETF_ETN_FUND' in types:remove('N05','FUND_HOLDING')
    if types & {'ETF_ETN_FUND','WARRANT','UNIT'}:remove('N07','ADR_UNDERLYING')
    if any(t=='ADR' for i,t in hits):remove('N04','ADR_PREFERRED_UNDERLYING')
    return (hits[0][1] if hits else 'TYPE_UNKNOWN'),tuple(hits),tuple(suppressed)

@dataclass(frozen=True)
class Decision:
    symbol:str
    eligible_for_new_nomination:bool
    reasons:tuple[str,...]
    evaluations:dict
    diagnostics:dict

class Counters:
    """Session-bounded dedup. No unbounded per-tick decision history."""
    def __init__(self,max_decisions=100000):
        self.max_decisions=max_decisions;self.decisions={};self.requests={}
        self.per_reason=Counter();self.blocked_source=Counter();self.request_failures=Counter()
        self.unknown_passed=set();self.excluded=set();self.source_symbols=set()
        self.limits=Counter()
    def request_failure(self,request_id,reason):
        if request_id in self.requests:return
        if len(self.requests)>=self.max_decisions:self.limits['request_dedup_capacity']+=1;return
        self.requests[request_id]=reason;self.request_failures[reason]+=1
    def record(self,decision_id,d):
        key=(decision_id,d.symbol)
        if key in self.decisions:
            if self.decisions[key]!=d.reasons:raise ValueError('DECISION_ID_REUSED_WITH_CHANGED_RESULT')
            return
        if len(self.decisions)>=self.max_decisions:
            self.limits['decision_audit_capacity']+=1
            raise ValueError('DECISION_AUDIT_CAPACITY')
        self.decisions[key]=d.reasons
        if d.diagnostics.get('primary_type')=='TYPE_UNKNOWN':self.unknown_passed.add(d.symbol)
        for reason in d.reasons:self.per_reason[reason]+=1
        if d.reasons:self.excluded.add(d.symbol)
        for reason in ('TRADE_TIMEOUT','TRADE_HTTP_429','TRADE_HTTP_5XX','PRICE_SOURCE_UNAVAILABLE'):
            if reason in d.reasons:self.blocked_source[reason]+=1;self.source_symbols.add(d.symbol);break
    def snapshot(self):
        return {'reason_decisions':dict(self.per_reason),'excluded_unique':len(self.excluded),'unknown_type_unique':len(self.unknown_passed),'blocked_source_decisions':dict(self.blocked_source),'request_failures':dict(self.request_failures),'blocked_source_unique_symbols':len(self.source_symbols),'decision_count':len(self.decisions),'capacity_blocks':dict(self.limits)}

class FilterEngine:
    def __init__(self,frozen_root,*,enabled=False,clock=None,counters=None,audit_sink=None,clock_uncertainty=None):
        if type(enabled) is not bool:raise TypeError('EXPLICIT_BOOLEAN_FLAG_REQUIRED')
        if enabled and audit_sink is None:raise ValueError('ENABLED_FILTER_REQUIRES_AUDIT_SINK')
        self.enabled=enabled;self.frozen=FrozenFiles(frozen_root)
        self.audit_sink=audit_sink
        self.clock_uncertainty=clock_uncertainty
        self.clock=clock or (lambda:datetime.now(UTC));self.counters=counters or Counters()
        self._static={}
        if self.frozen.ok:
            for sym,asset in self.frozen.day['assets'].items():
                self._static[sym]=self._classify_asset(asset)
    @property
    def integrity_ok(self):return self.frozen.ok
    def _classify_asset(self,asset):
        return classify_name(asset.get('name'),self.frozen.patterns)
    def day_state(self,current_session,refresh_error=None):
        try:
            sessions=self.frozen.calendar['sessions'];built=self.frozen.day['session']
            if current_session not in sessions or built not in sessions:raise ValueError()
            age=sessions.index(current_session)-sessions.index(built)
            return {'age_sessions':age,'refresh_error':refresh_error,'allowed':0<=age<=1,'state':'FRESH' if age==0 else 'STALE_WITHIN_ONE_SESSION' if age==1 else 'LIST_TOO_OLD','calendar_version':self.frozen.calendar['version']}
        except (AttributeError,KeyError,ValueError):return {'age_sessions':None,'refresh_error':refresh_error,'allowed':False,'state':'CALENDAR_UNAVAILABLE'}
    def static_reasons(self,symbol,asset=None):
        f=self.frozen;asset=f.day['assets'].get(symbol) if asset is None else asset;reasons=set()
        if asset is None:return {'ASSET_STATUS_UNKNOWN'},'TYPE_UNKNOWN',(),()
        if symbol in f.activity:reasons.add('ACTIVITY_DENYLIST_EXACT_MATCH')
        if symbol in f.manual:reasons.add('TYPE_ADR_MANUAL')
        exception=f.type_manual.get(symbol)
        if exception:
            identity_matches=asset.get('id')==exception['asset_id'] and asset.get('name')==exception['name']
            reasons.add('TYPE_MANUAL_REVIEWED_EXCEPTION' if identity_matches else 'TYPE_MANUAL_IDENTITY_MISMATCH')
        for field,value,reason in [('exchange','OTC','MARKET_OTC'),('status','active','ASSET_INACTIVE'),('tradable',True,'ASSET_NOT_TRADABLE')]:
            v=asset.get(field)
            if v is None or (field=='exchange' and v not in {'NYSE','NASDAQ','ARCA','BATS','AMEX','OTC'}):reasons.add('ASSET_STATUS_UNKNOWN')
            elif field=='exchange' and v==value:reasons.add(reason)
            elif field=='status' and v!=value:reasons.add(reason)
            elif field=='tradable' and v is not True:reasons.add(reason)
        typ,hits,suppressed=self._classify_asset(asset) if asset is not f.day['assets'].get(symbol) else self._static[symbol]
        reasons.update('TYPE_'+t for i,t in hits)
        return reasons,typ,hits,suppressed
    def evaluate(self,symbol,decision_id,*,session,trust_state,trade=None,asset=None,now=None,refresh_error=None,clock_uncertainty=None,final_candidate=True,quote=None):
        # This API has no active-trade, subscription, Redis or trust-state mutation.
        evaluations={x:'NOT_EVALUATED' for x in ('integrity','type','activity','asset','calendar','price','trust')}
        reasons=set();diag={'enabled':self.enabled,'pins':PINS,'quote':quote,'final_candidate':final_candidate}
        if isinstance(quote,dict):
            try:diag['quote_age_at_decision']=(utc(now or self.clock())-utc(quote['event_time'])).total_seconds()
            except (KeyError,ValueError,TypeError):diag['quote_age_at_decision']=None
        if not self.integrity_ok:
            evaluations['integrity']='FAIL';reasons.update(self.frozen.errors)
        else:evaluations['integrity']='PASS'
        if not reasons and not self.enabled:
            return Decision(symbol,True,(),evaluations,diag)
        if not reasons:
            sr,typ,hits,suppressed=self.static_reasons(symbol,asset);reasons.update(sr)
            diag.update(primary_type=typ,matched_rules=hits,suppressed_rules=suppressed)
            evaluations.update(type='FAIL' if any(r.startswith('TYPE_') for r in sr) else 'PASS',activity='FAIL' if 'ACTIVITY_DENYLIST_EXACT_MATCH' in sr else 'PASS',asset='FAIL' if any(r.startswith(('ASSET_','MARKET_')) for r in sr) else 'PASS')
            day=self.day_state(session,refresh_error);diag['day']=day;evaluations['calendar']='PASS' if day['allowed'] else 'FAIL'
            if not day['allowed']:reasons.add(day['state'])
            evaluations['trust']='PASS' if trust_state=='LIVE_TRUSTED' else 'FAIL'
            if trust_state!='LIVE_TRUSTED':reasons.add('NOT_LIVE_TRUSTED')
            if not final_candidate:reasons.add('NOT_FINAL_CANDIDATE')
            # Avoid a price request/check for statically blocked or untrusted symbols.
            if not reasons:
                uncertainty=self.clock_uncertainty if clock_uncertainty is None else clock_uncertainty
                pr,pdiag=self.check_trade(trade,now or self.clock(),uncertainty)
                reasons.update(pr);diag.update(pdiag);evaluations['price']='FAIL' if pr else 'PASS'
        d=Decision(symbol,not reasons,tuple(sorted(reasons)),evaluations,diag)
        if self.audit_sink is not None:
            try:self.audit_sink({'decision_id':decision_id,'decision_utc':utc(now or self.clock()).isoformat(),**asdict(d)})
            except Exception:d=Decision(symbol,False,tuple(sorted(set(d.reasons)|{'AUDIT_WRITE_FAILED'})),evaluations,diag)
        try:self.counters.record(decision_id,d)
        except ValueError as exc:
            return Decision(symbol,False,tuple(sorted(set(d.reasons)|{str(exc)})),evaluations,diag)
        return d
    @staticmethod
    def check_trade(trade,now,uncertainty=0.0):
        reasons=set();diag={'trade':trade}
        if not isinstance(trade,dict):return {'PRICE_MISSING'},diag
        if trade.get('error'):
            error=trade['error'] if trade['error'] in ('TRADE_TIMEOUT','TRADE_HTTP_429','TRADE_HTTP_5XX','PRICE_SOURCE_UNAVAILABLE') else 'PRICE_SOURCE_UNAVAILABLE'
            return {error},diag
        if trade.get('feed')!='sip' or trade.get('currency')!='USD':reasons.add('PRICE_FEED_OR_CURRENCY_INVALID')
        try:
            p=Decimal(str(trade.get('price')))
            if not p.is_finite() or p<=0:raise ValueError()
            if p>Decimal('50'):reasons.add('PRICE_ABOVE_50')
        except (InvalidOperation,ValueError,TypeError):reasons.add('PRICE_INVALID')
        try:
            age=(utc(now)-utc(trade['event_time'])).total_seconds();diag['trade_age_at_decision']=age
            if age<0:reasons.add('FUTURE_TIMESTAMP')
            elif age>60:reasons.add('PRICE_STALE')
            if not isinstance(uncertainty,(int,float)) or not math.isfinite(uncertainty) or uncertainty<0 or uncertainty>1:reasons.add('CLOCK_UNCERTAIN')
            elif age<=60<age+uncertainty:reasons.add('CLOCK_UNCERTAIN')
        except (ValueError,KeyError,TypeError):reasons.add('PRICE_TIMESTAMP_INVALID')
        diag['clock_uncertainty_seconds']=uncertainty
        return reasons,diag

def effective_subscriptions(day_symbols,active_symbols,recovery_symbols=()):
    return frozenset(day_symbols)|frozenset(active_symbols)|frozenset(recovery_symbols)

def activity_review_status(version_date,last_review,now):
    day=utc(now).date();version_age=(day-utc(version_date).date()).days
    review_age=None if last_review is None else (day-utc(last_review).date()).days
    return {'version_age_days':version_age,'days_since_review':review_age,'review_reminder':review_age is None or review_age>35,'blocks_new_nomination':False}

class JsonlAuditSink:
    def __init__(self,path):self.path=pathlib.Path(path)
    def __call__(self,event):
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False,sort_keys=True,default=str)+'\n');stream.flush()

class PreparedGate:
    """Hot-path cache only. Prepare prices outside the message processing lock.

    Cache key is symbol + confluence trigger, not a reusable authorization.
    Every consume rechecks age, status, list and current trust; no stored PASS.
    """
    def __init__(self,engine,session,trust,asset_status,*,max_prepared=256):
        self.engine=engine;self.session=session;self.trust=trust;self.asset_status=asset_status
        self.max_prepared=max_prepared;self.prepared={};self.last_decision=None;self.sequence=0
    @property
    def enabled(self):return self.engine.enabled
    @property
    def integrity_ok(self):return self.engine.integrity_ok
    def put(self,symbol,trigger,trade):
        if (symbol,trigger) not in self.prepared and len(self.prepared)>=self.max_prepared:
            self.engine.counters.limits['prepared_capacity']+=1
            raise ValueError('PREPARED_CAPACITY')
        self.prepared[(symbol,trigger)]=dict(trade)
    def consume(self,symbol,trigger):
        self.sequence+=1
        trade=self.prepared.get((symbol,trigger));quote=None
        if trade is not None:
            trade=dict(trade);comparison=trade.pop('quote_comparison',None)
            quote=deepcopy(comparison.get('value')) if isinstance(comparison,dict) else None
        self.last_decision=self.engine.evaluate(symbol,str(self.sequence),session=self.session,trust_state=self.trust(),trade=trade,asset=self.asset_status(symbol),quote=quote)
        return self.last_decision.eligible_for_new_nomination
    def expire(self,symbol,trigger):self.prepared.pop((symbol,trigger),None)
    def admit_or_request(self,symbol,trigger,**sip_context):return self.consume(symbol,trigger)

def bind_isolated_filters(pipeline,*,frozen_root=None,enabled=False,audit_sink=None):
    """Composition entrypoint for the isolated shadow copy only. No env flag."""
    if pipeline.shadow is not True:raise ValueError('ISOLATED_SHADOW_ONLY')
    froot=frozen_root or pathlib.Path(__file__).parent/'frozen'
    e=FilterEngine(froot,enabled=enabled,audit_sink=audit_sink)
    return PreparedGate(e,pipeline.session,
        lambda:'LIVE_TRUSTED' if pipeline._decision_gate_open() else 'STREAM_UNTRUSTED',
        lambda symbol:e.frozen.day['assets'].get(symbol) if e.integrity_ok else None)
