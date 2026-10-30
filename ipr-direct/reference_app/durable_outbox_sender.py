class OutboxSendError(RuntimeError):pass
class DurableOutboxSender:
 def __init__(self,store,telegram,shadow_mode=True):self.store=store;self.telegram=telegram;self.shadow_mode=shadow_mode
 def deliver_one(self,e):
  if e["state"]!="PENDING":return "SKIP"
  if self.shadow_mode:return "SHADOW_SUPPRESSED"
  try:self.telegram.send(e["payload"])
  except Exception as x:self.store.mark_attempt(e["event_id"],success=False);raise OutboxSendError(str(x))
  self.store.mark_attempt(e["event_id"],success=True);return "SENT"
