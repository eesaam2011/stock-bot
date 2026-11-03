import unittest,copy
from unittest.mock import Mock
from datetime import timedelta
from dintegration.clock import ClockUnavailable
from dreview.core import Blocked
from test_direct import OP,bar
import test_r4

class R5HistoryCommit(unittest.TestCase):
 def fixture(self):
  f=test_r4.R4Tests().deferred()
  f.history['Y']=[bar(OP-timedelta(minutes=2)),bar(OP-timedelta(minutes=1)),bar(OP)]
  return f
 def history(self,f):return {s:list(h) for s,h in f.a.bridge.p.br.history.items()}
 def test_clock_after_fetch_no_history_change_retry_exact_rows(self):
  for fail_at in (2,3):
   with self.subTest(check=fail_at):
    f=self.fixture();before=copy.deepcopy(self.history(f));epochs=len(f.epochs);n=[0]
    def checks():
     n[0]+=1
     if n[0]==fail_at:raise ClockUnavailable('fixture')
    f.a.checks=checks
    self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertEqual(self.history(f),before);self.assertEqual(len(f.epochs),epochs);self.assertTrue(f.a.transport_trusted)
    self.assertEqual(f.calls[-1]['symbols'],['Y'])
    f.a.checks=lambda:None;self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]))
    rows=list(f.a.bridge.p.br.history['Y']);expected=[f.a.bridge.p.br._compact_bar(b) for b in f.history['Y']]
    self.assertEqual(len(rows),3);self.assertEqual(rows,expected);self.assertEqual(len({r[0] for r in rows}),3)
    self.assertEqual(list(f.a.bridge.p.br.history['X']),before['X'])
 def test_journal_failure_before_preload_leaves_all_history_unchanged(self):
  f=self.fixture();before=copy.deepcopy(self.history(f));f.a.checkpoint.append_epoch=Mock(side_effect=ConnectionError('fixture'))
  with self.assertRaises(ConnectionError):f.a.admit_incremental(['Y'],f.now[0])
  self.assertEqual(self.history(f),before);self.assertFalse(f.a.transport_trusted)
 def test_postjournal_root_failure_before_preload(self):
  f=self.fixture();before=copy.deepcopy(self.history(f));original=f.a.checkpoint.append_epoch
  def append(event):original(event);f.a.root_check=lambda:False
  f.a.checkpoint.append_epoch=append
  with self.assertRaises(Blocked):f.a.admit_incremental(['Y'],f.now[0])
  self.assertEqual(self.history(f),before);self.assertFalse(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_order_journal_leadership_root_preload_attest(self):
  f=self.fixture();events=[];append=f.a.checkpoint.append_epoch;preload=f.a.bridge.preload;attest=f.a.c.attest;lead=f.a.bridge.leadership.require_current;root=f.a.root_check
  f.a.checkpoint.append_epoch=lambda v:(events.append('journal'),append(v))[-1]
  f.a.bridge.leadership.require_current=lambda:(events.append('leadership'),lead())[-1]
  f.a.root_check=lambda:(events.append('root'),root())[-1]
  f.a.bridge.preload=lambda v:(events.append('preload'),preload(v))[-1]
  f.a.c.attest=lambda *a,**kw:(events.append('attest'),attest(*a,**kw))[-1]
  self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]))
  j=events.index('journal');p=events.index('preload');self.assertLess(j,p);self.assertIn('leadership',events[j+1:p]);self.assertIn('root',events[j+1:p]);self.assertLess(p,events.index('attest'))
