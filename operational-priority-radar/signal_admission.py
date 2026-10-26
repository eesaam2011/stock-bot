"""Fail-closed admission; does not grant market-data trust or send messages."""
import hashlib,json
from datetime import datetime,timedelta,timezone
from pathlib import Path

def utc(value):
    value=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError('UTC_REQUIRED')
    return value.astimezone(timezone.utc)

class SignalAdmission:
    # Proposal for review: new signal age, not an entry-price/trust/lease timeout.
    MAX_SIGNAL_AGE=timedelta(minutes=5)
    def __init__(self,session,schedule=None):
        self.session=session;self.schedule=schedule;self.counts={}
    @classmethod
    def from_environment(cls,session):
        import os
        p=os.environ.get('OPR_MARKET_SCHEDULE_FILE');digest=os.environ.get('OPR_MARKET_SCHEDULE_SHA256');inline=os.environ.get('OPR_MARKET_SCHEDULE_JSON')
        if not digest or (not inline and not p):return cls(session)
        if inline and p:raise ValueError('AMBIGUOUS_SCHEDULE_SOURCE')
        try:raw=inline.encode('utf-8') if inline else Path(p).read_bytes()
        except FileNotFoundError:return cls(session)
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SCHEDULE_HASH_MISMATCH')
        d=json.loads(raw)
        cal=json.loads((Path(__file__).parent/'frozen/CALENDAR.json').read_text())
        if d.get('session')!=session or session not in cal['sessions']:raise ValueError('SCHEDULE_SESSION_INVALID')
        from zoneinfo import ZoneInfo
        op=utc(d['open_utc']);cl=utc(d['close_utc']);ny=ZoneInfo('America/New_York')
        if op.astimezone(ny).strftime('%Y-%m-%d %H:%M')!=session+' 09:30' or cl<=op or cl-op>timedelta(hours=6,minutes=30):raise ValueError('SCHEDULE_HOURS_INVALID')
        # Exact early-close time must come from the reviewed Colab calendar export.
        return cls(session,d)
    def window_reason(self,now):
        now=utc(now)
        if not self.schedule:return 'CALENDAR_UNAVAILABLE'
        if now.date().isoformat()!=self.session:return 'SESSION_MISMATCH'
        if not utc(self.schedule['open_utc'])<=now<utc(self.schedule['close_utc']):return 'OUTSIDE_MARKET_HOURS'
        return None
    def reason(self,bar_end,now,bar_minutes=0,check_age=True):
        now=utc(now);end=utc(bar_end)
        reason=self.window_reason(now)
        if reason:return reason
        if end>now:return 'INCOMPLETE_BAR'
        if not utc(self.schedule['open_utc'])<end<=utc(self.schedule['close_utc']):return 'BAR_OUTSIDE_MARKET_HOURS'
        if end-timedelta(minutes=bar_minutes)<utc(self.schedule['open_utc']):return 'BAR_SPANS_MARKET_OPEN'
        if check_age and now-end>self.MAX_SIGNAL_AGE:return 'STALE'
        return None
    def pair_reason(self,e,b,now):
        # Structural/market/completion checks for BOTH; freshness only for the signal triggering the original decision-time contract.
        available=[(kind,row) for kind,row in (("E",e),("B",b)) if row is not None]
        for kind,row in available:
            reason=self.reason(row['bar_end_ts'],now,5 if kind=='E' else 1,check_age=False)
            if reason:return kind,reason,row
        if available:
            kind,row=max(available,key=lambda x:(utc(x[1]['decision_available_ts']),utc(x[1]['bar_end_ts'])))
            reason=self.reason(row['bar_end_ts'],now,5 if kind=='E' else 1)
            if reason:return kind,reason,row
        return None
    def record(self,reason,symbol,kind,end,now):
        key=kind+':'+reason;self.counts[key]=self.counts.get(key,0)+1
        # Counts every rejection; avoid one stdout write per historical bar.
        if self.counts[key]<=5:
            print(json.dumps({'stage':'SIGNAL_ADMISSION','reason':reason,'symbol':symbol,'kind':kind,'bar_end_ts':utc(end).isoformat(),'now':utc(now).isoformat(),'count':self.counts[key]},sort_keys=True),flush=True)

class BufferBudgetExceeded(RuntimeError):pass

class PackedHistory:
    """Internal exact tuple serialization; never unpickle external input."""
    def __init__(self,maxlen=60):
        from collections import deque
        self.rows=deque(maxlen=maxlen)
    def append(self,row):
        import pickle
        raw=pickle.dumps(row,protocol=4)
        if len(raw)>512:raise BufferBudgetExceeded("PACKED_BAR_OVER_512_BYTES")
        self.rows.append(raw)
    def __iter__(self):
        import pickle
        return (pickle.loads(row) for row in self.rows)
    def __len__(self):return len(self.rows)
