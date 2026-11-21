"""R10.10b diagnostic sidecar. No network/client commands, no observer locks.
Producer lanes are per-thread bounded deques (CPython); only writer serializes/I/O.
Never retain client arguments, keys, values, HTTP bodies or arbitrary exceptions.
"""
import contextlib
import json
import math
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from restart_budget import UNTRUSTED_AFTER_ACK_SEC
from pathlib import PurePath

VERSION = "R10.10b-diag2"
MAX_LINE_BYTES = 4096
MAX_LINES_PER_MINUTE = 180
MAX_LINES_PER_WINDOW = 600
TAIL_SECONDS = 180
MAX_LANES = 32
LANE_CAPACITY = 64
_ACTIVE = None
_LOCAL = threading.local()
COMMANDS = frozenset(("SCAN", "MGET", "EVAL", "EVALSHA", "GET", "SET", "HGET", "TIME", "OTHER"))
PHASES = frozenset(("warmup", "journal.evidence", "journal.failed", "journal.resolve",
                    "preload", "attest", "leadership_root_clock", "journal.persist",
                    "journal.lock_wait", "canonical", "capture"))
FILES = frozenset(("runtime.py", "adapter.py", "live.py", "recovery_io.py",
                   "replan.py", "scan_health.py", "guards.py", "store.py"))
HOT = ("_active_by_symbol", "_entry_opportunities", "_entry_uncertain",
       "_structure_watch_symbols", "trades", "halted", "terminal_counts",
       "entry_exit_counts", "entry_exit_counts_by_symbol")


def enabled():
    try:
        from ipr.review_flags import IPR_RECOVERY_DIAG_ENABLED
        return IPR_RECOVERY_DIAG_ENABLED is True
    except Exception:
        return False


def _number(v):
    return type(v) in (int, float) and math.isfinite(v)


