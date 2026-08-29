from __future__ import annotations

import json, math, os, re, time
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta, timezone
from statistics import mean
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")

def now_iso(): return datetime.now(UTC).isoformat()
def avg(xs, default=0.0):
    xs=list(xs); return mean(xs) if xs else default
def clamp(x, lo=0.0, hi=100.0): return max(lo, min(hi, float(x)))
def parse_dt(s): return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)

class RedisREST:
    def __init__(self):
        self.url=os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/"); self.token=os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    def command(self, *parts):
        if not self.url or not self.token: raise RuntimeError("Redis environment is missing")
        req=Request(self.url, data=json.dumps(list(parts)).encode(), headers={"Authorization":f"Bearer {self.token}","Content-Type":"application/json"}, method="POST")
        with urlopen(req, timeout=90) as response: payload=json.load(response)
        if payload.get("error"): raise RuntimeError(str(payload["error"]))
        return payload.get("result")
    def get_json(self, key, default=None):
        raw=self.command("GET", key); return default if raw is None else json.loads(raw)
    def set_json(self, key, value): return self.command("SET", key, json.dumps(value, separators=(",",":"), ensure_ascii=False))

class Alpaca:
    def __init__(self):
        self.headers={"APCA-API-KEY-ID":os.getenv("ALPACA_API_KEY", ""),"APCA-API-SECRET-KEY":os.getenv("ALPACA_SECRET_KEY", "")}
    def get(self, url, params=None):
        target=url+("?"+urlencode(params, doseq=True) if params else "")
        for attempt in range(7):
            try:
                with urlopen(Request(target, headers=self.headers), timeout=90) as response: return json.load(response)
            except HTTPError as exc:
                if exc.code != 429 or attempt == 6: raise RuntimeError(f"Alpaca HTTP {exc.code}: {exc.read(500).decode('utf-8','replace')}")
                time.sleep(min(30, 2**attempt))
    def bars(self, symbols, start, end, feed):
        out={s:[] for s in symbols}; token=None
        while True:
            params={"symbols":",".join(symbols),"timeframe":"1Min","start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),"feed":feed,"limit":10000,"adjustment":"raw","sort":"asc"}
            if token: params["page_token"]=token
            page=self.get("https://data.alpaca.markets/v2/stocks/bars", params)
            for symbol, rows in (page.get("bars") or {}).items(): out.setdefault(symbol, []).extend(rows or [])
            token=page.get("next_page_token")
            if not token: return out

class BacktestCollector:
    """Resumable, bounded Redis backtest that can only continue the populated v3 dataset."""
    def __init__(self, redis_client=None, alpaca_client=None):
        self.redis=redis_client or RedisREST(); self.alpaca=alpaca_client or Alpaca()
        self.prefix=os.getenv("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v3")
        self.batch_size=max(1,int(os.getenv("NDR_BT_SYMBOL_BATCH_SIZE","10")))
        self.delay=float(os.getenv("NDR_BT_REQUEST_DELAY_SECONDS","0.15"))
        self.modes=tuple(x.strip() for x in os.getenv("NDR_BT_TEST_MODES","strict,approx").split(",") if x.strip())
        self.opp_min=88.0; self.fail_max=35.0
    def key(self, suffix): return f"{self.prefix}:{suffix}"
    def status(self): return self.redis.get_json(self.key("status"), {"phase":"NOT_PREPARED"})
    def save_status(self, **updates):
        current=self.status(); current.update(updates); current["updated_at"]=now_iso(); self.redis.set_json(self.key("status"), current); return current
    def prepare(self): raise RuntimeError("Preparation disabled: continue the existing v3 DETAIL_READY dataset")
    def coarse_step(self): raise RuntimeError("Coarse collection disabled: continue the existing v3 DETAIL_READY dataset")
    def detail_index_step(self):
        manifest=self.redis.get_json(self.key("manifest"))
        if not manifest: raise RuntimeError("Backtest manifest is missing")
        ck=self.key("detail_index_cursor"); cursor=str(self.redis.command("GET",ck) or "0")
        scanned=self.redis.command("SSCAN",self.key("coarse_candidates"),cursor,"COUNT",1000)
        if not isinstance(scanned,list) or len(scanned)!=2: raise RuntimeError(f"Unexpected SSCAN response: {scanned!r}")
        nxt,members=str(scanned[0]),list(scanned[1] or []); allowed=set(manifest["sessions"]); grouped={}; malformed=0
        for member in members:
            pair=str(member).split("|",1)
            if len(pair)!=2 or pair[0] not in allowed or not SYMBOL_RE.fullmatch(pair[1]): malformed+=1; continue
            grouped.setdefault(pair[0],set()).add(pair[1])
        added=sum(int(self.redis.command("SADD",self.key(f"detail_session:{s}"),*sorted(v)) or 0) for s,v in grouped.items())
        if added: self.redis.command("INCRBY",self.key("detail_indexed"),added)
        if malformed: self.redis.command("INCRBY",self.key("detail_malformed"),malformed)
        self.redis.command("SET",ck,nxt); indexed=int(self.redis.command("GET",self.key("detail_indexed")) or 0); total=int(self.redis.command("SCARD",self.key("coarse_candidates")) or 0)
        if nxt=="0":
            self.redis.set_json(self.key("detail_cursor"),{"session_index":0,"sscan_cursor":"0","processed":0})
            return self.save_status(phase="DETAIL_REPLAY_READY",message="Candidate session index completed",detail_indexed=indexed,detail_index_pct=100.0)
        return self.save_status(phase="DETAIL_INDEXING",message="Indexing candidates by session",detail_indexed=indexed,detail_index_pct=round(indexed/max(1,total)*100,2),detail_index_cursor=nxt)
    @staticmethod
    def session_window(session):
        day=date.fromisoformat(session); return datetime.combine(day-timedelta(days=1),dtime(16),NY).astimezone(UTC),datetime.combine(day,dtime(16),NY).astimezone(UTC)
    @staticmethod
    def merge_bars(sip, boats):
        merged={}
        for feed,rows in (("sip",sip),("boats",boats)):
            for raw in rows:
                if not raw.get("t"): continue
                et=parse_dt(raw["t"]).astimezone(NY); m=et.hour*60+et.minute; overnight=m>=1200 or m<240
                if (feed=="boats")==overnight: bar=dict(raw); bar["feed"]=feed; merged[raw["t"]]=bar
        return sorted(merged.values(),key=lambda x:x["t"])
    @staticmethod
    def phase(bar):
        et=parse_dt(bar["t"]).astimezone(NY); m=et.hour*60+et.minute
        if 960<=m<1200:return "AFTER_HOURS"
        if m>=1200 or m<240:return "OVERNIGHT"
        if 240<=m<570:return "PREMARKET"
        return "REGULAR"
    @staticmethod
    def features(history, mode):
        closes=[float(x["c"]) for x in history]; highs=[float(x["h"]) for x in history]; vols=[float(x.get("v",0)) for x in history]
        price,ref=closes[-1],float(history[0]["o"]); total=sum(vols); vwap=sum(float(x.get("vw") or x["c"])*float(x.get("v",0)) for x in history)/total if total else avg(closes)
        recent=history[-12:]; prior=history[-24:-12]; rv=avg(float(x.get("v",0)) for x in recent); pv=avg((float(x.get("v",0)) for x in prior),max(1,rv)); accel=rv/max(1,pv)
        window=closes[-30:]; lo,hi=min(window),max(window); span=max(1e-9,hi-lo); acceptance=clamp(100*avg(1 if x>=lo+.55*span else 0 for x in window)); closepos=clamp(100*(price-lo)/span)
        demand=clamp(.5*closepos+.25*acceptance+.25*min(100,accel*40)); resistance=max(highs[-21:-1] or highs[-1:]); reclaim=100 if price>=resistance*.998 else clamp(50+(price/resistance-1)*1000)
        pullback=clamp(100-max(0,(hi-price)/max(hi,1e-9)*500)); change=(price/ref-1)*100; extension=clamp(max(0,change-12)*4+max(0,(price/max(vwap,1e-9)-1)*100-8)*5)
        trajectory=clamp(50+(closes[-1]/max(closes[max(0,len(closes)-10)],1e-9)-1)*700); continuity=clamp(avg(1 if float(x.get("n",0))>0 else 0 for x in recent)*100)
        spread=clamp(avg((float(x["h"])-float(x["l"]))/max(float(x["c"]),1e-9)*100 for x in recent),0,25); spreadq=clamp(100-spread*15)
        participation=clamp(avg([min(100,accel*45),continuity,min(100,math.log10(total+1)*18)])); persistence=clamp(avg([acceptance,pullback,trajectory])); liquidity=clamp(avg([spreadq,continuity])); context=clamp(avg([100-extension,trajectory]))
        opportunity=clamp(.24*participation+.34*demand+.18*persistence+.10*liquidity+.14*context); failure=clamp(.30*(100-demand)+.25*(100-acceptance)+.20*(100-pullback)+.15*(100-reclaim)+.10*(100-spreadq))
        unavailable=["point_in_time_news","point_in_time_float","historical_quotes"]+(["historical_trades"] if mode=="strict" else [])
        return {"price":price,"vwap":vwap,"change_pct":change,"resistance":resistance,"opportunity":opportunity,"failure_pressure":failure,"demand_efficiency":demand,"price_acceptance":acceptance,"volume_acceleration":accel,"spread_pct":None if mode=="strict" else spread,"extension_risk":extension,"unavailable_features":unavailable}
    @staticmethod
    def signal(bar, phase, f): return {"ts":bar["t"],"phase":phase,"price":round(f["price"],6),"opportunity":round(f["opportunity"],3),"failure_pressure":round(f["failure_pressure"],3),"resistance":round(f["resistance"],6)}
    @staticmethod
    def entry_plan(history,f):
        if len(history)<15:return None
        trs=[]; prev=float(history[-15]["c"])
        for b in history[-14:]:
            h,l,c=float(b["h"]),float(b["l"]),float(b["c"]); trs.append(max(h-l,abs(h-prev),abs(l-prev))); prev=c
        entry=f["price"]; stop=max(min(float(x["l"]) for x in history[-8:])*.998,entry-max(avg(trs)*1.3,entry*.015)); pct=(entry-stop)/entry*100
        if stop>=entry or pct<=0 or pct>6:return None
        risk=entry-stop; return {"stop":round(stop,6),"stop_pct":round(pct,3),"t1":round(entry+risk*1.5,6),"t2":round(entry+risk*2.5,6),"t3":round(entry+risk*4,6)}
    @staticmethod
    def outcome(signal,bars):
        if not signal:return None
        future=[x for x in bars if x["t"]>signal["ts"]]
        if not future:return {**signal,"mfe_pct":0.0,"mae_pct":0.0,"time_to_mfe_minutes":None,"forward_bars":0}
        high=max(future,key=lambda x:float(x["h"])); low=min(future,key=lambda x:float(x["l"])); price=signal["price"]
        return {**signal,"mfe_pct":round((float(high["h"])/price-1)*100,4),"mae_pct":round((float(low["l"])/price-1)*100,4),"time_to_mfe_minutes":round((parse_dt(high["t"])-parse_dt(signal["ts"])).total_seconds()/60,1),"forward_bars":len(future)}
    def partition(self,session):
        manifest=self.redis.get_json(self.key("manifest"),{}); return "holdout" if session in set(manifest.get("holdout_sessions",[])) else "development"
    def replay(self,session,symbol,bars,mode):
        result={"schema":1,"session":session,"partition":self.partition(session),"symbol":symbol,"mode":mode,"bars":len(bars),"breakout_ready":None,"confirmed_entry":None,"block_reasons":Counter(),"unavailable_features":Counter()}; history=[]; ready=None; entry=None
        for bar in bars:
            history.append(bar)
            if len(history)<24:continue
            f=self.features(history,mode); result["unavailable_features"].update(f["unavailable_features"]); phase=self.phase(bar); reasons=[]
            if f["opportunity"]<self.opp_min:reasons.append("opportunity")
            if f["failure_pressure"]>self.fail_max:reasons.append("failure_pressure")
            if f["price"]<f["vwap"]:reasons.append("below_vwap")
            if f["demand_efficiency"]<65:reasons.append("demand_efficiency")
            if f["price_acceptance"]<62:reasons.append("price_acceptance")
            if f["volume_acceleration"]<1:reasons.append("volume_not_accelerating")
            if mode=="strict":reasons.append("strict_feature_unavailable")
            result["block_reasons"].update(reasons)
            if not reasons and ready is None:ready=self.signal(bar,phase,f)
            if ready and entry is None and phase=="REGULAR":
                er=list(reasons)
                if mode=="strict":er.append("historical_quote_trade_float_news_unavailable")
                closes=[float(x["c"]) for x in history[-3:]]
                if sum(x>=f["resistance"]*.998 for x in closes[-2:])<2:er.append("breakout_not_held")
                plan=self.entry_plan(history,f)
                if not plan:er.append("trade_plan_invalid")
                result["block_reasons"].update(er)
                if not er:entry=self.signal(bar,phase,f);entry.update(plan)
        result["breakout_ready"]=self.outcome(ready,bars);result["confirmed_entry"]=self.outcome(entry,bars);result["block_reasons"]=dict(result["block_reasons"].most_common(20));result["unavailable_features"]=dict(result["unavailable_features"].most_common());return result
    def detail_replay_step(self):
        manifest=self.redis.get_json(self.key("manifest"))
        if not manifest:raise RuntimeError("Backtest manifest is missing")
        cursor=self.redis.get_json(self.key("detail_cursor"),{"session_index":0,"sscan_cursor":"0","processed":0}); si=int(cursor.get("session_index",0))
        if si>=len(manifest["sessions"]):return self.finalize_report()
        session=manifest["sessions"][si]; scanned=self.redis.command("SSCAN",self.key(f"detail_session:{session}"),str(cursor.get("sscan_cursor","0")),"COUNT",self.batch_size); nxt,symbols=str(scanned[0]),sorted(set(scanned[1] or []))
        if symbols:
            start,end=self.session_window(session);sip=self.alpaca.bars(symbols,start,end,"sip");time.sleep(self.delay);boats=self.alpaca.bars(symbols,start,end,"boats");time.sleep(self.delay)
            for symbol in symbols:
                bars=self.merge_bars(sip.get(symbol,[]),boats.get(symbol,[]))
                for mode in self.modes:
                    field=f"{session}|{symbol}|{mode}"
                    if not self.redis.command("HEXISTS",self.key("results"),field):self.redis.command("HSET",self.key("results"),field,json.dumps(self.replay(session,symbol,bars,mode),separators=(",",":"),ensure_ascii=False))
                self.redis.command("SADD",self.key(f"completed:{session}"),symbol)
        if nxt=="0":si+=1
        cursor={"session_index":si,"sscan_cursor":nxt,"processed":int(cursor.get("processed",0))+len(symbols)};self.redis.set_json(self.key("detail_cursor"),cursor);total=int(self.redis.command("SCARD",self.key("coarse_candidates")) or 0)
        return self.save_status(phase="DETAIL_REPLAYING",message=f"Causal 1-minute replay: {session}",detail_cursor=cursor,detail_processed=cursor["processed"],detail_progress_pct=round(cursor["processed"]/max(1,total)*100,2))
    def iter_results(self):
        cursor="0"
        while True:
            scanned=self.redis.command("HSCAN",self.key("results"),cursor,"COUNT",500);cursor=str(scanned[0]);rows=scanned[1] or []
            for i in range(0,len(rows),2):yield json.loads(rows[i+1])
            if cursor=="0":break
    @staticmethod
    def aggregate(rows,sensitivity=False):
        out={"cases":len(rows),"signals":{},"block_reasons":{},"unavailable_features":{}};blocks=Counter();missing=Counter()
        for row in rows:blocks.update(row.get("block_reasons",{}));missing.update(row.get("unavailable_features",{}))
        out["block_reasons"]=dict(blocks.most_common(30));out["unavailable_features"]=dict(missing.most_common())
        for name in ("breakout_ready","confirmed_entry"):
            sig=[x[name] for x in rows if x.get(name)];mfes=sorted(x["mfe_pct"] for x in sig)
            out["signals"][name]={"count":len(sig),"avg_mfe_pct":round(avg(mfes),4),"median_mfe_pct":round(mfes[len(mfes)//2],4) if mfes else 0,"avg_mae_pct":round(avg(x["mae_pct"] for x in sig),4),"mfe_ge_5_pct":sum(x["mfe_pct"]>=5 for x in sig),"mfe_ge_10_pct":sum(x["mfe_pct"]>=10 for x in sig)}
        if sensitivity:
            out["sensitivity"]=[]
            for opp in (75,80,85,88,90,93):
                for fail in (25,30,35,40,45):
                    sig=[x["breakout_ready"] for x in rows if x.get("breakout_ready") and x["breakout_ready"]["opportunity"]>=opp and x["breakout_ready"]["failure_pressure"]<=fail]
                    out["sensitivity"].append({"opportunity_min":opp,"failure_max":fail,"signals":len(sig),"avg_mfe_pct":round(avg(x["mfe_pct"] for x in sig),4),"mfe_ge_5_rate":round(sum(x["mfe_pct"]>=5 for x in sig)/max(1,len(sig))*100,2)})
        return out
    def finalize_report(self):
        # HSCAN and accumulate instead of loading hundreds of thousands of cases.
        buckets={}
        for mode in self.modes:
            for part in ("development","holdout"):
                buckets[(mode,part)]={"cases":0,"blocks":Counter(),"missing":Counter(),
                    "signals":{n:{"count":0,"mfe":[],"mae_sum":0.0,"ge5":0,"ge10":0} for n in ("breakout_ready","confirmed_entry")},
                    "sensitivity":{(o,f):[0,0.0,0] for o in (75,80,85,88,90,93) for f in (25,30,35,40,45)}}
        for row in self.iter_results():
            b=buckets[(row["mode"],row["partition"])];b["cases"]+=1;b["blocks"].update(row.get("block_reasons",{}));b["missing"].update(row.get("unavailable_features",{}))
            for name in ("breakout_ready","confirmed_entry"):
                signal=row.get(name)
                if not signal:continue
                s=b["signals"][name];s["count"]+=1;s["mfe"].append(signal["mfe_pct"]);s["mae_sum"]+=signal["mae_pct"];s["ge5"]+=signal["mfe_pct"]>=5;s["ge10"]+=signal["mfe_pct"]>=10
            signal=row.get("breakout_ready")
            if signal and row["partition"]=="development":
                for (opp,fail),cell in b["sensitivity"].items():
                    if signal["opportunity"]>=opp and signal["failure_pressure"]<=fail:cell[0]+=1;cell[1]+=signal["mfe_pct"];cell[2]+=signal["mfe_pct"]>=5
        report={"generated_at":now_iso(),"schema":1,"source_prefix":self.prefix,"thresholds":{"opportunity":88,"failure":35},"sensitivity_policy":"development_only; holdout never used for tuning","modes":{}}
        for mode in self.modes:
            report["modes"][mode]={}
            for part in ("development","holdout"):
                b=buckets[(mode,part)];out={"cases":b["cases"],"block_reasons":dict(b["blocks"].most_common(30)),"unavailable_features":dict(b["missing"].most_common()),"signals":{}}
                for name,s in b["signals"].items():
                    mfes=sorted(s["mfe"]);n=s["count"];out["signals"][name]={"count":n,"avg_mfe_pct":round(sum(mfes)/max(1,n),4),"median_mfe_pct":round(mfes[n//2],4) if n else 0,"avg_mae_pct":round(s["mae_sum"]/max(1,n),4),"mfe_ge_5_pct":s["ge5"],"mfe_ge_10_pct":s["ge10"]}
                if part=="development":out["sensitivity"]=[{"opportunity_min":o,"failure_max":f,"signals":v[0],"avg_mfe_pct":round(v[1]/max(1,v[0]),4),"mfe_ge_5_rate":round(v[2]/max(1,v[0])*100,2)} for (o,f),v in b["sensitivity"].items()]
                report["modes"][mode][part]=out
        self.redis.set_json(self.key("report"),report);return self.save_status(phase="COMPLETED",message="Backtest completed",report_ready=True,detail_progress_pct=100.0,completed_at=now_iso())
    def report(self):return self.redis.get_json(self.key("report"),{"ready":False,"phase":self.status().get("phase")})
