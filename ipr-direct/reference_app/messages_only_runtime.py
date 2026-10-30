"""Opt-in wiring, disabled by default. Existing shadow/trust/decisions stay intact.
All ledger/HTTP work is on one bounded worker thread, never inside asyncio.
"""
import asyncio,concurrent.futures,types,os,time
PENDING_INTERVAL_SECONDS=5.0
from datetime import datetime,timezone
from messages_only import MessageConfig,RedisMessageLedger,MessagesOnlySender

def install_messages_only(supervisor,policy,env=None,*,sender_factory=MessagesOnlySender):
    env=os.environ if env is None else env
    config=MessageConfig.from_env(env)
    if not config.enabled:return None
    if env.get('OPR_SHADOW_MODE','true')!='true':raise ValueError('MESSAGES_ONLY_REQUIRES_SHADOW')
    mode=env.get('OPR_MESSAGES_MODE','TEST_ONLY')
    if mode not in {'TEST_ONLY','MESSAGES_ONLY'}:raise ValueError('MESSAGE_MODE_INVALID')
    test_flag='true' if mode=='TEST_ONLY' else env.get('OPR_MESSAGES_TEST_ON_START','false')
    if test_flag not in ('true','false'):raise ValueError('MESSAGE_TEST_FLAG_INVALID')
    executor=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='opr-messages-only')
    async def loop(s):
        trusted_since=None;tested=False;cap_closed=False;seen_terminal=set();next_pending=0.0
        async def work(callback):return await asyncio.get_running_loop().run_in_executor(executor,callback)
        try:
            while not s.stop_event.is_set():
                if not s.orchestrator.new_decisions_allowed():trusted_since=None
                elif not cap_closed:
                    if trusted_since is None:trusted_since=datetime.now(timezone.utc)
                    # Keep trust observation at 1s; only the expensive pending reader is 5s.
                    due=time.monotonic()>=next_pending
                    if not due:
                        try:await asyncio.wait_for(s.stop_event.wait(),timeout=1)
                        except asyncio.TimeoutError:pass
                        continue
                    next_pending=time.monotonic()+PENDING_INTERVAL_SECONDS
                    # Generation/owner are also checked atomically by the ledger.
                    client=s.orchestrator.redis.r
                    generation=await work(lambda:client.get(s.orchestrator.redis.generation_key))
                    if isinstance(generation,bytes):generation=generation.decode()
                    ledger=RedisMessageLedger(client,config,policy,s.orchestrator.worker_id,generation)
                    sender=sender_factory(config,policy,ledger)
                    if test_flag=='true' and not tested:
                        tested=True
                        await work(lambda:sender.send({},test=True))
                        cap_closed=sender.cap_reached or mode=='TEST_ONLY'
                    events=[] if cap_closed else await work(s.outbox_store.pending)
                    # At most one in-flight job; cap prevents HTTP floods, NOT scans.
                    for event in events:
                        if s.stop_event.is_set() or not s.orchestrator.new_decisions_allowed():break
                        logical_key=(event.get('session'),event.get('symbol'),event.get('event_type'))
                        if logical_key in seen_terminal:continue
                        result=await work(lambda e=event:sender.send(e,trusted=True,not_before=trusted_since))
                        if result in {'SENT','FAILED_FINAL','UNKNOWN_FINAL'}:seen_terminal.add(logical_key)
                        cap_closed=sender.cap_reached
                        if result in {'POLICY_CONFLICT','LEDGER_INVALID','LEDGER_MISSING','NOT_LEADER'}:
                            cap_closed=True
                            print({'stage':'MESSAGE_DELIVERY_PARKED','reason':result},flush=True)
                        if cap_closed:break
                try:await asyncio.wait_for(s.stop_event.wait(),timeout=1)
                except asyncio.TimeoutError:pass
        finally:
            executor.shutdown(wait=False,cancel_futures=True)
    supervisor.outbox_loop=types.MethodType(loop,supervisor)
    return config
