"""Observation failures may never replace/suppress application exceptions."""
import contextlib,json

def call(obj,name,*args,**kwargs):
    try:return getattr(obj,name)(*args,**kwargs)
    except BaseException:return None

@contextlib.contextmanager
def observing(factory,*args,**kwargs):
    cm=None;entered=False
    try:
        cm=factory(*args,**kwargs);cm.__enter__();entered=True
    except BaseException:pass
    try:yield entered
    except BaseException as error:
        if entered:
            try:cm.__exit__(type(error),error,error.__traceback__)
            except BaseException:pass
        raise # Ignore even a True suppression request by a broken observer.
    else:
        if entered:
            try:cm.__exit__(None,None,None)
            except BaseException:pass

def bounded(value,limit):
    try:
        if len(json.dumps(value,allow_nan=False).encode())<=limit:return value
        return {'truncated':True,'reason':'OBSERVATION_SIZE_LIMIT','limit_bytes':limit}
    except BaseException:return None

class SafeTimings:
    def __init__(self,inner):self.inner=inner
    @property
    def started(self):
        try:return self.inner.started
        except BaseException:return None
    def begin(self,*a,**kw):return call(self.inner,'begin',*a,**kw)
    def finish(self,*a,**kw):return call(self.inner,'finish',*a,**kw)
    def phase(self,*a,**kw):return observing(lambda:self.inner.phase(*a,**kw))
    def requests(self,*a,**kw):return observing(lambda:self.inner.requests(*a,**kw))
    def snapshot(self):return bounded(call(self.inner,'snapshot'),8192)
    def attempt_snapshot(self):return bounded(call(self.inner,'attempt_snapshot'),4096)
