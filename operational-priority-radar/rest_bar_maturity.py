from __future__ import annotations

from datetime import datetime, timedelta, timezone


class RESTBarMaturityUnsafe(RuntimeError):
    pass


class MaturedREST1MinCoordinator:
    """Bounded, deduplicated SIP REST 1Min source for BASE_READY."""

    def __init__(self, rest, symbols, on_bar, *, grace_seconds=90,
                 batch_size=200, max_symbols=20000):
        values = tuple(dict.fromkeys(str(x) for x in symbols))
        if (not values or len(values) > max_symbols or not callable(on_bar)
                or type(grace_seconds) is not int or not 30 <= grace_seconds <= 600
                or type(batch_size) is not int or not 1 <= batch_size <= 200):
            raise ValueError("invalid REST bar maturity configuration")
        self.rest = rest
        self.symbols = values
        self.on_bar = on_bar
        self.grace = timedelta(seconds=grace_seconds)
        self.batch_size = batch_size
        self._seen = set()
        self._bootstrapped = False

    @staticmethod
    def _ts(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise RESTBarMaturityUnsafe("REST_BAR_TIME_NAIVE")
        return parsed.astimezone(timezone.utc)

    def poll(self, now, allow_decision):
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RESTBarMaturityUnsafe("REST_MATURITY_NOW_INVALID")
        if not allow_decision:
            return {"accepted": 0, "duplicates": 0, "eligible": 0,
                    "decision_allowed": False}
        now = now.astimezone(timezone.utc)
        cutoff = now - self.grace
        # First pass seeds the complete rolling BASE_READY window. Later passes
        # overlap three minutes so retries and late REST visibility are harmless.
        start = (now - timedelta(minutes=65) if not self._bootstrapped
                 else now - timedelta(minutes=3))
        accepted = duplicates = eligible = 0
        for offset in range(0, len(self.symbols), self.batch_size):
            chunk = self.symbols[offset:offset + self.batch_size]
            rows = self.rest.bars_multi(chunk, start, cutoff, "1Min",
                                        batch_size=self.batch_size,
                                        max_workers=4)
            for symbol in chunk:
                for bar in sorted(rows.get(symbol) or [], key=lambda x: str(x.get("t"))):
                    bar_start = self._ts(bar.get("t"))
                    if bar_start + timedelta(minutes=1) > cutoff:
                        continue
                    eligible += 1
                    key = (symbol, bar_start.isoformat())
                    if key in self._seen:
                        duplicates += 1
                        continue
                    self.on_bar(symbol, bar, now)
                    self._seen.add(key)
                    accepted += 1
        self._bootstrapped = True
        # Bound dedup memory to the coordinator's 65-minute contract.
        floor = now - timedelta(minutes=70)
        self._seen = {key for key in self._seen
                      if datetime.fromisoformat(key[1]) >= floor}
        return {"accepted": accepted, "duplicates": duplicates,
                "eligible": eligible, "decision_allowed": True,
                "cutoff": cutoff.isoformat(), "grace_seconds": int(self.grace.total_seconds())}

