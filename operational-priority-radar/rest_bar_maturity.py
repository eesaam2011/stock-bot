from __future__ import annotations

from datetime import datetime, timedelta, timezone


class RESTBarMaturityUnsafe(RuntimeError):
    pass


class MaturedREST1MinCoordinator:
    """Bounded, deduplicated SIP REST 1Min source for BASE_READY."""

    def __init__(self, rest, symbols, on_bar, *, grace_seconds=90,
                 batch_size=200, max_symbols=20000, study_observer=None,
                 study_symbols=(), study_checkpoints=(30,60,90,120,180),
                 observation_lateness_seconds=45):
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
        if study_observer is not None and not callable(study_observer):
            raise ValueError("invalid REST maturity study observer")
        checkpoints=tuple(study_checkpoints)
        if ((study_observer is not None and study_symbols and not checkpoints)
                or any(type(x) is not int or not 30 <= x <= 600 for x in checkpoints)
                or tuple(sorted(set(checkpoints))) != checkpoints
                or type(observation_lateness_seconds) is not int
                or not 15 <= observation_lateness_seconds <= 180):
            raise ValueError("invalid REST maturity study configuration")
        study=set(str(x) for x in study_symbols)
        if not study.issubset(set(values)) or len(study)>64:
            raise ValueError("invalid REST maturity study symbols")
        self.study_observer=study_observer
        self.study_symbols=study
        self.study_checkpoints=checkpoints
        self.observation_lateness=timedelta(seconds=observation_lateness_seconds)
        self._study_seen=set()
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
        fetch_cutoff=(now-timedelta(seconds=self.study_checkpoints[0])
                      if self.study_observer and self.study_symbols else cutoff)
        # Include the full 1Min bar, the largest checkpoint, and the allowed
        # REST observation lateness. Preserve the normal three-minute overlap
        # when no study is active. The due-time gate below still rejects late data.
        overlap = timedelta(minutes=3)
        if self.study_observer is not None and self.study_symbols:
            overlap = max(overlap, timedelta(
                seconds=60 + max(self.study_checkpoints)
            ) + self.observation_lateness)
        start = (now - timedelta(minutes=65) if not self._bootstrapped
                 else now - overlap)
        accepted = duplicates = eligible = 0
        for offset in range(0, len(self.symbols), self.batch_size):
            chunk = self.symbols[offset:offset + self.batch_size]
            rows = self.rest.bars_multi(chunk, start, fetch_cutoff, "1Min",
                                        batch_size=self.batch_size,
                                        max_workers=4)
            for symbol in chunk:
                ordered=sorted(rows.get(symbol) or [], key=lambda x: str(x.get("t")))
                for index,bar in enumerate(ordered):
                    bar_start = self._ts(bar.get("t"))
                    bar_end=bar_start+timedelta(minutes=1)
                    if self.study_observer is not None and symbol in self.study_symbols:
                        for checkpoint in self.study_checkpoints:
                            due=bar_end+timedelta(seconds=checkpoint)
                            study_key=(symbol,bar_start.isoformat(),checkpoint)
                            if (due <= now < due+self.observation_lateness
                                    and study_key not in self._study_seen):
                                causal=[x for x in ordered[:index+1]
                                        if self._ts(x.get("t"))<=bar_start]
                                self.study_observer(symbol,causal,bar_end,
                                                    checkpoint,now)
                                self._study_seen.add(study_key)
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
        self._study_seen={key for key in self._study_seen
                          if datetime.fromisoformat(key[1])>=floor}
        return {"accepted": accepted, "duplicates": duplicates,
                "eligible": eligible, "decision_allowed": True,
                "cutoff": cutoff.isoformat(), "grace_seconds": int(self.grace.total_seconds()),
                "study_symbols":len(self.study_symbols),
                "study_observations_retained":len(self._study_seen),
                "study_checkpoints":self.study_checkpoints}
