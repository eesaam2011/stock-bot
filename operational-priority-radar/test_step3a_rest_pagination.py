"""Step 3A: real REST pagination safety; exhaustion is not session coverage."""
import unittest
from datetime import datetime,timezone
from alpaca_production_market import AlpacaREST,AlpacaCredentials,AlpacaMarketDataError
from production_recovery import ProductionStartupRecovery

START=datetime(2026,9,22,15,0,tzinfo=timezone.utc)
END=datetime(2026,9,22,16,0,tzinfo=timezone.utc)
def bar(ts="2026-09-22T15:55:00Z"):
    return {"t":ts,"o":10,"h":11,"l":9,"c":10,"v":100}

class Pages(AlpacaREST):
    def __init__(self,pages):
        super().__init__(AlpacaCredentials("test","test"))
        self.pages=pages;self.calls=[]
    def _get(self,path,params):
        self.calls.append((path,dict(params)))
        key=(params["timeframe"],params.get("page_token"))
        if key not in self.pages:raise AssertionError("unexpected page "+repr(key))
        return self.pages[key]

class TestAuditedPagination(unittest.TestCase):
    def test_all_pages_exhausted_and_no_session_claim(self):
        rest=Pages({("1Min",None):{"bars":{"A":[bar()]},"next_page_token":"next"},
                    ("1Min","next"):{"bars":{"A":[bar("2026-09-22T15:56:00Z")]},"next_page_token":None}})
        rows,a=rest.bars_multi_audited(["A","B"],START,END,"1Min")
        self.assertEqual(len(rows["A"]),2)
        self.assertEqual(rows["B"],[])
        self.assertEqual((a["pages"],a["rows"],a["empty_symbols"]),(2,2,1))
        self.assertTrue(a["api_pagination_exhausted"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertEqual(rest.calls[1][1]["page_token"],"next")
        self.assertEqual(rest.calls[0][1]["feed"],"sip")
        self.assertEqual(rest.calls[0][1]["adjustment"],"raw")
    def test_repeated_page_token_fails_instead_of_infinite_loop(self):
        rest=Pages({("1Min",None):{"bars":{},"next_page_token":"loop"},
                    ("1Min","loop"):{"bars":{},"next_page_token":"loop"}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_REPEATED_OR_INVALID_PAGE_TOKEN"):
            rest.bars_multi_audited(["A"],START,END,"1Min")
        self.assertEqual(len(rest.calls),2)
    def test_page_limit_fails_before_fetching_extra_page(self):
        rest=Pages({("1Min",None):{"bars":{},"next_page_token":"next"}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_PAGE_LIMIT_EXCEEDED"):
            rest.bars_multi_audited(["A"],START,END,"1Min",max_pages=1)
        self.assertEqual(len(rest.calls),1)
    def test_row_limit_fails(self):
        rest=Pages({("1Min",None):{"bars":{"A":[bar(),bar()]},"next_page_token":None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_ROW_LIMIT_EXCEEDED"):
            rest.bars_multi_audited(["A"],START,END,"1Min",max_rows_per_batch=1)
    def test_unknown_symbol_fails(self):
        rest=Pages({("1Min",None):{"bars":{"B":[bar()]},"next_page_token":None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_UNREQUESTED_SYMBOL"):
            rest.bars_multi_audited(["A"],START,END,"1Min")
    def test_malformed_response_fails(self):
        rest=Pages({("1Min",None):{"bars":[],"next_page_token":None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_INVALID_BARS_RESPONSE"):
            rest.bars_multi_audited(["A"],START,END,"1Min")
    def test_invalid_token_type_fails(self):
        rest=Pages({("1Min",None):{"bars":{},"next_page_token":123}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_REPEATED_OR_INVALID_PAGE_TOKEN"):
            rest.bars_multi_audited(["A"],START,END,"1Min")
    def test_invalid_symbol_rows_fails(self):
        rest=Pages({("1Min",None):{"bars":{"A":"bad"},"next_page_token":None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,"REST_INVALID_SYMBOL_ROWS"):
            rest.bars_multi_audited(["A"],START,END,"1Min")
    def test_duplicate_symbols_and_naive_times_fail_before_request(self):
        rest=Pages({})
        with self.assertRaisesRegex(AlpacaMarketDataError,"INVALID_AUDITED_REST_REQUEST"):
            rest.bars_multi_audited(["A","A"],START,END,"1Min")
        with self.assertRaisesRegex(AlpacaMarketDataError,"INVALID_AUDITED_REST_REQUEST"):
            rest.bars_multi_audited(["A"],START.replace(tzinfo=None),END,"1Min")
        self.assertFalse(rest.calls)
    def test_both_native_lanes_independently_exhausted(self):
        rest=Pages({("1Min",None):{"bars":{"A":[bar("2026-09-22T15:59:00Z")]},"next_page_token":None},
                    ("5Min",None):{"bars":{"A":[bar()]},"next_page_token":None}})
        one,five,a=rest.native_recovery_batch_audited(["A"],START,END)
        self.assertEqual(one["A"][0]["t"],"2026-09-22T15:59:00Z")
        self.assertEqual(five["A"][0]["_timeframe"],"native_5Min")
        self.assertTrue(a["both_api_page_chains_exhausted"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertEqual((a["native_1m"]["pages"],a["native_5m"]["pages"]),(1,1))
    def test_production_recovery_records_audit_but_stays_untrusted(self):
        rest=Pages({("1Min",None):{"bars":{"A":[bar("2026-09-22T15:59:00Z")]},"next_page_token":None},
                    ("5Min",None):{"bars":{"A":[bar()]},"next_page_token":None}})
        class Reader:
            def active_trades(self):return []
            def earliest_decision_anchor(self,session):return None
        rec=ProductionStartupRecovery(Reader(),rest,None,"2026-09-22",["A"],now_fn=lambda:END)
        result=rec.run()
        a=result["fetch_audit"]
        self.assertEqual(a["rest_pagination_audited_batches"],1)
        self.assertEqual((a["rest_pagination_pages_1m"],a["rest_pagination_pages_5m"]),(1,1))
        self.assertTrue(a["api_page_chains_exhausted"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertFalse(result["gap_recovered"])
        self.assertFalse(rec.continuity_verified(1))
    def test_production_fails_closed_on_repeated_token(self):
        rest=Pages({("1Min",None):{"bars":{},"next_page_token":"loop"},
                    ("1Min","loop"):{"bars":{},"next_page_token":"loop"}})
        class Reader:
            def active_trades(self):return []
            def earliest_decision_anchor(self,session):return None
        rec=ProductionStartupRecovery(Reader(),rest,None,"2026-09-22",["A"],now_fn=lambda:END)
        result=rec.run()
        self.assertEqual(result["reason"],"AlpacaMarketDataError")
        self.assertFalse(result["gap_recovered"])
        self.assertFalse(rec.continuity_verified(1))

if __name__=="__main__":unittest.main()
