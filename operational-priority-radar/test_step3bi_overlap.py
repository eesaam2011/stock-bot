from datetime import datetime, timedelta, timezone
import pytest
from rest_bar_maturity import MaturedREST1MinCoordinator

T = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)

class REST:
    def __init__(self):
        self.calls = []
    def bars_multi(self, symbols, start, end, timeframe, **kwargs):
        self.calls.append((start, end))
        rows = [{'t': (T + timedelta(minutes=i)).isoformat(), 'c': i} for i in range(20)]
        return {s: [r for r in rows if start <= datetime.fromisoformat(r['t']) <= end] for s in symbols}

@pytest.mark.parametrize('checkpoint', [30, 60, 90, 120, 180, 600])
@pytest.mark.parametrize('late', [0, 1, 44.999999])
def test_post_bootstrap_checkpoint_is_fetched(checkpoint, late):
    rest = REST(); observations = []; accepted = []
    c = MaturedREST1MinCoordinator(rest, ['A'], lambda *x: accepted.append(x),
        study_observer=lambda *x: observations.append(x), study_symbols=['A'],
        study_checkpoints=(checkpoint,))
    c.poll(T - timedelta(seconds=1), True)
    now = T + timedelta(seconds=60 + checkpoint + late)
    c.poll(now, True)
    target = [x for x in observations if x[2] == T + timedelta(minutes=1)]
    assert len(target) == 1
    assert target[0][3] == checkpoint
    assert all(datetime.fromisoformat(r['t']) <= T for r in target[0][1])
    before = len(observations)
    c.poll(now, True)
    assert len(observations) == before
    assert len({(x[0], x[1]['t']) for x in accepted}) == len(accepted)

@pytest.mark.parametrize('checkpoint', [120, 180])
def test_lateness_boundary_stays_excluded(checkpoint):
    observations = []
    c = MaturedREST1MinCoordinator(REST(), ['A'], lambda *x: None,
        study_observer=lambda *x: observations.append(x), study_symbols=['A'],
        study_checkpoints=(checkpoint,))
    c.poll(T - timedelta(seconds=1), True)
    c.poll(T + timedelta(seconds=60 + checkpoint + 45), True)
    assert not any(x[2] == T + timedelta(minutes=1) for x in observations)

def test_normal_overlap_unchanged_and_disabled_poll_does_not_fetch():
    rest = REST()
    c = MaturedREST1MinCoordinator(rest, ['A'], lambda *x: None)
    c.poll(T, False)
    assert rest.calls == []
    c.poll(T, True)
    assert rest.calls[-1][0] == T - timedelta(minutes=65)
    now = T + timedelta(minutes=10)
    c.poll(now, True)
    assert rest.calls[-1][0] == now - timedelta(minutes=3)

def test_active_study_rejects_empty_checkpoints():
    with pytest.raises(ValueError):
        MaturedREST1MinCoordinator(REST(), ['A'], lambda *x: None,
            study_observer=lambda *x: None, study_symbols=['A'], study_checkpoints=())
