import os,uuid
from dataclasses import dataclass
from market_trust import Trust,TrustState,new_entries_allowed
from constants import SLOW_CONSUMER_THRESHOLD

class ActionableModeBlocked(RuntimeError):pass

@dataclass(frozen=True)
class WorkerConfig:
 shadow_mode:bool=True
 redis_url:str|None=None
 alpaca_key:str|None=None
 alpaca_secret:str|None=None
 telegram_token:str|None=None
 telegram_chat_id:str|None=None

 @classmethod
 def from_env(cls):
  return cls(
   shadow_mode=os.getenv("OPR_SHADOW_MODE","true").lower()=="true",
   redis_url=os.getenv("REDIS_URL"),
   alpaca_key=os.getenv("ALPACA_API_KEY"),
   alpaca_secret=os.getenv("ALPACA_SECRET_KEY"),
   telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
   telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"))

 def validate(self):
  if not self.redis_url:raise ValueError("REDIS_URL required")
  if not self.alpaca_key or not self.alpaca_secret:raise ValueError("Alpaca credentials required")
  if not self.shadow_mode:
   if SLOW_CONSUMER_THRESHOLD is None:raise ActionableModeBlocked("SLOW_CONSUMER_THRESHOLD=PENDING_SHADOW_BENCHMARK")
   if not self.telegram_token or not self.telegram_chat_id:raise ActionableModeBlocked("Telegram config required")

class OperationalPriorityRadarWorker:
 def __init__(self,config,components):
  config.validate();self.config=config;self.c=components
  self.worker_instance_id=str(uuid.uuid4());self.trust=Trust()
  self.actionable_alerts_enabled=False
 def can_create_new_entry(self):
  return (not self.config.shadow_mode and self.actionable_alerts_enabled and new_entries_allowed(self.trust))
 def shadow_decision(self,decision):
  # Same decision pipeline, but actionable Entry delivery is forbidden.
  return {"mode":"OPERATIONAL_SHADOW_MODE","decision":decision,"actionable_sent":False}
 def assert_actionable_block(self):
  if self.config.shadow_mode:raise ActionableModeBlocked("actionable alerts forbidden in shadow mode")
