"""Offline caller-side gate. Original entry function remains byte-identical."""
def evaluate(controller,symbol,trigger,now,trades,halted,reference_evaluate):
 if not controller.can_entry(symbol):return {'status':'ISOLATED_UNTRUSTED_NO_ENTRY','decision':None}
 # Original trigger is never moved to ACK/first-trade time. No old received_at fabrication.
 decision=reference_evaluate(trigger,now,trades,halted)
 return {'status':decision.status.value,'decision':decision}
