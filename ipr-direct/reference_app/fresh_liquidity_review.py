"""Not imported by runtime. No default threshold; Colab data + pin are mandatory."""
import json,hashlib,math
from pathlib import Path
class FreshLiquidityReview:
 def __init__(self,*,enabled=False,threshold=None,path=None,pin=None,expected_last_session=None,expected_sessions=None):
  self.enabled=enabled;self.threshold=threshold;self.records={};self.missing=set()
  if not enabled:return
  if type(threshold)not in(int,float)or not math.isfinite(threshold)or threshold<=0:raise ValueError('USER_THRESHOLD_REQUIRED')
  if not path or not pin or not expected_last_session or not expected_sessions:raise ValueError('FRESH_ADV20_BINDING_REQUIRED')
  raw=Path(path).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=pin:raise ValueError('ADV20_PIN_MISMATCH')
  d=json.loads(raw)
  if d.get('required_sessions')!=20 or d.get('currency')!='USD' or d.get('last_session')!=expected_last_session:raise ValueError('ADV20_FRESHNESS_REQUIRED')
  if len(d.get('sessions',[]))!=20 or len(set(d['sessions']))!=20 or d['sessions']!=sorted(d['sessions']) or d['sessions'][-1]!=expected_last_session:raise ValueError('ADV20_SESSION_SET_INVALID')
  if d['sessions']!=list(expected_sessions):raise ValueError('ADV20_CALENDAR_SESSIONS_MISMATCH')
  self.records=d['records']
 def evaluate(self,symbol):
  if not self.enabled:return {'eligible':True,'reason':'DISABLED'}
  r=self.records.get(symbol,{});v=r.get('mean_dollar_volume')
  if r.get('valid_days')!=20 or type(v)not in(int,float)or not math.isfinite(v)or v<0:
   self.missing.add(symbol);return {'eligible':True,'reason':'INCOMPLETE_HISTORY_PASSED','missing_count':len(self.missing)}
  return {'eligible':v>=self.threshold,'reason':'PASS'if v>=self.threshold else'BELOW_USER_THRESHOLD','adv20_usd':v}
