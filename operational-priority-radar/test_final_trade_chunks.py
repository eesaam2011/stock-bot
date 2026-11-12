"""Bounded REST trade chunking; no tape completeness inference."""
import unittest
from datetime import datetime,timedelta,timezone
from recovery_trade_chunks import fetch_chunked_trades,TradeChunkUnsafe
from alpaca_production_market import AlpacaMarketDataError
from test_step16d_active_trade_recovery import T,trade,setup
from state_store import key_trade
import json

START=T;END=T+timedelta(minutes=30)
def row(minute,price=10):
    return {"t":(START+timedelta(minutes=minute)).isoformat(),"p":price}

class Chunks:
    def __init__(self,rows=(),include_end=False):
        self.rows=list(rows);self.include_end=include_end;self.calls=[]
    def trades_audited(self,symbol,start,end):
        self.calls.append((start,end))
        data=[x for x in self.rows if start<=datetime.fromisoformat(x["t"])<=end
              if self.include_end or datetime.fromisoformat(x["t"])<end]
        return data,{"rows":len(data),"api_pagination_exhausted":True}
    def bars_audited(self,*args):
        return [],{"api_pagination_exhausted":True}

class TestChunkedTradeREST(unittest.TestCase):
    def test_three_chunks_include_each_boundary_trade_once(self):
        r=Chunks([row(0),row(10),row(20),row(30)],include_end=True)
        rows,a=fetch_chunked_trades(r,"A",START,END,chunk_minutes=10)
        self.assertEqual(rows,[row(0),row(10),row(20),row(30)])
        self.assertEqual(a["chunks"],3)
        self.assertEqual(a["boundary_rows_excluded"],2)
        self.assertFalse(a["sip_trade_coverage_proven"])
        self.assertFalse(a["equal_timestamp_order_proven"])
    def test_identical_trades_same_timestamp_are_not_deduped(self):
        r=Chunks([row(1),row(1)])
        rows,_=fetch_chunked_trades(r,"A",START,END)
        self.assertEqual(len(rows),2)
    def test_out_of_order_trade_fails_closed(self):
        class Reversed(Chunks):
            def trades_audited(self,*a):
                rows,meta=super().trades_audited(*a)
                return list(reversed(rows)),meta
        with self.assertRaisesRegex(TradeChunkUnsafe,"NON_CHRONOLOGICAL"):
            fetch_chunked_trades(Reversed([row(1),row(2)]),"A",START,END)
    def test_truncated_page_chain_fails_closed(self):
        class Truncated(Chunks):
            def trades_audited(self,*a):
                rows,meta=super().trades_audited(*a)
                meta["api_pagination_exhausted"]=False
                return rows,meta
        with self.assertRaisesRegex(TradeChunkUnsafe,"PAGINATION_UNPROVEN"):
            fetch_chunked_trades(Truncated([row(1)]),"A",START,END)
    def test_out_of_range_trade_fails_closed(self):
        class Bad(Chunks):
            def trades_audited(self,*a):
                return [row(-1)],{"rows":1,"api_pagination_exhausted":True}
        with self.assertRaisesRegex(TradeChunkUnsafe,"OUT_OF_RANGE"):
            fetch_chunked_trades(Bad(),"A",START,END)
    def test_total_row_budget_fails_closed(self):
        with self.assertRaisesRegex(TradeChunkUnsafe,"TOTAL_ROW_LIMIT"):
            fetch_chunked_trades(Chunks([row(1),row(2)]),"A",START,END,
                                 max_total_rows=1)
    def test_chunk_count_budget_fails_closed(self):
        with self.assertRaisesRegex(TradeChunkUnsafe,"CHUNK_LIMIT"):
            fetch_chunked_trades(Chunks(),"A",START,END,
                                 chunk_minutes=1,max_chunks=2)
    def test_malformed_trade_fails_closed(self):
        class Bad(Chunks):
            def trades_audited(self,*a):
                return [{"p":10}],{"rows":1,"api_pagination_exhausted":True}
        with self.assertRaisesRegex(TradeChunkUnsafe,"MALFORMED_ROW"):
            fetch_chunked_trades(Bad(),"A",START,END)
    def test_invalid_configuration_fails_before_rest(self):
        r=Chunks()
        for kw in ({"chunk_minutes":0},{"max_chunks":0},
                   {"max_total_rows":0},{"chunk_minutes":True}):
            with self.assertRaisesRegex(TradeChunkUnsafe,"INVALID"):
                fetch_chunked_trades(r,"A",START,END,**kw)
        self.assertEqual(r.calls,[])
    def test_active_trade_fallback_restarts_whole_tape(self):
        class Limited(Chunks):
            def trades_audited(self,symbol,start,end):
                if end-start>timedelta(minutes=15):
                    raise AlpacaMarketDataError("REST_ROW_LIMIT_EXCEEDED")
                return super().trades_audited(symbol,start,end)
        r=Limited([row(1,8.8),row(2,12.5)])
        t=trade();rec,rd=setup(t,r)
        rec.enable_trade_chunk_fallback=True
        result=rec.reconcile(t)
        self.assertEqual(result["state"],"CLOSED_STOP")
        self.assertEqual(json.loads(rd.d[key_trade("TR")])["state"],"CLOSED_STOP")
        self.assertEqual(len(r.calls),2)
    def test_non_limit_rest_error_never_retries(self):
        class Error(Chunks):
            def trades_audited(self,*a):
                raise AlpacaMarketDataError("REST_REPEATED_OR_INVALID_PAGE_TOKEN")
        r=Error();t=trade();rec,rd=setup(t,r)
        rec.enable_trade_chunk_fallback=True
        before=rd.d[key_trade("TR")]
        with self.assertRaisesRegex(AlpacaMarketDataError,"REPEATED"):
            rec.reconcile(t)
        self.assertEqual(rd.d[key_trade("TR")],before)

if __name__=="__main__":unittest.main()
