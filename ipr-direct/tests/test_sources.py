import unittest,json,ast,socket,hashlib
from pathlib import Path
from datetime import date
from ipr.connectors import FloatReader,NewsReader

def blocked(*a,**k):raise AssertionError('NETWORK_FORBIDDEN')
socket.create_connection=blocked;socket.getaddrinfo=blocked;socket.socket.connect=blocked
NOW=1793365200
class Response:
 def __init__(self,data,status=200):self.data=data;self.status_code=status
 def json(self):return self.data
class FloatTests(unittest.TestCase):
 def reader(self,rows):
  calls=[]
  def get(url,**kw):calls.append((url,kw));return Response({'files':{'float_cache.json':{'content':json.dumps(rows)}}})
  r=FloatReader({'GIST_ID':'fixture_id','GITHUB_TOKEN':'fixture_credential'},get);return r,calls
 def test_actual_gist_protocol_and_preserved_date(self):
  r,c=self.reader({'ABC':{'float':123,'updated':'2026-10-26'}});self.assertTrue(r.refresh());self.assertEqual(r.value('abc',date(2026,10,30))['float'],123)
  self.assertEqual(c[0][0],'https://api.github.com/gists/fixture_id');self.assertEqual(c[0][1]['timeout'],10);self.assertFalse(c[0][1]['allow_redirects'])
 def test_zero_unknown_not_small(self):
  r,_=self.reader({'X':{'float':0.0,'updated':'2026-10-30'}});r.refresh();self.assertEqual(r.value('X',date(2026,10,30))['status'],'UNKNOWN')
 def test_exact_seven_days_valid(self):
  r,_=self.reader({'X':{'float':1,'updated':'2026-10-23'}});r.refresh();self.assertEqual(r.value('X',date(2026,10,30))['status'],'VALID')
 def test_old_future_missing_unknown(self):
  for row in [{'float':1,'updated':'2026-10-22'},{'float':1,'updated':'2026-10-31'},{'float':1},{'float':float('nan'),'updated':'2026-10-30'}]:
   r,_=self.reader({'X':row});r.refresh();self.assertEqual(r.value('X',date(2026,10,30))['cause'],'UNKNOWN_NOT_INFERRED')
 def test_missing_config_never_requests(self):
  r=FloatReader({},blocked);self.assertFalse(r.refresh())
 def test_failed_fetch_clears_old_value(self):
  r,_=self.reader({'X':{'float':1,'updated':'2026-10-30'}});r.refresh();r.get=lambda *a,**k:Response({},500);self.assertFalse(r.refresh());self.assertFalse(r.rows)
 def test_truncation_not_silently_used(self):
  r,_=self.reader({});r.get=lambda *a,**k:Response({'files':{'float_cache.json':{'content':'{}','truncated':True}}});self.assertFalse(r.refresh());self.assertEqual(r.reason,'GIST_CONTENT_TRUNCATED')
class NewsTests(unittest.TestCase):
 def setup_reader(self,row):
  calls=[]
  def post(url,**kw):calls.append(kw['json']);return Response({'result':json.dumps(row) if row is not None else None})
  r=NewsReader({'UPSTASH_REDIS_REST_URL':'https://fixture.invalid','UPSTASH_REDIS_REST_TOKEN':'fixture_credential'},post,clock=lambda:NOW);return r,calls
 def record(self,**kw):return dict(schema_version=2,producer='elite_catalyst_radar',symbol='ABC',checked_at=NOW-30,updated_at=NOW,expires_at=0,weekend_record=False,articles=[],analysis={'positive':True,'bonus':5,'serious_negative':False},**kw)
 def test_real_key_HGET_only_and_fresh_positive(self):
  r,c=self.setup_reader(self.record());x=r.read('abc');self.assertTrue(x['positive']);self.assertFalse(x['stale']);self.assertEqual(c,[['HGET','market_radar:news','ABC']])
 def test_missing_is_zero_not_exclusion(self):
  r,c=self.setup_reader(None);x=r.read('ABC');self.assertEqual(x['bonus'],0);self.assertEqual(x['status'],'shared_cache_missing')
 def test_stale_positive_zero_after_hour(self):
  row=self.record();row['checked_at']=NOW-3601;r,c=self.setup_reader(row);x=r.read('ABC');self.assertFalse(x['positive']);self.assertEqual(x['bonus'],0)
 def test_exact_one_hour_not_stale_original_boundary(self):
  row=self.record();row['checked_at']=NOW-3600;r,c=self.setup_reader(row);self.assertFalse(r.read('ABC')['stale'])
 def test_original_negative_flag_retained_without_IPR_gate(self):
  row=self.record();row['checked_at']=NOW-3601;row['analysis']={'positive':False,'serious_negative':True,'bonus':0};r,c=self.setup_reader(row);self.assertTrue(r.read('ABC')['serious_negative'])
 def test_future_record_original_consumer_does_not_reject(self):
  # Evidence of an original limitation, not claimed session validation.
  row=self.record();row['checked_at']=NOW+60;r,c=self.setup_reader(row);self.assertFalse(r.read('ABC')['stale'])
 def test_weekend_expires_at_not_enforced_by_original_consumer(self):
  row=self.record();row.update(weekend_record=True,expires_at=NOW-1);r,c=self.setup_reader(row);self.assertFalse(r.read('ABC')['stale'])
 def test_transport_error_does_not_leak_or_raise(self):
  r,c=self.setup_reader(None);r.post=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('fixture_sensitive_exception'));self.assertEqual(r.read('ABC')['status'],'shared_cache_missing')
 def test_no_source_config_no_call(self):
  self.assertEqual(NewsReader({},blocked).read('ABC')['bonus'],0)
 def test_old_articles_removed_by_original_12h_filter(self):
  row=self.record();row['articles']=[{'headline':'old','datetime':NOW-43201},{'headline':'fresh','datetime':NOW-1}];r,c=self.setup_reader(row);self.assertEqual(len(r.read('ABC') and r.raw('ABC')['articles']),2)
  from ipr import original_news_consumer as o
  import types
  ctx=dict(vars(o));ctx['time']=type('Clock',(),{'time':staticmethod(lambda:NOW)})
  fn=types.FunctionType(o.filter_recent_news_articles.__code__,ctx);self.assertEqual(len(fn(row['articles'])),1)
if __name__=='__main__':unittest.main(verbosity=2)
