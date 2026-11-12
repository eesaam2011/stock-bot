import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import rest_maturity_shadow_runner as runner
from rest_maturity_coverage import RESTMaturityCoverage

T = datetime(2026, 10, 5, 14, 29, tzinfo=timezone.utc)
CHECKPOINTS = (30, 60, 90, 120, 180)


def test_coverage_distinguishes_zero_reasons_and_deduplicates():
    c = RESTMaturityCoverage(('ACTIVE', 'QUIET', 'EMPTY', 'MISSING'), CHECKPOINTS, 30)
    rows = {'ACTIVE': [{'t': T.isoformat()}],
            'QUIET': [{'t': (T-timedelta(minutes=1)).isoformat()}], 'EMPTY': []}
    for checkpoint in CHECKPOINTS:
        now = T+timedelta(seconds=60+checkpoint)
        c.fetched(c.symbols, rows, now)
        c.recorded('ACTIVE', rows['ACTIVE'], T+timedelta(minutes=1), checkpoint)
        c.recorded('ACTIVE', rows['ACTIVE'], T+timedelta(minutes=1), checkpoint)
    r = c.report()
    assert r['selected_symbols'] == 4 and r['symbols_with_records'] == 1
    assert r['total_records'] == 5
    assert r['symbols']['ACTIVE']['unique_returned_bars'] == 1
    assert r['symbols']['ACTIVE']['returned_row_appearances'] == 5
    assert r['symbols']['ACTIVE']['fully_recorded_slots'] == 1
    assert set(r['symbols']['ACTIVE']['eligible_without_record_by_checkpoint'].values()) == {0}
    assert r['symbols']['QUIET']['reason'] == 'NO_SAMPLED_BAR_RETURNED'
    assert r['symbols']['EMPTY']['reason'] == 'NO_ROWS_RETURNED'
    assert r['symbols']['MISSING']['missing_symbol_responses'] == 5
    assert r['records_by_checkpoint'] == {str(c): 1 for c in CHECKPOINTS}


def test_eligible_missing_record_is_visible_and_late_fetch_is_not_eligible():
    c = RESTMaturityCoverage(('A',), (180,), 30)
    c.fetched(('A',), {'A': [{'t': T.isoformat()}]}, T+timedelta(seconds=240))
    r = c.report()['symbols']['A']
    assert r['reason'] == 'SAMPLED_BARS_WITHOUT_RECORDS'
    assert r['eligible_without_record_by_checkpoint'] == {'180': 1}
    late = RESTMaturityCoverage(('A',), (180,), 30)
    late.fetched(('A',), {'A': [{'t': T.isoformat()}]}, T+timedelta(seconds=285))
    assert late.report()['symbols']['A']['eligible_slots_seen_by_checkpoint'] == {'180': 0}


@pytest.mark.parametrize('limit_kind', ['row', 'slot'])
def test_coverage_caps_fail_closed(limit_kind):
    c = RESTMaturityCoverage(('A',), CHECKPOINTS, 30,
        max_unique_rows=1 if limit_kind == 'row' else 100, max_sample_slots=1)
    c.fetched(('A',), {'A': [{'t': T.isoformat()}]}, T+timedelta(seconds=90))
    with pytest.raises(RuntimeError, match='REST_MATURITY_COVERAGE_.*_LIMIT'):
        c.fetched(('A',), {'A': [{'t': (T+timedelta(minutes=30)).isoformat()}]},
                  T+timedelta(minutes=31, seconds=30))


@pytest.mark.parametrize('failure', [None, 'audit', 'limit'])
def test_runner_writes_runtime_coverage_and_partial_failure_evidence(tmp_path, failure):
    class Redis:
        def ping(self): return True
    class REST:
        def __init__(self, *args): pass
        def bars_multi(self, symbols, start, end, *args, **kwargs):
            rows = {'A': [{'t': T.isoformat()}],
                    'QUIET': [{'t': (T-timedelta(minutes=1)).isoformat()}], 'EMPTY': []}
            return {s: [r for r in rows[s] if start <= datetime.fromisoformat(r['t']) <= end]
                    for s in symbols if s in rows}
    class Audit:
        def __init__(self, *args, **kwargs): pass
        def record_checkpoint(self, lane, symbol, rows, bar_end, observed_at, checkpoint):
            if failure == 'audit': raise RuntimeError('AUDIT_WRITE_FAILED')
            return f'{symbol}:{bar_end}:{checkpoint}'
    clock = [T-timedelta(seconds=1)]
    def now(): return clock[0]
    def sleep(seconds): clock[0] += timedelta(seconds=seconds)
    args = SimpleNamespace(session='coverage-test', commit='a'*40, due_hours=18,
        symbols=4, sample_every_minutes=30, max_observations=1 if failure == 'limit' else 100,
        output=str(tmp_path/'observe.json'), end=T+timedelta(seconds=300), interval=15)
    with patch.dict('os.environ', {'ALPACA_API_KEY':'k','ALPACA_SECRET_KEY':'s','REDIS_URL':'redis://unit'}), \
         patch.object(runner, 'AlpacaREST', REST), \
         patch.object(runner, '_redis_from_url', return_value=Redis()), \
         patch.object(runner, 'build_operational_universe', return_value=('A','QUIET','EMPTY','MISSING')), \
         patch.object(runner, 'RESTBarShadowAudit', Audit):
        if failure:
            with pytest.raises(RuntimeError): runner.observe(args, now, sleep)
        else:
            runner.observe(args, now, sleep)
    body = json.loads((tmp_path/'observe.json').read_text())
    cov = body['coverage']
    assert cov['selected_symbols'] == 4
    assert body['observations'] == cov['total_records']
    assert body['canonical_writes'] == body['alerts_sent'] == 0
    assert not body['actionable_alerts_authorized'] and not body['finality_proven']
    if failure:
        assert body['errors'] and 'completed_at' not in body
        assert body['observations'] == (1 if failure == 'limit' else 0)
        assert any(cov['symbols']['A']['eligible_without_record_by_checkpoint'].values())
    else:
        assert not body['errors']
        assert cov['records_by_checkpoint'] == {str(c): 1 for c in CHECKPOINTS}
        assert cov['symbols']['A']['fully_recorded_slots'] == 1
        assert cov['symbols']['QUIET']['reason'] == 'NO_SAMPLED_BAR_RETURNED'
        assert cov['symbols']['EMPTY']['reason'] == 'NO_ROWS_RETURNED'
        assert cov['symbols']['MISSING']['missing_symbol_responses'] > 0
