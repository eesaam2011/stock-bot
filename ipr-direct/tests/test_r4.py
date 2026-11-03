import asyncio,contextlib,io,json,unittest
from unittest.mock import Mock
from datetime import timedelta
from dintegration.clock import ClockUnavailable
import test_r3
from test_direct import OP

class R4Tests(unittest.TestCase):
 def deferred(self):
  f=test_r3.trusted();f.now[0]+=timedelta(seconds=1);test_r3.receive(f,'Y',0);f.r.wake.clear();return f
 def test_rejection_cooldown_exact_boundary(self):
  f=self.deferred();f.history['Y'][0]['v']=999
  self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertEqual(f.a.rejection_until['Y'],30.)
  initial=len(f.calls);f.mono[0]=29.999
  with contextlib.redirect_stdout(io.StringIO()) as out:self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]))
  self.assertEqual(len(f.calls),initial);self.assertEqual(json.loads(out.getvalue())['rejection_reasons']['REJECTION_COOLDOWN'],1);self.assertIn('Y',f.a.recovering)
  f.mono[0]=30.;f.history['Y'][0]['v']=100
  self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]));self.assertNotIn('Y',f.a.recovering)
 def test_clock_at_each_precommit_check_defers_without_journal(self):
  for fail_at in (1,2,3):
   with self.subTest(fail_at=fail_at):
    f=self.deferred();n=[0];before=len(f.epochs)
    def checks():
     n[0]+=1
     if n[0]==fail_at:raise ClockUnavailable('fixture')
    f.a.checks=checks
    with contextlib.redirect_stdout(io.StringIO()) as out:self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]))
    self.assertTrue(f.a.transport_trusted);self.assertEqual(len(f.epochs),before);self.assertIn('Y',f.a.recovering);self.assertNotIn('Y',f.a.rejection_until)
    self.assertIn('INCREMENTAL_ADMISSION_DEFERRED',out.getvalue());self.assertFalse(f.a.c.proofs['Y']['continuity'])
    f.a.checks=lambda:None;self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]))
 def test_leader_still_fatal(self):
  f=self.deferred();f.a.checks=Mock(side_effect=RuntimeError('LEASE_LOST'))
  with self.assertRaises(RuntimeError):f.a.admit_incremental(['Y'],f.now[0])
  self.assertFalse(f.a.transport_trusted)
 def test_499_deferred_real_adapter_preserves_other_trusted_symbol(self):
  async def go():
   f=test_r3.setup(tuple('S%03d'%i for i in range(500)));f.now[0]=OP+timedelta(seconds=61)
   test_r3.receive(f,'S000',0);self.assertTrue(f.a.recover(f.now[0]));original=dict(f.a.c.proofs['S000']);f.r.wake.clear()
   for s in list(f.a.recovering):test_r3.receive(f,s,0)
   original_admit=f.a.admit_incremental
   def admit(ss,now):
    result=original_admit(ss,now);self.assertTrue(f.a.transport_trusted);self.assertEqual(f.a.c.proofs['S000'],original);f.mono[0]+=1;return result
   f.a.admit_incremental=admit;task=asyncio.create_task(f.r.recovery_loop());f.r.wake.set()
   try:
    await test_r3.until(lambda:not f.a.recovering);self.assertEqual(f.r.incremental_attempts,5);self.assertEqual([len(c['symbols']) for c in f.calls[1:]],[100,100,100,100,99]);self.assertTrue(f.a.transport_trusted)
   finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())
 def test_500_deferred_scheduler_five_batches_simulation(self):
  # Scheduler-only 500 deferred plus independent global-trust sentinel.
  # Real active BAR scope remains capped at 500, tested separately above.
  async def go():
   f=test_r3.setup();ss={'D%03d'%i for i in range(500)};f.a.recovering=ss.copy();f.a.c.bar=ss;f.a.proof.witnessed=lambda now:ss
   from unittest.mock import PropertyMock,patch
   sizes=[]
   def admit(batch,now):
    self.assertTrue(f.a.transport_trusted);sizes.append(len(batch));f.a.recovering.difference_update(batch);f.mono[0]+=1
   f.a.admit_incremental=admit
   with patch.object(type(f.a),'transport_trusted',new_callable=PropertyMock,return_value=True):
    task=asyncio.create_task(f.r.recovery_loop());f.r.wake.set()
    try:
     await test_r3.until(lambda:not f.a.recovering);self.assertEqual(sizes,[100]*5);self.assertEqual(f.r.attempts,0)
    finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())
