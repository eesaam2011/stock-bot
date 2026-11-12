async def native():
    while not r.stop.is_set():
        market = bundle['market']
        now = r.wall()
        if state['ready'] and state['scan_ok'] and market and (dt(market['open']) <= now < dt(market['close'])) and r.a.transport_trusted:
            with r.a.control_lock:
                syms = [s for s in r.c.bar if s not in r.a.recovering and all((r.c.proofs.get(s, {}).get(k) for k in ('history', 'continuity', 'reconciled')))]
            if syms:

                def native_work():
                    with api.operation('native'):
                        return r.a.bridge.native_cycle(syms, now, True)
                await asyncio.to_thread(native_work)
        await pause(r.stop, 5.0)
