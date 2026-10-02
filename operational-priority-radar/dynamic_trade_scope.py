from __future__ import annotations

import threading


class DynamicTradeScopeUnsafe(RuntimeError):
    pass


class DynamicTradeScope:
    """Thread-safe desired/ACKed trade scope tied to one SIP epoch."""

    def __init__(self, max_symbols=256, on_change=None):
        if type(max_symbols) is not int or not 1 <= max_symbols <= 4096:
            raise ValueError("invalid dynamic trade scope limit")
        self.max_symbols = max_symbols
        self.on_change = on_change
        self._lock = threading.RLock()
        self._desired = set()
        self._acked = set()
        self._epoch = None

    def start_epoch(self, epoch, initial=()):
        values = {str(x) for x in initial}
        if type(epoch) is not int or epoch < 1 or len(values) > self.max_symbols:
            raise DynamicTradeScopeUnsafe("DYNAMIC_TRADE_EPOCH_INVALID")
        with self._lock:
            self._epoch = epoch
            self._desired = values
            self._acked = set()

    def invalidate(self):
        with self._lock:
            self._epoch = None
            self._acked.clear()

    def require(self, symbol, required=True):
        if not isinstance(symbol, str) or not symbol:
            raise DynamicTradeScopeUnsafe("DYNAMIC_TRADE_SYMBOL_INVALID")
        with self._lock:
            before = set(self._desired)
            if required:
                if symbol not in self._desired and len(self._desired) >= self.max_symbols:
                    raise DynamicTradeScopeUnsafe("DYNAMIC_TRADE_SCOPE_LIMIT")
                self._desired.add(symbol)
            else:
                self._desired.discard(symbol)
            changed = before != self._desired
            desired = tuple(sorted(self._desired))
        if changed and self.on_change:
            self.on_change(desired)
        return changed

    def acknowledge(self, epoch, symbols):
        values = {str(x) for x in symbols}
        with self._lock:
            if epoch != self._epoch:
                raise DynamicTradeScopeUnsafe("DYNAMIC_TRADE_ACK_EPOCH_MISMATCH")
            if len(values) > self.max_symbols:
                raise DynamicTradeScopeUnsafe("DYNAMIC_TRADE_ACK_LIMIT")
            self._acked = values
            return self.snapshot()

    def authorized(self, epoch, symbol):
        with self._lock:
            return epoch == self._epoch and symbol in self._acked

    def desired(self):
        with self._lock:
            return tuple(sorted(self._desired))

    def snapshot(self):
        with self._lock:
            return {"epoch": self._epoch, "limit": self.max_symbols,
                    "desired": tuple(sorted(self._desired)),
                    "acked": tuple(sorted(self._acked)),
                    "fully_acked": self._desired == self._acked}

