"""Bounded paginated SIP trade history in disjoint time windows.

REST terminal pages prove only API pagination. They do not prove a complete
market tape, historical halt status, or the order of equal-timestamp trades.
"""
from datetime import datetime,timedelta,timezone

class TradeChunkUnsafe(RuntimeError):pass

def fetch_chunked_trades(rest,symbol,start,end,*,chunk_minutes=15,
                         max_chunks=96,max_total_rows=350000):
    if (not isinstance(symbol,str) or not symbol or symbol!=symbol.strip()
        or not isinstance(start,datetime) or not isinstance(end,datetime)
        or start.tzinfo is None or end.tzinfo is None or start>=end
        or type(chunk_minutes) is not int or not 1<=chunk_minutes<=60
        or type(max_chunks) is not int or not 1<=max_chunks<=288
        or type(max_total_rows) is not int or not 1<=max_total_rows<=350000
        or not callable(getattr(rest,"trades_audited",None))):
        raise TradeChunkUnsafe("INVALID_TRADE_CHUNK_REQUEST")
    out=[];cur=start;chunks=0;excluded_boundaries=0;previous=None
    while cur<end:
        if chunks>=max_chunks:raise TradeChunkUnsafe("TRADE_CHUNK_LIMIT_EXCEEDED")
        stop=min(end,cur+timedelta(minutes=chunk_minutes))
        rows,audit=rest.trades_audited(symbol,cur,stop)
        if (not isinstance(audit,dict)
            or audit.get("api_pagination_exhausted") is not True
            or not isinstance(rows,list)
            or audit.get("rows")!=len(rows)):
            raise TradeChunkUnsafe("TRADE_CHUNK_PAGINATION_UNPROVEN")
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get("t"),str):
                raise TradeChunkUnsafe("TRADE_CHUNK_MALFORMED_ROW")
            try:ts=datetime.fromisoformat(row["t"].replace("Z","+00:00"))
            except ValueError as exc:
                raise TradeChunkUnsafe("TRADE_CHUNK_MALFORMED_TIMESTAMP") from exc
            if ts.tzinfo is None or ts<cur or ts>stop:
                raise TradeChunkUnsafe("TRADE_CHUNK_OUT_OF_RANGE")
            if previous is not None and ts<previous:
                raise TradeChunkUnsafe("TRADE_CHUNK_NON_CHRONOLOGICAL")
            if ts==stop and stop<end:
                # Alpaca may treat 'end' inclusively; the next window owns
                # boundary trades. Never dedup identical same-time trades.
                excluded_boundaries+=1
                continue
            previous=ts
            out.append(row)
            if len(out)>max_total_rows:
                raise TradeChunkUnsafe("TRADE_CHUNK_TOTAL_ROW_LIMIT")
        chunks+=1;cur=stop
    return out,{"kind":"trades","symbol":symbol,"chunks":chunks,
                "rows":len(out),"boundary_rows_excluded":excluded_boundaries,
                "all_api_page_chains_exhausted":True,
                "full_session_coverage_proven":False,
                "sip_trade_coverage_proven":False,
                "halt_coverage_proven":False,
                "equal_timestamp_order_proven":False}
