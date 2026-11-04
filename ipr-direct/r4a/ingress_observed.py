"""Instrumentation around the unchanged original safety implementation."""
import time
from ws_ingress import BoundedWSIngress
class ObservedIngress(BoundedWSIngress):
 def __init__(self,*a,diagnostics,**kw):
  self.diagnostics=diagnostics;self.failure_diagnostics=None
  logger=kw.pop('logger',print)
  def observed_logger(row,**opts):
   logger({**row,'wave':self.failure_diagnostics,'ws_ingress':self.snapshot()},**opts)
  super().__init__(*a,logger=observed_logger,**kw)
 def submit(self,kind,msg,received_at):
  self.diagnostics.received(kind,received_at)
  return super().submit(kind,msg,received_at)
 def _work(self,item):
  start=time.monotonic();ok=False
  try:
   ok=super()._work(item);return ok
  finally:
   if item[0]=='BAR':self.diagnostics.finished(item[2],time.monotonic()-start,ok)
 def _fail(self,exc):
  if self.failure is not None and not self.failure.done():
   try:self.failure_diagnostics=self.diagnostics.snapshot(self)
   except Exception as error:self.failure_diagnostics={'diagnostics_error_type':type(error).__name__}
  return super()._fail(exc)
 def snapshot(self):
  result=super().snapshot()
  try:result['wave']=self.diagnostics.snapshot(self)
  except Exception as error:result['wave']={'diagnostics_error_type':type(error).__name__}
  return result
