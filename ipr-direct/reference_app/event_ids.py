def entry_event_id(session, symbol, opportunity_id):
    return f"ENTRY:{session}:{symbol}:{opportunity_id}"

def trade_event_id(event_type, trade_id):
    if event_type not in {"T1","T2","STOP","POST_T1_EXIT","FINAL"}:
        raise ValueError("unsupported event_type")
    return f"{event_type}:{trade_id}"
