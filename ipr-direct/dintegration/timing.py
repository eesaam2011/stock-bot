import time,json,functools
from contextlib import contextmanager
@contextmanager
def phase(name):
 start=time.monotonic();ok=False
 try:yield;ok=True
 finally:
  try:print(json.dumps({'stage':'PHASE_TIMING','phase':name,'seconds':time.monotonic()-start,'success':ok}),flush=True)
  except Exception:pass
def timed(name):
 def wrap(fn):
  @functools.wraps(fn)
  def call(*a,**k):
   with phase(name):return fn(*a,**k)
  return call
 return wrap
