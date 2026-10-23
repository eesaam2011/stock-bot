"""Bounded JSON stdout exporter on a dedicated writer thread, not the decision loop."""
import asyncio,concurrent.futures,json,sys,time,threading,builtins
from contextlib import contextmanager
from datetime import datetime,timezone

STDOUT_LOCK=threading.RLock()

@contextmanager
def serialized_prints():
    original=builtins.print
    def locked(*a,**kw):
        with STDOUT_LOCK:return original(*a,**kw)
    builtins.print=locked
    try:yield
    finally:builtins.print=original

class JsonStdout:
    def __init__(self,stream=None,capacity=8,identity=None):
        self.stream=stream or sys.stdout;self.queue=asyncio.Queue(maxsize=capacity)
        self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='opr-json-stdout')
        self.identity=identity or {};self.dropped=0;self.failures=0;self.written=0
    def offer(self,row):
        row={**row,**self.identity,'utc':datetime.now(timezone.utc).isoformat(),
             'stdout_reports_dropped_total':self.dropped,'stdout_write_errors_total':self.failures}
        try:self.queue.put_nowait(row);return True
        except asyncio.QueueFull:
            self.dropped+=1
            if row.get('stage') in {'FAIL_CLOSED_SELF_STOP','SELF_STOP_OUTCOME'}:
                # Preserve the stop reason ahead of old telemetry, without waiting.
                try:self.queue.get_nowait();self.queue.task_done()
                except asyncio.QueueEmpty:return False
                row['stdout_reports_dropped_total']=self.dropped
                self.queue.put_nowait(row);return True
            return False
    def _write(self,row):
        line=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n'
        with STDOUT_LOCK:
            self.stream.write(line);self.stream.flush()
    async def consume(self):
        while True:
            row=await self.queue.get()
            try:
                await asyncio.get_running_loop().run_in_executor(self.executor,self._write,row)
                self.written+=1
            except Exception:self.failures+=1
            finally:self.queue.task_done()
    async def flush(self):await self.queue.join()
    def close(self):self.executor.shutdown(wait=False,cancel_futures=True)

class OfferQueue:
    """PassiveMetrics uses a Queue-shaped nonblocking target; consumer is attached above."""
    def __init__(self,sink):self.sink=sink
    def put_nowait(self,row):
        if not self.sink.offer(row):raise asyncio.QueueFull()
