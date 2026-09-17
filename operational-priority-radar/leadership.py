from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from constants import LEASE_TTL_SECONDS, LEASE_RENEW_SECONDS, REDIS_NAMESPACE

LEADER_KEY = f"{REDIS_NAMESPACE}:runtime:leader"
GENERATION_KEY = f"{REDIS_NAMESPACE}:runtime:leader_generation"

class LeadershipError(RuntimeError): pass
class NotLeader(LeadershipError): pass
class StaleLeaderGeneration(LeadershipError): pass

@dataclass(frozen=True)
class LeadershipToken:
    worker_instance_id: str
    leader_generation: int

class InMemoryLeaseBackend:
    """Deterministic contract-test backend; production Redis comes later."""
    def __init__(self):
        self.owner: Optional[str] = None
        self.ttl = 0
        self.generation = 0

    def acquire(self, worker_id: str, ttl: int) -> Optional[int]:
        if self.owner is not None:
            return None
        self.owner = worker_id
        self.ttl = ttl
        self.generation += 1
        return self.generation

    def compare_and_renew(self, worker_id: str, ttl: int) -> bool:
        if self.owner != worker_id:
            return False
        self.ttl = ttl
        return True

    def compare_and_delete(self, worker_id: str) -> bool:
        if self.owner != worker_id:
            return False
        self.owner = None
        self.ttl = 0
        return True

    def expire_for_test(self) -> None:
        self.owner = None
        self.ttl = 0

    def current_owner(self) -> Optional[str]:
        return self.owner

    def current_generation(self) -> int:
        return self.generation

@dataclass
class LeadershipManager:
    backend: InMemoryLeaseBackend
    worker_instance_id: str
    token: Optional[LeadershipToken] = None

    def acquire(self) -> LeadershipToken:
        generation = self.backend.acquire(self.worker_instance_id, LEASE_TTL_SECONDS)
        if generation is None:
            raise NotLeader("leadership lease already held")
        self.token = LeadershipToken(self.worker_instance_id, generation)
        return self.token

    def renew(self) -> None:
        if self.token is None or not self.backend.compare_and_renew(self.worker_instance_id, LEASE_TTL_SECONDS):
            self.token = None
            raise NotLeader("cannot prove lease ownership")

    def release(self) -> None:
        if self.token is None:
            return
        if not self.backend.compare_and_delete(self.worker_instance_id):
            self.token = None
            raise NotLeader("refusing to delete another worker lease")
        self.token = None

    def require_current(self) -> LeadershipToken:
        if self.token is None:
            raise NotLeader("no leadership token")
        if self.backend.current_owner() != self.worker_instance_id:
            self.token = None
            raise NotLeader("lease ownership lost")
        if self.backend.current_generation() != self.token.leader_generation:
            raise StaleLeaderGeneration("stale fencing generation")
        return self.token

def validate_runtime_constants() -> None:
    if LEASE_TTL_SECONDS != 30 or LEASE_RENEW_SECONDS != 10:
        raise RuntimeError("leadership constants diverged from frozen spec")
    if LEASE_RENEW_SECONDS >= LEASE_TTL_SECONDS:
        raise RuntimeError("renewal interval must be shorter than lease TTL")
