class AlpacaNative5MinAdapter:
    def __init__(self,client): self.client=client
    def fetch_native_5min(self,symbol,session,start,end):
        result=self.client.bars([symbol],start,end,feed="sip",adjustment="raw",timeframe="5Min")
        rows=(result or {}).get(symbol)
        if rows is None:return None
        return [{**r,"_timeframe":"native_5Min"} for r in rows]
