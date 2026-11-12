"""Session-scoped isolation for symbols with uncertain market data.

An uncertainty affecting one dynamically subscribed symbol must not invalidate
the broad-universe bar session.  Isolation is deliberately conservative: once
blocked, a symbol cannot create E/B, confluence, entry, or an actionable alert
for the rest of that market session.  A later process/session starts clean only
after the normal startup recovery gate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import threading


class SymbolDecisionIsolationUnsafe(RuntimeError):
    pass


class SymbolDecisionIsolation:
    def __init__(self, session, *, max_symbols=256):
        if not isinstance(session, str) or not session:
            raise ValueError("SYMBOL_ISOLATION_SESSION_INVALID")
        if type(max_symbols) is not int or not 1 <= max_symbols <= 4096:
            raise ValueError("SYMBOL_ISOLATION_LIMIT_INVALID")
        self.session = session
        self.max_symbols = max_symbols
        self._blocked = {}
        self._lock = threading.RLock()

    @staticmethod
    def _utc(value):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SymbolDecisionIsolationUnsafe("SYMBOL_ISOLATION_TIME_INVALID")
        return value.astimezone(timezone.utc)

    def block(self, symbol, reason, observed_at, *, evidence=None):
        if not isinstance(symbol, str) or not symbol or len(symbol) > 32:
            raise SymbolDecisionIsolationUnsafe("SYMBOL_ISOLATION_SYMBOL_INVALID")
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise SymbolDecisionIsolationUnsafe("SYMBOL_ISOLATION_REASON_INVALID")
        observed = self._utc(observed_at)
        with self._lock:
            prior = self._blocked.get(symbol)
            if prior is not None:
                prior["observations"] += 1
                prior["latest_observed_at_utc"] = observed.isoformat()
                return deepcopy(prior)
            if len(self._blocked) >= self.max_symbols:
                raise SymbolDecisionIsolationUnsafe("SYMBOL_ISOLATION_LIMIT")
            record = {
                "schema": "OPR_SYMBOL_DECISION_ISOLATION_V1",
                "session": self.session,
                "symbol": symbol,
                "reason": reason,
                "blocked_at_utc": observed.isoformat(),
                "latest_observed_at_utc": observed.isoformat(),
                "observations": 1,
                "scope": "SYMBOL_REMAINDER_OF_SESSION",
                "new_eb_allowed": False,
                "new_confluence_allowed": False,
                "new_entry_allowed": False,
                "retroactive_entry_allowed": False,
                "other_symbols_affected": False,
                "evidence": deepcopy(evidence) if evidence is not None else None,
            }
            self._blocked[symbol] = record
            return deepcopy(record)

    def blocked(self, symbol):
        with self._lock:
            return symbol in self._blocked

    def require_allowed(self, symbol):
        if self.blocked(symbol):
            raise SymbolDecisionIsolationUnsafe("SYMBOL_DECISION_BLOCKED")
        return True

    def snapshot(self):
        with self._lock:
            return {
                "schema": "OPR_SYMBOL_DECISION_ISOLATION_SNAPSHOT_V1",
                "session": self.session,
                "blocked_count": len(self._blocked),
                "blocked_symbols": tuple(sorted(self._blocked)),
                "records": tuple(deepcopy(self._blocked[k])
                                 for k in sorted(self._blocked)),
                "global_session_invalidated": False,
                "direct_authorized": False,
                "actionable_alerts_authorized": False,
            }
