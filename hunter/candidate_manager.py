from datetime import datetime, timedelta

from shared.config import CANDIDATE_EXPIRE_HOURS

def get_current_time():
    return datetime.utcnow() 

def build_candidate_record(
    symbol,
    source,
    candidate_score,
    reason,
):
    now = get_current_time()
    expires_at = now + timedelta(hours=CANDIDATE_EXPIRE_HOURS)

    return {
        "symbol": str(symbol).upper().strip(),
        "source": source,
        "candidate_score": candidate_score,
        "reason": reason,
        "is_active_now": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

def update_candidate_record(
    candidate,
    reason,
    is_active_now=True,
):
    now = get_current_time()

    candidate["reason"] = reason
    candidate["is_active_now"] = is_active_now
    candidate["updated_at"] = now.isoformat()
    candidate["last_seen_at"] = now.isoformat()

    return candidate

def is_candidate_expired(candidate):
    expires_at = candidate.get("expires_at")

    if not expires_at:
        return True

    expires_at = datetime.fromisoformat(expires_at)

    return get_current_time() > expires_at

def get_candidate_age_minutes(candidate):
    created_at = candidate.get("created_at")

    if not created_at:
        return None

    created_at = datetime.fromisoformat(created_at)
    age = get_current_time() - created_at

    return int(age.total_seconds() / 60)
