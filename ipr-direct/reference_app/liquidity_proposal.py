"""Review-only liquidity component. Not imported by the runtime or pipeline."""
from pathlib import Path
import collections,hashlib,json,math
PIN='855c87dd567e31695465fe8037c5c4214b7db3d620fabf26da2ab84cc442e869'
class LiquidityProposal:
    def __init__(self,*,enabled=False,threshold=None,path=None):
        if type(enabled) is not bool:raise TypeError('EXPLICIT_BOOLEAN_FLAG_REQUIRED')
        if enabled and (type(threshold) not in (int,float) or not math.isfinite(threshold) or threshold<=0):raise ValueError('USER_THRESHOLD_REQUIRED_NO_DEFAULT')
        self.enabled=enabled;self.threshold=threshold;self.records={};self.failure=None
        self.counters=collections.Counter();self._counted=set()
        if not enabled:return
        try:
            raw=Path(path or Path(__file__).parent/'research_liquidity/ADV20_20261016.json').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=PIN:raise ValueError('LIQUIDITY_FILE_SHA256_MISMATCH')
            x=json.loads(raw)
            if x['currency']!='USD' or x['required_sessions']!=20:raise ValueError('LIQUIDITY_FILE_SCHEMA')
            self.records=x['records']
        except Exception as exc:self.failure=str(exc)
    def _count(self,symbol,reason):
        key=(symbol,reason)
        if key not in self._counted:self._counted.add(key);self.counters[reason]+=1
    def evaluate(self,symbol):
        if not self.enabled:return {'eligible':True,'evaluation':'NOT_EVALUATED','reason':'LIQUIDITY_DISABLED'}
        if self.failure:
            self._count(symbol,'LIQUIDITY_FILE_INVALID');return {'eligible':False,'evaluation':'FAIL','reason':'LIQUIDITY_FILE_INVALID'}
        r=self.records.get(symbol)
        if not r or r['valid_days']!=20 or r['mean_dollar_volume'] is None:
            self._count(symbol,'LIQUIDITY_INCOMPLETE_HISTORY_PASSED');return {'eligible':True,'evaluation':'UNKNOWN_PASSED','reason':'LIQUIDITY_INCOMPLETE_HISTORY_PASSED'}
        blocked=r['mean_dollar_volume']<self.threshold
        reason='LIQUIDITY_BELOW_USER_THRESHOLD' if blocked else 'LIQUIDITY_PASSED'
        self._count(symbol,reason)
        return {'eligible':not blocked,'evaluation':'FAIL' if blocked else 'PASS','reason':reason,'mean_dollar_volume':r['mean_dollar_volume']}
