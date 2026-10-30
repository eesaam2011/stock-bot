from dataclasses import dataclass
@dataclass
class ResourceGuard:
 memory_pct:float=0.;pipeline_healthy:bool=True
 def level(self):
  return "EMERGENCY" if self.memory_pct>=90 else "CRITICAL" if self.memory_pct>=80 else "PRESSURE" if self.memory_pct>=70 else "NORMAL"
 def new_entries_allowed(self):return self.level() not in {"CRITICAL","EMERGENCY"} and self.pipeline_healthy
 def active_trade_allowed(self):return True
