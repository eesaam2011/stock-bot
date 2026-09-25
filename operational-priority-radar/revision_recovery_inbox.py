"""One bounded revision handoff slot; a second distinct revision blocks replay."""
from copy import deepcopy
import json
import threading
from sip_semantic_digest import canonical_sha256


class RevisionInboxUnsafe(RuntimeError):
    pass


class RevisionRecoveryInbox:
    def __init__(self):
        self._lock=threading.RLock()
        self._value=None
        self._blocked=None

    def observe_terminal(self, terminal):
        if not isinstance(terminal,dict):return
        capture=terminal.get('capture_before_teardown')
        if not isinstance(capture,dict) or 'revision_diagnostic' not in capture:return
        with self._lock:
            if self._blocked:return
            value=capture['revision_diagnostic']
            try:
                if not isinstance(value,dict):raise ValueError()
                # Terminal diagnostics are allowlisted upstream. Enforce an
                # independent serialized limit before retaining the handoff.
                if len(json.dumps(value,allow_nan=False).encode())>16384:raise ValueError()
                body=dict(value);digest=body.pop('diagnostic_sha256',None)
                epoch=value.get('epoch')
                if (type(epoch) is not int or epoch<1 or epoch!=terminal.get('epoch')
                    or not digest or canonical_sha256(body)!=digest):raise ValueError()
            except (ValueError,TypeError,OverflowError):
                self._blocked='REVISION_HANDOFF_INVALID';return
            if self._value is not None:
                if self._value['diagnostic_sha256']!=digest:
                    self._blocked='REVISION_HANDOFF_MULTIPLE_UNRESOLVED'
                return
            self._value=deepcopy(value)

    def snapshot(self):
        with self._lock:
            if self._blocked:raise RevisionInboxUnsafe(self._blocked)
            if self._value is None:raise RevisionInboxUnsafe('REVISION_HANDOFF_EMPTY')
            return deepcopy(self._value)

    def require_unchanged(self, digest):
        value=self.snapshot()
        if value['diagnostic_sha256']!=digest:
            raise RevisionInboxUnsafe('REVISION_HANDOFF_CHANGED')
