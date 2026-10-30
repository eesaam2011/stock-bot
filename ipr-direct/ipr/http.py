"""Small requests-compatible read transport, stdlib only; no redirects/retries/proxies."""
import json,urllib.request,urllib.error
from .api import NoRedirect
class Response:
 def __init__(self,status,raw):self.status_code=status;self.raw=raw
 def json(self):return json.loads(self.raw)
def request(url,method,headers,body,timeout):
 if not isinstance(url,str) or not url.startswith('https://'):raise ValueError('HTTPS_REQUIRED')
 req=urllib.request.Request(url,data=body,headers=headers,method=method)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 try:r=opener.open(req,timeout=timeout)
 except urllib.error.HTTPError as e:r=e
 with r:
  raw=r.read(32*1024*1024+1)
  if len(raw)>32*1024*1024:raise ValueError('RESPONSE_TOO_LARGE')
  return Response(r.status,raw)
def get(url,headers,timeout,allow_redirects=False):
 if allow_redirects:raise ValueError('REDIRECTS_FORBIDDEN')
 return request(url,'GET',headers,None,timeout)
def post(url,headers,json,timeout,allow_redirects=False):
 if allow_redirects:raise ValueError('REDIRECTS_FORBIDDEN')
 return request(url,'POST',{**headers,'Content-Type':'application/json'},__import__('json').dumps(json).encode(),timeout)