class Recorder:
    def __init__(self, runtime, sink=None, clock=None, wall=None):
        self.r = runtime
        self.clock = clock or time.monotonic
        self.wall = wall or (lambda: datetime.now(timezone.utc).isoformat())
        self.sink = sink or (lambda line: print(line, flush=True))
        self.lanes = {}
        self.writer_errors = self.observer_errors = 0
        self.lines = self.dropped_rate = self.dropped_size = 0
        self.bucket = None
        self.bucket_lines = 0
        self.written_at = deque()
        self.stopping = False
        self.thread = None
        self.restores = []
        self.attempt = 0
        self.last_warm = None
        self.cancel_observed = None
        self.last_timer_enqueue = None
        self.last_timer_drain = None
        self.ack_observed = None
        self.last_health = None
        self.window = None
        self.window_count = 0
        self.dropped_window = 0

    def in_window(self, now=None):
        window = self.window
        return window is not None and (window[1] is None or
                    (self.clock() if now is None else now) < window[1])

    def gate_transition(self, row):
        if (row.get('stage'), row.get('reason'), row.get('from'), row.get('to')) != (
                'SCAN_GATE_TRANSITION', 'TRUST_LOST', True, False):
            return
        # Only bounded scalar bookkeeping here; original gate owns its lock.
        self.window_count += 1
        self.window = [self.window_count, None, 0]

    def scan_aggregate(self, seconds):
        lane = self.lane()
        if lane is not None:
            aggregate = lane['outside_scan']
            aggregate['count'] += 1
            aggregate['seconds'] += seconds
            aggregate['max_seconds'] = max(aggregate['max_seconds'], seconds)

    def held(self):
        # Ownership query only; never acquire a business/observer lock.
        try:
            a = self.r.a
            for lock in (a.bridge.p._decision_lock, a.control_lock):
                if getattr(lock, "_is_owned", lambda: False)():
                    return True
            return False
        except Exception:
            return True  # unknown ownership => suppress
    def lane(self):
        ident = threading.get_ident()
        lane = self.lanes.get(ident)
        if lane is None:
            if len(self.lanes) >= MAX_LANES:
                return None
            lane = {"queue": deque(), "errors": 0, "dropped_queue": 0,
                    "skipped_live_lock": 0, "active_command": None, 'outside_scan': {'count':0,'seconds':0.,'max_seconds':0.}}
            self.lanes[ident] = lane
        return lane

    def safe(self, name, *a, **kw):
        try:
            if not enabled() or not self.in_window():
                return None
            if self.held():
                # Do not inspect/copy payloads or stack frames under live locks.
                lane = self.lane()
                if lane is not None:
                    lane["skipped_live_lock"] += 1
                return None
            return getattr(self, name)(*a, **kw)
        except Exception:
            self.observer_errors += 1
            return None

    def emit(self, stage, **fields):
        lane = self.lane()
        if lane is None:
            self.observer_errors += 1
            return
        if len(lane["queue"]) >= LANE_CAPACITY:
            lane["dropped_queue"] += 1
            return
        # Only our bounded primitive fields enter lanes; args/bodies never enter.
        row = {"stage": "R1010_DIAG_" + stage, "diag_version": VERSION,
               "observed_utc": self.wall(), "observed_monotonic": self.clock(),
               "boot": getattr(self.r, "boot_number", "UNKNOWN"),
               "session": getattr(self.r.a.bridge.p, "session", "UNKNOWN"),
               **fields}
        lane["queue"].append((row, self.window))

    def stats(self):
        lanes = tuple(self.lanes.values())
        return {"observer_errors": self.observer_errors,
                "writer_errors": self.writer_errors, "written_lines": self.lines,
                "dropped_rate": self.dropped_rate, "dropped_window": self.dropped_window,
                "window_line_limit": MAX_LINES_PER_WINDOW, "dropped_size": self.dropped_size,
                "dropped_queue": sum(x["dropped_queue"] for x in lanes),
                "skipped_live_lock": sum(x["skipped_live_lock"] for x in lanes),
                "line_limit_per_minute": MAX_LINES_PER_MINUTE,
                "max_line_bytes_including_newline": MAX_LINE_BYTES,
                "lane_count": len(lanes), "lane_limit": MAX_LANES,
                "lane_capacity": LANE_CAPACITY}

    def outside_summary(self):
        rows = [x['outside_scan'] for x in tuple(self.lanes.values())]
        return {'count':sum(x['count'] for x in rows),
                'seconds':sum(x['seconds'] for x in rows),
                'max_seconds':max((x['max_seconds'] for x in rows),default=0.),
                'scope':'outside recovery thread, cumulative recorder lifetime; no caller attribution invented'}

    def write(self, row, window=None):
        # Worker/test-drain only: never called by a producer.
        try:
            bucket = int(self.clock() // 60)
            now=self.clock()
            while self.written_at and now-self.written_at[0]>=60.:
                self.written_at.popleft()
            if bucket != self.bucket:
                self.bucket, self.bucket_lines = bucket, 0
            if len(self.written_at) >= MAX_LINES_PER_MINUTE:
                self.dropped_rate += 1
                return
            if window is not None:
                if window[2] >= MAX_LINES_PER_WINDOW:
                    self.dropped_window += 1
                    return
                row["window_id"] = window[0]
            text = json.dumps(row, allow_nan=False, separators=(",", ":"))
            if len(text.encode("utf-8")) + 1 > MAX_LINE_BYTES:
                self.dropped_size += 1
                return
            self.bucket_lines += 1  # includes failed sink attempts
            self.written_at.append(now)
            if window is not None:
                window[2] += 1
            self.sink(text)
            self.lines += 1
        except Exception:
            self.writer_errors += 1

    def drain(self):
        # No join/wait/acquire in callers. Production calls this only in writer.
        try:
            if not enabled():
                for lane in tuple(self.lanes.values()):
                    lane["queue"].clear()
                return
            now = self.clock()
            if self.last_health is None or now - self.last_health >= 60:
                self.last_health = now
                self.write({"stage": "R1010_DIAG_HEALTH", "diag_version": VERSION,
                            "observed_utc": self.wall(), 'outside_scan':self.outside_summary(), **self.stats()})
            for lane in tuple(self.lanes.values()):
                for _ in range(LANE_CAPACITY):
                    try:
                        row, window = lane["queue"].popleft()
                    except IndexError:
                        break
                    self.write(row, window)
        except Exception:
            self.writer_errors += 1

    def start(self):
        def work():
            while not self.stopping:
                time.sleep(.05)
                self.drain()
            self.drain()
        self.thread = threading.Thread(target=work, name="ipr-recovery-diag", daemon=True)
        self.thread.start()

    def close(self):
        # Signal only. Never join a potentially blocked stdout writer.
        try:
            self.stopping = True
            self.window = None
            for obj, name, original in reversed(self.restores):
                setattr(obj, name, original)
            self.r.a._ipr_recovery_diag=None
        except Exception:
            self.observer_errors += 1
        global _ACTIVE
        if _ACTIVE is self:
            _ACTIVE = None

    def wrap(self, obj, name, factory):
        original = getattr(obj, name)
        replacement = factory(original)
        setattr(obj, name, replacement)
        self.restores.append((obj, name, original))

    def begin(self, run):
        self.attempt += 1
        scope = {"id": self.attempt, "run": run, "start": self.clock(),
                 "window": self.window, "commands": {}, "phase": "capture", "last_phase": "capture",
                 "rest_count": 0, "rest_bytes": 0, "rest_bytes_known": True}
        _LOCAL.attempt = scope
        self.last_timer_drain = None
        # This phase wrapper is installed only on this attempt's private run.
        old = run.phase
        @contextlib.contextmanager
        def observed(name):
            previous = self.safe('phase_enter', scope, name)
            try:
                with old(name):
                    yield
            finally:
                self.safe('phase_exit', scope, previous)
        run.phase = observed
        self.emit("ATTEMPT_START", attempt_id=scope["id"],
                  start_monotonic=run.start, utc_start_observed=self.wall())

    def end(self, run):
        scope = getattr(_LOCAL, "attempt", None)
        if not scope or scope["run"] is not run:
            return
        try:
            if scope["window"] is not self.window:
                return
            if run.success and scope["window"][1] is None:
                scope["window"][1] = self.clock() + TAIL_SECONDS
            reason = run.cancel_reason
            fields = []
            if reason == "RECOVERY_HOT_STATE_CHANGED":
                try:
                    for name in HOT:
                        if getattr(run.a.bridge.p, name) != run.baseline[name]:
                            fields.append(name)
                except Exception:
                    fields = 'UNKNOWN'
                    self.observer_errors += 1
            timers = tuple(run.coord.timers)
            count = len(timers)
            # These are existing semantic timer timestamps, not enqueue clocks.
            stamp = [v for v in timers if isinstance(v, datetime)]
            ages = [(run.now-v).total_seconds() for v in stamp]
            since = (self.clock() - self.last_timer_drain["observed_monotonic"]
                     if self.last_timer_drain is not None else "UNKNOWN")
            self.emit("CANCEL_DETAILS", attempt_id=scope["id"],
                      reason=reason if reason in (
                          "RECOVERY_TIMER_CHANGED", "RECOVERY_HOT_STATE_CHANGED",
                          "RECOVERY_CANCELLED", "RECOVERY_PINS_CHANGED",
                          "RECOVERY_IO_UNDER_LIVE_LOCK", "RECOVERY_EPOCH_CHANGED",
                          "RECOVERY_CONNECTION_CHANGED", "RECOVERY_LEADERSHIP_CHANGED",
                          "RECOVERY_WITNESS_CHANGED", "RECOVERY_MEMBERSHIP_CHANGED") else ("NONE" if run.success else "OTHER"),
                      phase_observed=scope.get('failure_phase', scope["last_phase"]),
                      elapsed_seconds=self.clock() - scope["start"],
                      hot_fields_observed_after_failure=fields,
                      hot_field_change_time="UNKNOWN",
                      timer_class="expire_entry_opportunities", queued_timers_observed=count,
                      oldest_timer_age_at_attempt_now_seconds=max(ages) if ages else "UNKNOWN",
                      newest_timer_age_at_attempt_now_seconds=min(ages) if ages else "UNKNOWN",
                      timer_age_scope="semantic now vs timer timestamps, not exact queue residence",
                      final_validation_monotonic="UNKNOWN",
                      last_timer_enqueue=self.last_timer_enqueue,
                      last_timer_drain=self.last_timer_drain,
                      since_last_drain_observation_seconds=since,
                      cancel_invalidator_observation=self.cancel_observed,
                      missing_observation_reason="live-lock producers are skipped; not exact mutation trace",
                      **({'membership_changes': run.membership_changes}
                         if reason == 'RECOVERY_MEMBERSHIP_CHANGED' else {}))
            self.emit("ATTEMPT_SUMMARY", attempt_id=scope["id"], success=bool(run.success),
                      elapsed_seconds=self.clock() - scope["start"],
                      client_command_counts_and_seconds=scope["commands"],
                      counter_scope="RedisTelemetry calls in recovery thread only; phases below are separate, never added twice",
                      io_proxy_scan=run.io.get("redis.scan", "UNKNOWN"),
                      io_proxy_mget=run.io.get("redis.mget", "UNKNOWN"),
                      io_proxy_eval=run.io.get("redis.eval", "UNKNOWN"),
                      warmup=run.io.get("warmup", "UNKNOWN"),
                      rest_requests=scope["rest_count"],
                      rest_response_bytes=scope["rest_bytes"] if scope["rest_bytes_known"] else "UNKNOWN")
        finally:
            _LOCAL.attempt = None

    def command_begin(self, name):
        command = name.upper() if name.upper() in COMMANDS else 'OTHER'
        lane = self.lane()
        if lane is None: return None
        concurrent = {}
        for other in tuple(self.lanes.values()):
            active = other['active_command']
            if active:
                concurrent[active] = concurrent.get(active, 0) + 1
        previous = lane['active_command']
        token = {'lane': lane, 'previous': previous, 'monotonic': self.clock(),
                 'utc': self.wall(), 'other_inflight': concurrent, 'window': self.window}
        lane['active_command'] = command
        return token

    def command(self, name, seconds, args, kw, result=None, token=None):
        if token is not None:
            token['lane']['active_command'] = token['previous']
            if token['window'] is not self.window:
                return
        command = name.upper() if name.upper() in COMMANDS else 'OTHER' 
        scope = getattr(_LOCAL, "attempt", None)
        if scope is not None:
            row = scope["commands"].setdefault(command, {"count": 0, "seconds": 0., "max_seconds": 0.})
            row["count"] += 1
            row["seconds"] += seconds
            row["max_seconds"] = max(row["max_seconds"], seconds)
        # No raw arguments retained/emitted. Pattern mapped to a fixed enum.
        pattern = kw.get("match", args[1] if len(args) > 1 and command == "SCAN" else None)
        klass = "OTHER"
        if type(pattern) is str:
            if pattern.endswith(":trade:*"): klass = "CANONICAL_TRADE"
            elif pattern.endswith(":opportunity:*"): klass = "CANONICAL_OPPORTUNITY"
            elif pattern.endswith(":outbox:*"): klass = "OUTBOX"
        if command in ('SCAN', 'MGET'):
            details = {
                'start_monotonic': token['monotonic'] if token else 'UNKNOWN',
                'end_monotonic': self.clock(),
                'start_utc': token['utc'] if token else 'UNKNOWN',
                'end_utc': self.wall(),
                'other_inflight_at_start': token['other_inflight'] if token else 'UNKNOWN',
                'inflight_scope': 'observed client wrappers only; live-lock commands unobserved',
                'pool_wait_observed': 'UNKNOWN', 'pool_wait_seconds': 'UNKNOWN',
                'timestamp_scope': 'before client invocation / after original RTT bookkeeping; observer times',
            }
        if command == 'MGET' and scope is not None:
            keys = args[0] if args else ()
            self.emit('MGET', attempt_id=scope['id'],
                      command_number=scope['commands']['MGET']['count'],
                      requested_keys=len(keys) if type(keys) in (list, tuple) else 'UNKNOWN',
                      seconds=seconds, **details)
        if command == "SCAN" and scope is None:
            self.scan_aggregate(seconds)
        if command == "SCAN" and scope is not None:
            returned = "UNKNOWN"
            if isinstance(result, (tuple, list)) and len(result) == 2:
                returned = len(result[1]) if isinstance(result[1], (tuple, list)) else "UNKNOWN"
            self.emit("SCAN", attempt_id=scope["id"] if scope else None,
                      scope="RECOVERY" if scope else "OUTSIDE_RECOVERY",
                      pattern_class=klass, seconds=seconds,
                      count_hint=kw.get("count", 500) if _number(kw.get("count", 500)) else "UNKNOWN",
                      returned_keys=returned,
                      command_number=scope['commands']['SCAN']['count'] if scope else 'UNKNOWN',
                      cursor_after=('ZERO' if result[0] in (0, '0', b'0') else 'NONZERO')
                          if type(result) in (list, tuple) and len(result)==2 else 'UNKNOWN',
                      server_execution_seconds="UNKNOWN", **details)
        journal = getattr(_LOCAL, "journal", None)
        if journal is not None and command in ("EVAL", "EVALSHA"):
            # UTF8 payload size only; no value/script/key survives this call.
            sizes = [len(v.encode("utf-8")) for v in args if type(v) is str]
            journal["eval_utf8_argument_bytes"] += sum(sizes)
            journal["eval_count"] += 1

    def phase(self, name, seconds):
        journal = getattr(_LOCAL, "journal", None)
        if journal is not None and name in ("journal.lock_wait", "journal.persist"):
            journal[name] += seconds
            journal[name+'_observed'] = True

    def bind(self, api=None):
        a = self.r.a
        a._ipr_recovery_diag = self
        gate = getattr(a, "scan_gate", None)
        if gate is not None:
            def observe_gate(original):
                def log(row):
                    result = original(row)
                    try:
                        self.gate_transition(row)
                    except Exception:
                        self.observer_errors += 1
                    return result
                return log
            self.wrap(gate, "log", observe_gate)
        coord = getattr(a, "_recovery_io", None)
        if coord is not None:
            def expire_factory(original):
                def expire(now):
                    before = self.safe('expire_before', coord)
                    value = original(now)
                    self.safe('expired', coord, before, now)
                    return value
                return expire
            self.wrap(coord, "expire", expire_factory)
            # Coordinator replaced pipeline timer with its original bound method before install.
            self.wrap(a.bridge.p, "expire_entry_opportunities", lambda old: coord.expire)
            def drain_factory(original):
                def drain():
                    value = original()
                    self.safe("drained", value)
                    return value
                return drain
            self.wrap(coord, "drain", drain_factory)
        def close_factory(original):
            def close(reason):
                before = self.safe('before_close')
                value = original(reason)
                self.safe("closed", before)
                return value
            return close
        self.wrap(a, "close_eligibility", close_factory)
        def warm_factory(original):
            def warm(symbols, now, required):
                before = self.safe('before_warm', symbols, now, required)
                try:
                    return original(symbols, now, required)
                finally:
                    self.safe('after_warm', before)
            return warm
        self.wrap(a.rest, "warmup", warm_factory)
        state = a.checkpoint
        def journal_factory(original):
            def append(event, *aa, **kw):
                before = self.safe('before_journal')
                try:
                    return original(event, *aa, **kw)
                finally:
                    self.safe('after_journal', before)
            return append
        self.wrap(state, "append_epoch", journal_factory)
        deadline = self.r.deadline
        def ack_factory(original):
            def ack():
                value = original()
                self.safe("acked")
                return value
            return ack
        self.wrap(deadline, "ack", ack_factory)
        def tick_factory(original):
            def tick(trusted):
                try:
                    value=original(trusted)
                except Exception:
                    self.safe('deadline_tick', trusted, True)
                    raise
                self.safe('deadline_tick', trusted, False)
                return value
            return tick
        self.wrap(deadline, 'tick', tick_factory)
        if api is not None and hasattr(api, "call") and hasattr(api, "opener"):
            self.bind_api(api)
        self.safe("emit", "INSTALLED", enabled=True, mode="REVIEW_DIAGNOSTICS_ONLY")

    def phase_enter(self, scope, name):
        previous = scope['phase']
        scope['phase'] = name if name in PHASES else 'canonical'
        scope['last_phase'] = scope['phase']
        return previous

    def phase_exit(self, scope, previous):
        scope['phase'] = previous or 'canonical'

    def failure(self, run):
        scope = getattr(_LOCAL, 'attempt', None)
        if scope is not None:
            scope['failure_phase'] = ('final_validate_hot' if run.cancel_reason in (
                'RECOVERY_TIMER_CHANGED', 'RECOVERY_HOT_STATE_CHANGED',
                'RECOVERY_PINS_CHANGED', 'RECOVERY_WITNESS_CHANGED')
                else scope['last_phase'])

    def expire_before(self, coord):
        return len(coord.timers)

    def expired(self, coord, before, now):
        if type(before) is int and len(coord.timers) > before:
            self.last_timer_enqueue = {
                'timer_class': 'expire_entry_opportunities',
                'observed_monotonic': self.clock(),
                'source_time': now.isoformat() if isinstance(now, datetime) else 'UNKNOWN',
                'scope': 'after enqueue returned; exact enqueue instant UNKNOWN'}

    def before_warm(self, symbols, now, required):
        scope = getattr(_LOCAL, 'attempt', None)
        if scope is None: return None
        key = (frozenset(symbols), now, required)
        before = (scope, self.clock(), scope['rest_count'], scope['rest_bytes'],
                  len(symbols), self.last_warm == key)
        self.last_warm = key
        return before

    def after_warm(self, before):
        if before is None: return
        scope, start, r0, b0, symbols, same = before
        self.emit('WARMUP', attempt_id=scope['id'], symbols=symbols,
                  seconds=self.clock()-start, actual_rest_requests=scope['rest_count']-r0,
                  response_bytes=scope['rest_bytes']-b0 if scope['rest_bytes_known'] else 'UNKNOWN',
                  recomputed=True, identical_request_identity_to_previous=same,
                  exact_batches_scope='REST_REQUEST records; body bytes, no wire accounting')

    def before_journal(self):
        j = {'journal.lock_wait': 0., 'journal.persist': 0.,
             'eval_utf8_argument_bytes': 0, 'eval_count': 0}
        before = (getattr(_LOCAL, 'journal', None), j, self.clock())
        _LOCAL.journal = j
        return before

    def after_journal(self, before):
        if before is None: return
        previous, j, start = before
        _LOCAL.journal = previous
        scope = getattr(_LOCAL, 'attempt', None)
        self.emit('JOURNAL', attempt_id=scope['id'] if scope else None,
                  operation_class='journal.evidence' if scope and scope['phase']=='journal.evidence' else 'journal.persist',
                  elapsed_seconds=self.clock()-start,
                  lock_wait_seconds=j['journal.lock_wait'] if j.get('journal.lock_wait_observed') else 'UNKNOWN',
                  persist_seconds=j['journal.persist'] if j.get('journal.persist_observed') else 'UNKNOWN',
                  payload_utf8_argument_bytes=j['eval_utf8_argument_bytes'] if j['eval_count'] else 'UNKNOWN',
                  payload_scope='existing EVAL string arguments UTF8 incl script/keys; no RESP framing')

    def before_close(self):
        old = self.r.a.cancel_event
        return old, old.is_set(), self.r.a.transport_trusted

    def drained(self, value):
        self.last_timer_drain = {"timer_class": "expire_entry_opportunities",
                                "count": len(value), "observed_monotonic": self.clock(),
                                "scope": "after original drain returned"}
    def acked(self):
        armed = self.r.deadline.armed
        self.ack_observed = {"armed_monotonic": armed,
                             "deadline_monotonic": armed + UNTRUSTED_AFTER_ACK_SEC if _number(armed) else "UNKNOWN",
                             "ack_observed_utc": self.wall(),
                             "ack_utc_exact": "UNKNOWN", "timeout_seconds": UNTRUSTED_AFTER_ACK_SEC}
        self.emit("ACK_DEADLINE", **self.ack_observed)

    def deadline_tick(self, trusted, raised):
        if raised:
            self.emit('ACK_TIMEOUT_OBSERVED', deadline=self.ack_observed or 'UNKNOWN',
                      armed_monotonic=self.r.deadline.armed,
                      exact_tick_time='UNKNOWN; original guard clock not called again')

    def closed(self, before):
        if before is None: return
        old, old_was_set, was_trusted = before
        if old is not self.r.a.cancel_event or (not old_was_set and self.r.a.cancel_event.is_set()):
            # This is an after-call observation; no stack/frame locals retained.
            frame = sys._getframe(2)
            caller = "UNKNOWN"
            filename = "UNKNOWN"
            for _ in range(6):
                if frame is None: break
                name = PurePath(frame.f_code.co_filename).name
                fn = frame.f_code.co_name
                if name in FILES and fn not in ("close", "closed", "safe", "revoke", "close_eligibility"):
                    caller, filename = fn, name
                    break
                frame = frame.f_back
            self.cancel_observed = {"caller_function": caller, "caller_file": filename,
                                    "observed_monotonic": self.clock(),
                                    "observed_utc": self.wall(), "invalidation_exact_time": "UNKNOWN"}
        if was_trusted and not self.r.a.transport_trusted:
            self.emit("TRUST_LOST", deadline=self.ack_observed or "UNKNOWN",
                      lost_observed_utc=self.wall(), cancellation=self.cancel_observed or "UNKNOWN")

    def bind_api(self, api):
        recorder = self
        class Response:
            def __init__(self, raw, info): self.raw, self.info = raw, info
            def __getattr__(self, n): return getattr(self.raw, n)
            def __enter__(self):
                returned = self.raw.__enter__()
                try:return Response(returned, self.info)
                except Exception:
                    recorder.observer_errors+=1
                    return returned
            def __exit__(self, *a): return self.raw.__exit__(*a)
            def read(self, *a, **kw):
                value = self.raw.read(*a, **kw)  # the original single read, no extra I/O
                recorder.safe('body_read', self.info, value)
                return value
        def opener_factory(original):
            def opener(*a, **kw):
                value = original(*a, **kw)
                info = getattr(_LOCAL, "rest_request", None)
                try:return Response(value, info) if info is not None else value
                except Exception:
                    recorder.observer_errors+=1
                    return value
            return opener
        def call_factory(original):
            def call(host, path, params):
                before = recorder.safe('before_rest')
                try:
                    return original(host, path, params)
                finally:
                    recorder.safe('after_rest', before, params)
            return call
        self.wrap(api, "opener", opener_factory)
        self.wrap(api, "call", call_factory)

    def before_rest(self):
        scope=getattr(_LOCAL,'attempt',None)
        if scope is None: return None
        before=(scope,{'bytes':0,'known':False},getattr(_LOCAL,'rest_request',None),self.clock())
        _LOCAL.rest_request=before[1]
        return before

    def body_read(self, info, value):
        if info is not None and type(value) is bytes:
            info['bytes']+=len(value);info['known']=True

    def after_rest(self, before, params):
        if before is None:return
        scope,info,previous,start=before
        _LOCAL.rest_request=previous
        scope['rest_count']+=1;scope['rest_bytes']+=info['bytes']
        scope['rest_bytes_known'] &= info['known']
        tf=params.get('timeframe') if type(params) is dict else None
        self.emit('REST_REQUEST',attempt_id=scope['id'],request_number=scope['rest_count'],
                  request_class=tf if tf in ('1Min','5Min') else 'OTHER',
                  seconds=self.clock()-start,body_bytes=info['bytes'] if info['known'] else 'UNKNOWN',
                  page='PAGINATED' if type(params) is dict and params.get('page_token') else 'FIRST',
                  scope='one original call incl REST lock/HTTP/read/JSON; not pure HTTP time')


def safe_global(name, *args):
    recorder = _ACTIVE
    if recorder is not None:
        return recorder.safe(name, *args)


def safe_begin(run):
    recorder = getattr(run.a, "_ipr_recovery_diag", None)
    if recorder is not None:
        recorder.safe("begin", run)


def safe_end(run):
    recorder = getattr(run.a, "_ipr_recovery_diag", None)
    try:
        if recorder is not None:
            recorder.safe("end", run)
    finally:
        _LOCAL.attempt=None


def install(runtime, api=None, sink=None, clock=None, wall=None, start_writer=True):
    global _ACTIVE
    if not enabled(): return None
    recorder = None
    try:
        recorder = Recorder(runtime, sink=sink, clock=clock, wall=wall)
        recorder.bind(api)
        _ACTIVE = recorder
        if start_writer:recorder.start()
        return recorder
    except Exception:
        if recorder is not None:
            recorder.observer_errors += 1
            recorder.close()
        return None

