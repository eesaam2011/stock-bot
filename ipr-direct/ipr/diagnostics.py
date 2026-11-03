"""Only known local error-code families, never remote bodies/URLs/exceptions."""
import re
def reason(exc):
 s=str(exc)
 prefixes=('CGROUP_','WS_','TAIL_','CONFLUENCE_','CLOCK_','SESSION_','PIN_','NEEDS_','ROOT_','HISTORY_','RTH_','OUTBOX_','ENTRY_','PREP_','NO_US_','ALPACA_','ASSET','CALENDAR_','PAGINATION_','BAR_','REFERENCE_HOST_','RESPONSE_','CHILD_','TELEGRAM_','MISSING_','DELIVERY_')
 return s if s.startswith(prefixes) and re.fullmatch('[A-Z][A-Z_0-9]{1,99}',s) else type(exc).__name__

def exception_fields(exc):
 import os
 message=str(exc)
 for key,value in os.environ.items():
  if value and any(k in key.upper() for k in ('TOKEN','SECRET','PASSWORD','KEY','URL','CHAT_ID','GIST_ID')):message=message.replace(value,'[REDACTED]')
 message=re.sub(r"(?i)(?:https?|rediss?|wss?)://[^\s\"'<>]+",'[REDACTED_URL]',message)
 message=re.sub(r"(?i)(?:token|password|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",'[REDACTED_CREDENTIAL]',message)
 message=re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|[0-9]{6,}:[A-Za-z0-9_-]{20,})\b','[REDACTED_TOKEN]',message)
 return {'exception_type':type(exc).__module__+'.'+type(exc).__qualname__,'message_redacted_120':message[:120]}
