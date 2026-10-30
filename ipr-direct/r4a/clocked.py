"""Only this facade supplies plan/ACK/tick timestamps; never accepts wall time."""
import math,time
from dreview.core import Blocked
class ClockedControl:
 def __init__(self,controller,clock=time.monotonic):
  self.c=controller;self.clock=clock;self.last=None
 def now(self):
  n=self.clock()
  if not isinstance(n,(int,float)) or not math.isfinite(n) or (self.last is not None and n<self.last):
   self.c.source_error(0);raise Blocked('MONOTONIC_REVERSED_OR_INVALID')
  self.last=n;return n
 def plan(self):return self.c.plan(self.now())
 def ack(self,connection,bars,trades):return self.c.ack(connection,bars,trades,self.now())
 def tick(self):return self.c.tick(self.now())
