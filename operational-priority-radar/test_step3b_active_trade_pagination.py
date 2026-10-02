"""Step 3B: bound single-symbol trade/bar history before ANY recovery commit."""
import json,unittest
from datetime import timedelta
from alpaca_production_market import AlpacaREST,AlpacaCredentials,AlpacaMarketDataError
from active_trade_recovery import ActiveTradeRecoveryError
from test_step16d_active_trade_recovery import T,trade,setup
from state_store import key_trade

class Pages(AlpacaREST):
    def __init__(self,pages):
        super().__init__(AlpacaCredentials('test','test'))
        self.pages=pages;self.calls=[]
    def _get(self,path,params):
        kind=path.rsplit('/',1)[-1]
        key=(kind,params.get('page_token'))
        self.calls.append((path,dict(params)))
        if key not in self.pages:raise AssertionError('unexpected '+str(key))
        return self.pages[key]

def event(sec,price):return {'t':(T+timedelta(seconds=sec)).isoformat(),'p':price}
def bar(sec,price):return {'t':(T+timedelta(seconds=sec)).isoformat(),'c':price}
END=T+timedelta(minutes=30)

class TestSingleSymbolPagination(unittest.TestCase):
    def test_trades_two_pages_complete_and_audited(self):
        r=Pages({('trades',None):{'trades':[event(1,10.1)],'next_page_token':'p2'},
                 ('trades','p2'):{'trades':[event(2,10.2)],'next_page_token':None}})
        rows,a=r.trades_audited('A',T,END)
        self.assertEqual(len(rows),2)
        self.assertEqual((a['pages'],a['rows']),(2,2))
        self.assertTrue(a['api_pagination_exhausted'])
        self.assertFalse(a['sip_trade_coverage_proven'])
        self.assertEqual(r.calls[1][1]['page_token'],'p2')
        self.assertEqual(r.calls[0][1]['feed'],'sip')
    def test_bars_pagination_raw_and_audited(self):
        r=Pages({('bars',None):{'bars':[bar(0,10.1)],'next_page_token':None}})
        rows,a=r.bars_audited('A',T,END,'1Min')
        self.assertEqual(len(rows),1)
        self.assertTrue(a['api_pagination_exhausted'])
        self.assertFalse(a['full_session_coverage_proven'])
        self.assertEqual(r.calls[0][1]['adjustment'],'raw')
    def test_repeated_token_fails(self):
        r=Pages({('trades',None):{'trades':[],'next_page_token':'repeat'},
                 ('trades','repeat'):{'trades':[],'next_page_token':'repeat'}})
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_REPEATED_OR_INVALID_PAGE_TOKEN'):
            r.trades('A',T,END)
        self.assertEqual(len(r.calls),2)
    def test_page_limit_fails(self):
        r=Pages({('trades',None):{'trades':[],'next_page_token':'next'}})
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_PAGE_LIMIT_EXCEEDED'):
            r.trades_audited('A',T,END,max_pages=1)
        self.assertEqual(len(r.calls),1)
    def test_row_limit_fails(self):
        r=Pages({('trades',None):{'trades':[event(1,10),event(2,11)],'next_page_token':None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_ROW_LIMIT_EXCEEDED'):
            r.trades_audited('A',T,END,max_rows=1)
    def test_malformed_response_fails(self):
        r=Pages({('bars',None):{'bars':{'A':[]},'next_page_token':None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_INVALID_SINGLE_SYMBOL_ROWS'):
            r.bars('A',T,END,'1Min')
    def test_missing_rows_key_fails(self):
        r=Pages({('trades',None):{'next_page_token':None}})
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_MISSING_SINGLE_SYMBOL_ROWS'):
            r.trades('A',T,END)
    def test_explicit_null_rows_is_empty_but_not_coverage(self):
        r=Pages({('trades',None):{'trades':None,'next_page_token':None}})
        rows,a=r.trades_audited('A',T,END)
        self.assertEqual(rows,[])
        self.assertFalse(a['sip_trade_coverage_proven'])
    def test_bad_params_rejected_before_http(self):
        r=Pages({})
        for kw in ({'max_pages':0},{'max_rows':0},{'limit':0}):
            with self.assertRaisesRegex(AlpacaMarketDataError,'INVALID_SINGLE_SYMBOL_REST_REQUEST'):
                r.trades_audited('A',T,END,**kw)
        with self.assertRaisesRegex(AlpacaMarketDataError,'INVALID_SINGLE_SYMBOL_REST_REQUEST'):
            r.bars_audited('A',T,END,'SYNTHETIC')
        self.assertFalse(r.calls)
    def test_trade_recovery_reads_both_paginated_lanes_before_commit(self):
        r=Pages({('trades',None):{'trades':[event(1,8.8)],'next_page_token':'p2'},
                 ('trades','p2'):{'trades':[event(2,12.5)],'next_page_token':None},
                 ('bars',None):{'bars':[],'next_page_token':None}})
        t=trade();rec,rd=setup(t,r)
        result=rec.reconcile(t)
        self.assertEqual(result['state'],'CLOSED_STOP')
        self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'CLOSED_STOP')
        self.assertEqual([p.rsplit('/',1)[-1] for p,_ in r.calls],['trades','trades','bars'])
    def test_truncated_trade_chain_never_commits(self):
        r=Pages({('trades',None):{'trades':[event(1,12.5)],'next_page_token':'loop'},
                 ('trades','loop'):{'trades':[],'next_page_token':'loop'}})
        t=trade();rec,rd=setup(t,r)
        before=rd.d[key_trade('TR')]
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_REPEATED_OR_INVALID_PAGE_TOKEN'):
            rec.reconcile(t)
        self.assertEqual(rd.d[key_trade('TR')],before)
        self.assertFalse(any(':outbox:' in k for k in rd.d))
    def test_truncated_bar_chain_never_commits_even_if_t2_trade(self):
        r=Pages({('trades',None):{'trades':[event(1,12.5)],'next_page_token':None},
                 ('bars',None):{'bars':[],'next_page_token':'loop'},
                 ('bars','loop'):{'bars':[],'next_page_token':'loop'}})
        t=trade();rec,rd=setup(t,r)
        before=rd.d[key_trade('TR')]
        with self.assertRaisesRegex(AlpacaMarketDataError,'REST_REPEATED_OR_INVALID_PAGE_TOKEN'):
            rec.reconcile(t)
        self.assertEqual(rd.d[key_trade('TR')],before)
    def test_malformed_trade_never_skipped_to_false_t2(self):
        r=Pages({('trades',None):{'trades':[{'t':event(1,8.8)['t']},event(2,12.5)],'next_page_token':None},
                 ('bars',None):{'bars':[],'next_page_token':None}})
        t=trade();rec,rd=setup(t,r)
        with self.assertRaisesRegex(ActiveTradeRecoveryError,'MALFORMED_RECOVERED_TRADE'):
            rec.reconcile(t)
        self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'ACTIVE_PRE_T1')
    def test_nonfinite_trade_price_rejected(self):
        r=Pages({('trades',None):{'trades':[event(1,float('nan'))],'next_page_token':None},
                 ('bars',None):{'bars':[],'next_page_token':None}})
        t=trade();rec,_=setup(t,r)
        with self.assertRaisesRegex(ActiveTradeRecoveryError,'INVALID_RECOVERED_TRADE_EVENT'):
            rec.reconcile(t)
    def test_future_bar_rejected_before_commit(self):
        r=Pages({('trades',None):{'trades':[event(1,12.5)],'next_page_token':None},
                 ('bars',None):{'bars':[bar(30*60,8.8)],'next_page_token':None}})
        t=trade();rec,rd=setup(t,r)
        with self.assertRaisesRegex(ActiveTradeRecoveryError,'INVALID_RECOVERED_BAR_EVENT'):
            rec.reconcile(t)
        self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'ACTIVE_PRE_T1')

if __name__=='__main__':unittest.main()
