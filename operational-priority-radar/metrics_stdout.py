"""Bounded JSON stdout exporter on a dedicated writer thread, not the decision loop."""
import asyncio,concurrent.futures,json,sys,time,threading,builtins
from contextlib import contextmanager
from datetime import datetime,timezone

STDOUT_LOCK=threading.RLock()

@contextmanager
def serialized_prints():
    original=builtins.print
    import lock_output
    output=lock_output.Output(original);lock_output.ACTIVE=output
    builtins.print=output.print
    try:yield
    finally:
        output.flush();builtins.print=original;lock_output.ACTIVE=None

class JsonStdout:
    def __init__(self,stream=None,capacity=8,identity=None):
        self.stream=stream or sys.stdout;self.queue=asyncio.Queue(maxsize=capacity)
        self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='opr-json-stdout')
        self.identity=identity or {};self.dropped=0;self.failures=0;self.written=0
        self._write_order=threading.Condition();self._issued=0;self._completed=0;self._tickets={};self._closed=False;self._futures=set()
    def offer(self,row):
        row={**row,**self.identity,'utc':datetime.now(timezone.utc).isoformat(),
             'stdout_reports_dropped_total':self.dropped,'stdout_write_errors_total':self.failures}
        try:self.queue.put_nowait(row);return True
        except asyncio.QueueFull:
            self.dropped+=1
            if row.get('stage') in {'FAIL_CLOSED_SELF_STOP','SELF_STOP_OUTCOME'}:
                # Preserve the stop reason ahead of old telemetry, without waiting.
                try:
                    old=self.queue.get_nowait();self.queue.task_done()
                except asyncio.QueueEmpty:return False
                # Never evict an older security record. Synchronous enqueue/write
                # fallback is outside decision lock, and keeps security FIFO.
                if old.get('stage') in {'FAIL_CLOSED_SELF_STOP','SELF_STOP_OUTCOME'}:
                    self.dropped-=1
                    self._submit(old)
                row['stdout_reports_dropped_total']=self.dropped
                self.queue.put_nowait(row);return True
            return False
    def _reserve_write(self):
        with self._write_order:
            ticket=self._issued;self._issued+=1;self._tickets[ticket]='pending';return ticket
    def _complete(self,ticket):
        with self._write_order:
            if ticket < self._completed:return
            self._tickets[ticket]='done'
            while self._tickets.get(self._completed)=='done':
                del self._tickets[self._completed];self._completed+=1
            self._write_order.notify_all()
    def _reservation(self):
        # Includes recovery if allocation raises AFTER incrementing _issued.
        with self._write_order:
            before=self._issued
            try:return self._reserve_write()
            except BaseException:
                for ticket in range(before,self._issued):self._complete(ticket)
                raise
    def _write(self,row,ticket=None):
        ticket=self._reservation() if ticket is None else ticket
        with self._write_order:
            while ticket!=self._completed:
                if ticket<self._completed:return
                self._write_order.wait()
            self._tickets[ticket]='running'
        try:self._write_payload(row)
        finally:self._complete(ticket)
    def _submit(self,row):
        ticket=self._reservation()
        try:
            if self._closed:raise RuntimeError('STDOUT_EXECUTOR_CLOSED')
            future=self.executor.submit(self._write,row,ticket)
        except BaseException:
            self._complete(ticket);raise
        with self._write_order:self._futures.add(future)
        def finished(f):
            # A cancelled queued callable never runs its _write finally.
            if f.cancelled():self._complete(ticket);self.failures+=1
            else:
                try:f.result();self.written+=1
                except BaseException:self.failures+=1
            with self._write_order:self._futures.discard(f)
        future.add_done_callback(finished)
        return future
    def _write_payload(self,row):
        line=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n'
        import lock_output
        if lock_output.ACTIVE is not None and self.stream is sys.stdout:
            lock_output.ACTIVE.print(line,end="")
        else:
            with STDOUT_LOCK:
                self.stream.write(line);self.stream.flush()
    async def consume(self):
        while True:
            row=await self.queue.get();future=None
            try:
                future=self._submit(row)
                # Shield allows explicit cancellation of queued work and lets
                # a running writer keep ownership until its finally executes.
                await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                if future is not None:future.cancel()
                raise
            except Exception:
                if future is None:self.failures+=1 # submitted failures counted by callback
            finally:self.queue.task_done()
    async def flush(self):await self.queue.join()
    def close(self):
        self._closed=True
        self.executor.shutdown(wait=False,cancel_futures=True)

class OfferQueue:
    """PassiveMetrics uses a Queue-shaped nonblocking target; consumer is attached above."""
    def __init__(self,sink):self.sink=sink
    def put_nowait(self,row):
        if not self.sink.offer(row):raise asyncio.QueueFull()
