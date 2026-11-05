"""Offline-only full verifier for an explicit JSON export. No Redis connection."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent;sys.path[:0]=[str(ROOT),str(ROOT/'reference_app')];sys.dont_write_bytecode=True
from r4a.durable import RedisState,READ_LUA
from r9core.chain import ChainState,StateConflict
from r9core.audit import full_verify
class ExportReader:
 def __init__(self,data):self.data=data
 def get(self,k):return self.data.get('strings',{}).get(k)
 def hget(self,k,f):return self.data.get('hashes',{}).get(k,{}).get(str(f))
 def mget(self,ks):return [self.get(k) for k in ks]
 def eval(self,script,n,*args):
  keys=args[:n]
  if script==READ_LUA:
   seq=self.get(keys[0]);raw=self.get(keys[1]);rows=self.data.get('hashes',{}).get(keys[2])
   if raw is None and seq is None and rows is None:return []
   if raw is None or seq is None:return ['CORRUPT']
   if int(seq)<1 or len(rows or {})!=int(seq) or (rows or {}).get(str(seq))!=raw:return ['CORRUPT']
   return [seq,raw]
  if script=="return redis.call('EXISTS',KEYS[1])":return int(keys[0] in self.data.get('strings',{}) or keys[0] in self.data.get('hashes',{}))
  if script=="return redis.call('HLEN',KEYS[1])":return len(self.data.get('hashes',{}).get(keys[0],{}))
  raise StateConflict('OFFLINE_READER_FORBIDS_SCRIPT')
def main():
 p=argparse.ArgumentParser();p.add_argument('--format',required=True,choices=['legacy','chain2']);p.add_argument('--export',required=True);a=p.parse_args()
 data=json.loads(Path(a.export).read_text());r=ExportReader(data)
 if a.format=='legacy':
  s=RedisState(r,data['session'],data['root_hash'],'read_only',0);s.restore();out={'format':'legacy','records':s.seq,'full_chain_verified':True}
 else:
  s=ChainState(r,data['session'],data['root_hash'],'readonly',0);out={'format':'chain2',**full_verify(s)}
 print(json.dumps(out,sort_keys=True))
if __name__=='__main__':main()
