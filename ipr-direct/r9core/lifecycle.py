"""Latch failures before any stop flag wakes sibling tasks."""
import asyncio
class FirstFailure:
 def __init__(self):self.error=None;self.task=None
 def capture(self,task):
  if task.cancelled():return
  exc=task.exception()
  if exc is not None and self.error is None:self.error=exc;self.task=task.get_name()
 def raise_first(self):
  if self.error is not None:raise self.error
async def wait_first(tasks,failure):
 for t in tasks:t.add_done_callback(failure.capture)
 done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
 for t in done:failure.capture(t)
 failure.raise_first()
async def close_tasks(tasks,failure,timeout=60.):
 for t in tasks:t.cancel()
 try:
  results=await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),timeout)
  for result in results:
   if isinstance(result,Exception) and failure.error is None:failure.error=result
 except asyncio.TimeoutError as exc:
  if failure.error is None:failure.error=RuntimeError('TAIL_60_SECONDS_PARENT_KILL_REQUIRED')
 failure.raise_first()
