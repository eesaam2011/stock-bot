from datetime import timedelta
from .rest import instant
class RecoveryCoordinator:
 """Wrap original canonical reconciler; never declares historical fills live trades."""
 def __init__(self,original_recovery,clock_ok,stream_overlap_proof):self.original=original_recovery;self.clock_ok=clock_ok;self.stream_overlap_proof=stream_overlap_proof
 def run(self):return self.original.run()
 def on_status(self,msg):return self.original.on_status(msg)
 def verify_symbol_continuity(self,s,one,five,now):
  if not self.clock_ok() or not one or not five:return False
  if any(instant(b['t'])+timedelta(minutes=1)>now for b in one):return False
  if any(b.get('_timeframe')!='native_5Min' or instant(b['t'])+timedelta(minutes=5)>now for b in five):return False
  # No min(row_count) shortcut: actual overlap proof required from buffered stream.
  return self.stream_overlap_proof(s,one,five,now) is True
