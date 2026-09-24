"""Offline EHR G5 requirements preflight; does not contact Alpaca or Redis."""
import argparse
import hashlib
import json
from pathlib import Path

def verify(path):
    raw=path.read_bytes()
    data=json.loads(raw)
    req=data["requirements"]
    if len(req)!=583 or len({r["symbol"] for r in req})!=376:
        raise ValueError("Unexpected G5 cohort size; expected 583 cases / 376 symbols")
    ids=[r["case_id"] for r in req]
    if len(set(ids))!=583:
        raise ValueError("Duplicate G5 case_id")
    if data.get("status")!="REQUIREMENTS_ONLY_PRICES_NOT_PRESENT":
        raise ValueError("Wrong G5 requirements format")
    if data.get("summary",{}).get("last_required_exit_H20")!="2026-09-28":
        raise ValueError("Unexpected frozen H20 horizon")
    if any(not r.get("next_regular_entry_session") or not r.get("required_exit_sessions") for r in req):
        raise ValueError("Missing entry or exit dates")
    return {"cases":len(req),"symbols":len({r["symbol"] for r in req}),
            "first_entry":min(r["next_regular_entry_session"] for r in req),
            "last_H20":max(r["required_exit_sessions"]["20"] for r in req),
            "file_sha256":hashlib.sha256(raw).hexdigest(),
            "ready_for_collection":True,"returns_computed":False}

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("requirements",type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.requirements),indent=2))
