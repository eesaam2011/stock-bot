"""Only known local error-code families, never remote bodies/URLs/exceptions."""
import re
def reason(exc):
 s=str(exc)
 prefixes=('CGROUP_','WS_','TAIL_','CONFLUENCE_','CLOCK_','SESSION_','PIN_','NEEDS_','ROOT_','HISTORY_','RTH_','OUTBOX_','ENTRY_','PREP_','NO_US_','ALPACA_','ASSET','CALENDAR_','PAGINATION_','BAR_','REFERENCE_HOST_','RESPONSE_','CHILD_','TELEGRAM_','MISSING_','DELIVERY_')
 return s if s.startswith(prefixes) and re.fullmatch('[A-Z][A-Z_0-9]{1,99}',s) else type(exc).__name__
