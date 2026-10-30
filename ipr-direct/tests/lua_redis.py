"""Networkless Redis subset: actual Lua scripts run in installed liblua5.4.
Models atomic scripts, Redis types and expiry. NOT a Redis server/version test.
"""
import ctypes,ctypes.util,json,threading,fnmatch
class SimRedisError(RuntimeError):pass

def lua(x):
 if x is None:return 'nil'
 if isinstance(x,bool):return 'true' if x else 'false'
 if isinstance(x,(int,float)):return repr(x)
 if isinstance(x,str):return json.dumps(x,ensure_ascii=True).replace('\\u','\\u') if all(ord(c)<128 for c in x) else 'string.char('+','.join(str(n) for n in x.encode())+')'
 if isinstance(x,(list,tuple)):return '{'+','.join(lua(v) for v in x)+'}'
 return '{'+','.join('['+lua(str(k))+']='+lua(v) for k,v in x.items())+'}'

PRELUDE=r'''
local function purge(k)
 local e=state.exp[k];if e and e<=now then state.values[k]=nil;state.hashes[k]=nil;state.exp[k]=nil end
end
redis={}
function redis.call(cmd,...)
 local a={...};cmd=string.upper(cmd);local k=tostring(a[1]);purge(k)
 local v=state.values;local h=state.hashes;local e=state.exp
 if cmd=='TYPE' then return {ok=v[k]~=nil and 'string' or h[k]~=nil and 'hash' or 'none'} end
 if cmd=='EXISTS' then return (v[k]~=nil or h[k]~=nil) and 1 or 0 end
 if cmd=='TIME' then return {tostring(math.floor(now)),'0'} end
 if cmd=='DEL' then local exists=v[k]~=nil or h[k]~=nil;v[k]=nil;h[k]=nil;e[k]=nil;return exists and 1 or 0 end
 if cmd=='EXPIRE' then if v[k]==nil and h[k]==nil then return 0 end;e[k]=now+tonumber(a[2]);return 1 end
 if cmd=='GET' then if h[k]~=nil then error('WRONGTYPE') end;return v[k] or false end
 if cmd=='SET' then
  local nx=false;local ttl=nil
  for i=3,#a do if a[i]=='NX' then nx=true elseif a[i]=='EX' then ttl=tonumber(a[i+1]) end end
  if nx and (v[k]~=nil or h[k]~=nil) then return false end
  v[k]=tostring(a[2]);h[k]=nil;e[k]=ttl and now+ttl or nil;return {ok='OK'}
 end
 if cmd=='INCR' then
  if h[k] then error('WRONGTYPE') end
  local n=tonumber(v[k] or '0');if not n or n~=math.floor(n) then error('NOT_INTEGER') end
  v[k]=string.format('%.0f',n+1);return n+1
 end
 if cmd=='HGET' or cmd=='HSET' or cmd=='HLEN' then
  if v[k]~=nil then error('WRONGTYPE') end
  if cmd=='HGET' then return h[k] and h[k][tostring(a[2])] or false end
  if cmd=='HLEN' then local n=0;for _ in pairs(h[k] or {}) do n=n+1 end;return n end
  h[k]=h[k] or {};local n=0
  for i=2,#a,2 do local f=tostring(a[i]);if h[k][f]==nil then n=n+1 end;h[k][f]=tostring(a[i+1]) end;return n
 end
 error('UNSUPPORTED_REDIS_COMMAND_'..cmd)
end
local function quote(s)
 return '"'..s:gsub('[%z\1-\31\\"]',function(c)
  if c=='"' then return '\\"' elseif c=='\\' then return '\\\\' else return string.format('\\u%04x',string.byte(c)) end
 end)..'"'
end
function encode(x)
 local t=type(x)
 if t=='nil' then return 'null' elseif t=='boolean' then return x and 'true' or 'false' elseif t=='number' then return tostring(x) elseif t=='string' then return quote(x) end
 local n=0;local array=true
 for k in pairs(x) do n=n+1;if type(k)~='number' then array=false end end
 local a={}
 if array then for i=1,n do a[#a+1]=encode(x[i]) end;return '['..table.concat(a,',')..']' end
 for k,v in pairs(x) do a[#a+1]=quote(tostring(k))..':'..encode(v) end
 return '{'..table.concat(a,',')..'}'
end
'''
class LuaRedis:
 def __init__(self,now=1793194200):
  name=ctypes.util.find_library('lua5.4')
  if not name:raise RuntimeError('OFFLINE_TEST_DEPENDENCY_LIBLUA5_4_MISSING')
  self.lib=ctypes.CDLL(name);L=self.lib
  L.luaL_newstate.restype=ctypes.c_void_p;L.luaL_openlibs.argtypes=[ctypes.c_void_p]
  L.luaL_loadstring.argtypes=[ctypes.c_void_p,ctypes.c_char_p];L.luaL_loadstring.restype=ctypes.c_int
  L.lua_pcallk.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_longlong,ctypes.c_void_p];L.lua_pcallk.restype=ctypes.c_int
  L.lua_tolstring.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_size_t)];L.lua_tolstring.restype=ctypes.c_void_p
  L.lua_close.argtypes=[ctypes.c_void_p]
  self.state={'values':{},'hashes':{},'exp':{}};self.now=now;self.lock=threading.RLock();self.available=True;self.lose_reply=False
 def _run(self,s):
  L=self.lib;p=L.luaL_newstate();L.luaL_openlibs(p)
  try:
   if L.luaL_loadstring(p,s.encode()) or L.lua_pcallk(p,0,1,0,0,None):
    n=ctypes.c_size_t();v=L.lua_tolstring(p,-1,ctypes.byref(n));raise SimRedisError(ctypes.string_at(v,n.value).decode())
   n=ctypes.c_size_t();v=L.lua_tolstring(p,-1,ctypes.byref(n));return json.loads(ctypes.string_at(v,n.value))
  finally:L.lua_close(p)
 def eval(self,script,nkeys,*args):
  with self.lock:
   if not self.available:raise ConnectionError('SIM_REDIS_UNAVAILABLE')
   code='state='+lua(self.state)+';now='+str(self.now)+';KEYS='+lua(list(args[:nkeys]))+';ARGV='+lua([str(x) for x in args[nkeys:]])+';\n'+PRELUDE+'\nlocal ok,result=pcall(function()\n'+script+'\nend);return encode({ok=ok,result=result,state=state})'
   out=self._run(code);self.state=out['state']
   for k in ('values','hashes','exp'):
    if self.state[k]==[]:self.state[k]={}
   if self.lose_reply:self.lose_reply=False;raise TimeoutError('SIM_AMBIGUOUS_REPLY_AFTER_COMMIT')
   if not out['ok']:raise SimRedisError(str(out['result']))
   return out['result']
 def get(self,k):
  v=self.eval("return redis.call('GET',KEYS[1])",1,k);return None if v is False else v
 def hget(self,k,f):
  v=self.eval("return redis.call('HGET',KEYS[1],ARGV[1])",1,k,f);return None if v is False else v
 def set(self,k,v,nx=False,ex=None):
  args=['SET',k,str(v)]+(['NX'] if nx else [])+(['EX',ex] if ex else [])
  r=self.eval('return redis.call('+','.join(lua(x) for x in args)+')',0);return bool(r)
 def mget(self,keys):return [self.get(k) for k in keys]
 def scan(self,cursor=0,match='*',count=500):
  with self.lock:
   keys=sorted(set(self.state['values'])|set(self.state['hashes']))
   keys=[k for k in keys if fnmatch.fnmatchcase(k,match) and self.eval("return redis.call('EXISTS',KEYS[1])",1,k)]
   start=int(cursor);end=min(start+count,len(keys));return (end if end<len(keys) else 0,keys[start:end])
 def time(self):
  if not self.available:raise ConnectionError('SIM_REDIS_UNAVAILABLE')
  return int(self.now),0
