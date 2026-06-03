import os
import json
import requests

from hunter.candidate_manager import is_candidate_expired


REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
REDIS_CANDIDATES_KEY = "hunter_candidates"


def redis_headers():
    return {
        "Authorization": f"Bearer {REDIS_TOKEN}",
        "Content-Type": "application/json",
    }


def redis_ready():
    return bool(REDIS_URL and REDIS_TOKEN)


def save_candidate(candidate):
    if not redis_ready():
        print("❌ Redis settings are missing", flush=True)
        return False

    symbol = candidate.get("symbol")

    if not symbol:
        return False

    symbol = str(symbol).upper().strip()

    try:
        response = requests.post(
            REDIS_URL,
            headers=redis_headers(),
            data=json.dumps([
                "HSET",
                REDIS_CANDIDATES_KEY,
                symbol,
                json.dumps(candidate),
            ]),
            timeout=10,
        )
    except Exception:
        return False

    return response.status_code == 200
    
def get_candidate(symbol):
    if not redis_ready():
        print("❌ Redis settings are missing", flush=True)
        return None

    symbol = str(symbol).upper().strip()

    url = f"{REDIS_URL}/hget/{REDIS_CANDIDATES_KEY}/{symbol}"

    try:
        response = requests.get(
            url,
            headers=redis_headers(),
            timeout=10,
        )
    except Exception:
        return None

    if response.status_code != 200:
        return None

    data = response.json().get("result")

    if not data:
        return None

    try:
        return json.loads(data)
    except Exception:
        return None


def delete_candidate(symbol):
    if not redis_ready():
        print("❌ Redis settings are missing", flush=True)
        return False

    symbol = str(symbol).upper().strip()

    url = f"{REDIS_URL}/hdel/{REDIS_CANDIDATES_KEY}/{symbol}"

    try:
        response = requests.post(
            url,
            headers=redis_headers(),
            timeout=10,
        )
    except Exception:
        return False

    return response.status_code == 200


def get_all_candidates():
    if not redis_ready():
        print("❌ Redis settings are missing", flush=True)
        return []

    url = f"{REDIS_URL}/hgetall/{REDIS_CANDIDATES_KEY}"

    try:
        response = requests.get(
            url,
            headers=redis_headers(),
            timeout=10,
        )
    except Exception:
        return []

    if response.status_code != 200:
        return []

    result = response.json().get("result")

    if not result:
        return []

    candidates = []

    for i in range(0, len(result), 2):
        candidate_data = result[i + 1]

        try:
            candidates.append(json.loads(candidate_data))
        except Exception:
            continue

    return candidates


def delete_expired_candidates():
    candidates = get_all_candidates()
    deleted_count = 0

    for candidate in candidates:
        if is_candidate_expired(candidate):
            symbol = candidate.get("symbol")

            if symbol and delete_candidate(symbol):
                deleted_count += 1

    return deleted_count

