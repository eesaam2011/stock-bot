"""D: static loader extracted from reviewed frozen controller, no dynamic updates."""
from collections import Counter
from threading import RLock
from ipr_filters import FilterEngine
class UniverseUnavailable(RuntimeError):pass
class FrozenSubscriptionUniverse:
    def __init__(self, frozen_root, session, *, on_change=None):
        self.lock=RLock();self.on_change=on_change;self.counters=Counter()
        self.needs={};self.version=0;self.acked_version=None;self.error=None
        self.session=session;self.static=frozenset();self.engine=None
        try:
            self.engine=FilterEngine(frozen_root,enabled=False)
            if not self.engine.integrity_ok:raise UniverseUnavailable('FROZEN_INTEGRITY_FAILED')
            day=self.engine.day_state(session)
            if not day['allowed']:raise UniverseUnavailable(day['state'])
            self.static=frozenset(s for s in self.engine.frozen.day['assets'] if not self.engine.static_reasons(s)[0])
            if not self.static:raise UniverseUnavailable('FROZEN_ELIGIBLE_EMPTY')
        except Exception as exc:
            self.error=str(exc) if isinstance(exc,UniverseUnavailable) else 'FROZEN_LOAD_FAILED'
            self.counters[self.error]+=1
        self.desired=self.static

    def check_session(self,session):
        with self.lock:
            self.session=session
            state=self.engine.day_state(session) if self.engine and self.engine.integrity_ok else {'allowed':False,'state':'FROZEN_INTEGRITY_FAILED','age_sessions':None}
            if not state['allowed']:
                self.error=state['state'];self.counters[self.error]+=1
                # Existing static data retained for monitoring; nomination closed.
            return state

