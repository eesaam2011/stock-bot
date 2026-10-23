"""Offline strict policy generator/validator. No app decisions, network, or writes at import.
Templates are deliberately not executable policies. Frozen files are read, never updated.
"""
import ast,hashlib,json,os
from pathlib import Path
from datetime import datetime,timezone
from restart_budget import SessionPolicy

APP=Path(__file__).resolve().parent
FIELDS={'session','start_utc','end_utc','frozen_session','frozen_manifest_sha256','frozen_count'}

def frozen_identity(app=APP):
    # Parse literal pins without importing any original app module in the guard.
    tree=ast.parse((app/'frozen_pins.py').read_text())
    pins=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PINS' for t in n.targets))
    lines=[]
    for name,expected in sorted(pins.items()):
        actual=hashlib.sha256((app/'frozen'/name).read_bytes()).hexdigest()
        if actual!=expected:raise ValueError('FROZEN_PIN_MISMATCH:'+name)
        lines.append(actual+'  '+name+'\n')
    day=json.loads((app/'frozen/DAY_LIST.json').read_text())
    return day,hashlib.sha256(''.join(lines).encode()).hexdigest()

def read_bound_policy(text,env=None,app=APP):
    env=os.environ if env is None else env
    data=json.loads(text)
    if not isinstance(data,dict) or set(data)!=FIELDS:raise ValueError('POLICY_FIELDS_INVALID')
    p=SessionPolicy(**data).validate(env)
    day,digest=frozen_identity(app)
    if p.frozen_session!=p.session or day['session']!=p.session:raise ValueError('FROZEN_SESSION_MISMATCH')
    if digest!=p.frozen_manifest_sha256:raise ValueError('FROZEN_MANIFEST_MISMATCH')
    # Reuse the original filter's day/eligibility rules only in the generator;
    # runtime guard validates the reviewed count by that same stdlib-only module.
    from frozen_static_scope import FrozenSubscriptionUniverse
    scope=FrozenSubscriptionUniverse(app/'frozen',p.session)
    state=scope.engine.day_state(p.session) if scope.engine else {}
    if scope.error or not state.get('allowed') or state.get('age_sessions')!=0:raise ValueError('FRESH_SCOPE_REQUIRED')
    if type(p.frozen_count) is not int or p.frozen_count!=len(scope.static) or not scope.static:raise ValueError('FROZEN_COUNT_MISMATCH')
    return p

def generate(session,start,end,*,now=None,env=None,app=APP):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset().total_seconds()!=0:raise ValueError('UTC_REQUIRED')
    if session!=now.date().isoformat():raise ValueError('POLICY_MUST_BE_FOR_TODAY_UTC')
    day,digest=frozen_identity(app)
    if day['session']!=session:raise ValueError('FROZEN_SESSION_MISMATCH')
    from frozen_static_scope import FrozenSubscriptionUniverse
    scope=FrozenSubscriptionUniverse(app/'frozen',session)
    data=dict(session=session,start_utc=start,end_utc=end,frozen_session=day['session'],frozen_manifest_sha256=digest,frozen_count=len(scope.static))
    text=json.dumps(data,sort_keys=True,separators=(',',':'))
    p=read_bound_policy(text,env,app)
    if not p.time_allows(now.timestamp()):raise ValueError('SESSION_CLOSED')
    return text

def main():
    import argparse
    parser=argparse.ArgumentParser(description='Offline only; stdout emits policy only after all checks pass')
    parser.add_argument('--session',required=True);parser.add_argument('--start-utc',required=True);parser.add_argument('--end-utc',required=True)
    args=parser.parse_args()
    print(generate(args.session,args.start_utc,args.end_utc))

if __name__=='__main__':main()
