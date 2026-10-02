"""Bounded, run-time-only coverage diagnostics for the read-only study runner."""
from datetime import datetime, timedelta, timezone


class RESTMaturityCoverage:
    def __init__(self, symbols, checkpoints, sample_every_minutes, *,
                 max_unique_rows=100000, max_sample_slots=5000):
        self.symbols = tuple(symbols)
        self.checkpoints = tuple(checkpoints)
        if (not self.symbols or len(set(self.symbols)) != len(self.symbols)
                or not 1 <= len(self.symbols) <= 64
                or type(sample_every_minutes) is not int or sample_every_minutes <= 0
                or type(max_unique_rows) is not int or max_unique_rows < 1
                or type(max_sample_slots) is not int or max_sample_slots < 1):
            raise ValueError('invalid maturity coverage configuration')
        self.sample_every_minutes = sample_every_minutes
        self.max_unique_rows = max_unique_rows
        self.max_sample_slots = max_sample_slots
        self._rows = set()
        self._slots = {}
        self._stats = {s: {'fetch_calls': 0, 'empty_responses': 0,
            'missing_symbol_responses': 0, 'returned_row_appearances': 0,
            'first_fetch_at': None, 'last_fetch_at': None,
            'first_bar_start': None, 'last_bar_start': None,
            'recorded_input_rows_min': None, 'recorded_input_rows_max': None}
            for s in self.symbols}

    @staticmethod
    def _ts(value):
        ts = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if ts.tzinfo is None:
            raise ValueError('coverage timestamp must have timezone')
        return ts.astimezone(timezone.utc)

    def _slot(self, symbol, bar_end):
        key = (symbol, bar_end.isoformat())
        if key not in self._slots:
            if len(self._slots) >= self.max_sample_slots:
                raise RuntimeError('REST_MATURITY_COVERAGE_SLOT_LIMIT')
            self._slots[key] = {'eligible': set(), 'recorded': set(), 'fetched': False}
        return self._slots[key]

    def fetched(self, symbols, rows, poll_at, *, lateness_seconds=45):
        """Count actual returned rows; poll_at is the coordinator's time reference."""
        poll_at = self._ts(poll_at)
        for symbol in symbols:
            stats = self._stats[symbol]
            stats['fetch_calls'] += 1
            stats['first_fetch_at'] = stats['first_fetch_at'] or poll_at.isoformat()
            stats['last_fetch_at'] = poll_at.isoformat()
            if symbol not in rows:
                stats['missing_symbol_responses'] += 1
            values = rows.get(symbol) or []
            stats['empty_responses'] += int(not values)
            stats['returned_row_appearances'] += len(values)
            for row in values:
                start = self._ts(row['t'])
                key = (symbol, start.isoformat())
                if key not in self._rows:
                    if len(self._rows) >= self.max_unique_rows:
                        raise RuntimeError('REST_MATURITY_COVERAGE_ROW_LIMIT')
                    self._rows.add(key)
                text = start.isoformat()
                stats['first_bar_start'] = min(stats['first_bar_start'] or text, text)
                stats['last_bar_start'] = max(stats['last_bar_start'] or text, text)
                end = start + timedelta(minutes=1)
                if int(end.timestamp() // 60) % self.sample_every_minutes:
                    continue
                slot = self._slot(symbol, end)
                slot['fetched'] = True
                for checkpoint in self.checkpoints:
                    due = end + timedelta(seconds=checkpoint)
                    if due <= poll_at < due + timedelta(seconds=lateness_seconds):
                        slot['eligible'].add(checkpoint)

    def recorded(self, symbol, rows, bar_end, checkpoint):
        if checkpoint not in self.checkpoints:
            raise ValueError('unknown coverage checkpoint')
        slot = self._slot(symbol, self._ts(bar_end))
        if checkpoint in slot['recorded']:
            return
        slot['recorded'].add(checkpoint)
        stats = self._stats[symbol]
        n = len(rows)
        old = stats['recorded_input_rows_min']
        stats['recorded_input_rows_min'] = n if old is None else min(old, n)
        old = stats['recorded_input_rows_max']
        stats['recorded_input_rows_max'] = n if old is None else max(old, n)

    def report(self):
        output = {}
        for symbol in self.symbols:
            stats = dict(self._stats[symbol])
            slots = [v for (s, _), v in self._slots.items() if s == symbol]
            row_count = sum(s == symbol for s, _ in self._rows)
            counts = {str(c): sum(c in v['recorded'] for v in slots)
                      for c in self.checkpoints}
            sampled = sum(v['fetched'] for v in slots)
            reason = ('RECORDED' if sum(counts.values()) else
                      'NO_FETCH' if not stats['fetch_calls'] else
                      'NO_ROWS_RETURNED' if not row_count else
                      'NO_SAMPLED_BAR_RETURNED' if not sampled else
                      'SAMPLED_BARS_WITHOUT_RECORDS')
            stats.update(unique_returned_bars=row_count, sampled_slots_seen=sampled,
                eligible_slots_seen_by_checkpoint={str(c): sum(c in v['eligible'] for v in slots)
                                                   for c in self.checkpoints},
                recorded_by_checkpoint=counts,
                eligible_without_record_by_checkpoint={str(c): sum(c in v['eligible'] and
                    c not in v['recorded'] for v in slots) for c in self.checkpoints},
                fully_recorded_slots=sum(set(self.checkpoints) <= v['recorded'] for v in slots),
                reason=reason)
            output[symbol] = stats
        by_checkpoint = {str(c): sum(v['recorded_by_checkpoint'][str(c)] for v in output.values())
                         for c in self.checkpoints}
        return {'schema': 'OPR_REST_MATURITY_COVERAGE_V1',
            'source': 'ACTUAL_RUN_FETCHES_AND_SUCCESSFUL_AUDIT_CALLBACKS',
            'time_reference': 'COORDINATOR_POLL_AT_NOT_RESPONSE_RECEIPT_TIME',
            'selected_symbols': len(self.symbols),
            'symbols_with_records': sum(v['reason'] == 'RECORDED' for v in output.values()),
            'records_by_checkpoint': by_checkpoint,
            'total_records': sum(by_checkpoint.values()),
            'max_unique_rows': self.max_unique_rows, 'max_sample_slots': self.max_sample_slots,
            'symbols': output,
            'limits': 'Returned rows are not proof of full market coverage or provider finality; '
                      'sampled_slots_seen can include bootstrap history outside due-time gates.'}


class CoverageREST:
    """Wrap only the study runner's REST fetches; production REST is unchanged."""
    def __init__(self, rest, coverage):
        self.rest = rest
        self.coverage = coverage
        self.poll_at = None

    def bars_multi(self, symbols, *args, **kwargs):
        rows = self.rest.bars_multi(symbols, *args, **kwargs)
        self.coverage.fetched(symbols, rows, self.poll_at)
        return rows
