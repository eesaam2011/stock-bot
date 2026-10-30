"""Operational safety bounds. Test fixture RSS/deadline limits are not runtime guards."""
from restart_budget import UNTRUSTED_AFTER_ACK_SEC,MAX_RESTARTS
MIB=1024*1024
class SafetyStop(RuntimeError):pass
class AckTrustDeadline:
 def __init__(self,clock):self.clock=clock;self.armed=None
 def ack(self):
  if self.armed is None:self.armed=self.clock()
 def tick(self,trusted):
  if trusted:self.armed=None
  elif self.armed is not None and self.clock()-self.armed>=UNTRUSTED_AFTER_ACK_SEC:raise SafetyStop('WS_ACK_UNTRUSTED_TIMEOUT')
 # Disconnect never resets an armed deadline.
class MemoryBounds:
 def check(self,sample):
  if sample.get('limit_bytes')!=512*MIB:raise SafetyStop('CGROUP_LIMIT_NOT_512MIB')
  if not isinstance(sample.get('anon_bytes'),int) or sample['anon_bytes']<0:raise SafetyStop('CGROUP_ANON_UNREADABLE')
  if sample['anon_bytes']>=410*MIB:raise SafetyStop('CGROUP_ANON_LIMIT_410MIB')
  if not isinstance(sample.get('current_bytes'),int) or sample['current_bytes']<0:raise SafetyStop('CGROUP_CURRENT_UNREADABLE')
  if sample['current_bytes']>=512*MIB:raise SafetyStop('CGROUP_MEMORY_LIMIT_512MIB')
def cgroup_sample():
 from pathlib import Path
 root=Path('/sys/fs/cgroup')
 stat=dict(line.split() for line in (root/'memory.stat').read_text().splitlines())
 return {'limit_bytes':int((root/'memory.max').read_text()),'current_bytes':int((root/'memory.current').read_text()),'anon_bytes':int(stat['anon'])}
