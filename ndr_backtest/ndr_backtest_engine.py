from __future__ import annotations

import json, math, os, re, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
        try:
            with urlopen(req, timeout=90) as response: payload=json.load(response)
        except HTTPError as exc:
            detail=exc.read(500).decode("utf-8","replace")
            raise RuntimeError(f"Redis HTTP {exc.code}: {detail}") from exc
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
    def bars(self, symbols, start, end, feed, adjustment="raw"):
        out={s:[] for s in symbols}; token=None
        while True:
            params={"symbols":",".join(symbols),"timeframe":"1Min","start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),"feed":feed,"limit":10000,"adjustment":adjustment,"sort":"asc"}
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
    def hset_bounded(self,key,pairs,max_fields=10,max_bytes=200000):
        """Write hash pairs in small requests so one large result cannot cause HTTP 400/413."""
        chunk=[];size=0
        for field,value in pairs:
            pair_size=len(field.encode("utf-8"))+len(value.encode("utf-8"))
            if chunk and (len(chunk)>=max_fields or size+pair_size>max_bytes):
                self.redis.command("HSET",key,*[item for pair in chunk for item in pair]);chunk=[];size=0
            chunk.append((field,value));size+=pair_size
        if chunk:self.redis.command("HSET",key,*[item for pair in chunk for item in pair])
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
    def features(history, mode, running=None):
        # All non-cumulative features use at most 30 completed bars.  The
        # optional running totals make this O(1) per minute while producing the
        # same values as summing the full causal history on every minute.
        tail=history[-30:]; closes=[float(x["c"]) for x in tail]; highs=[float(x["h"]) for x in tail]
        if running:
            ref=running["reference"]; total=running["volume"]
            vwap=running["vwap_numerator"]/total if total else avg(closes)
        else:
            ref=float(history[0]["o"]); total=sum(float(x.get("v",0)) for x in history)
            vwap=sum(float(x.get("vw") or x["c"])*float(x.get("v",0)) for x in history)/total if total else avg(closes)
        price=closes[-1]
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
        running={"reference":float(bars[0]["o"]) if bars else 0.0,"volume":0.0,"vwap_numerator":0.0}
        for bar in bars:
            history.append(bar);volume=float(bar.get("v",0));running["volume"]+=volume;running["vwap_numerator"]+=float(bar.get("vw") or bar["c"])*volume
            if len(history)<24:continue
            f=self.features(history,mode,running); result["unavailable_features"].update(f["unavailable_features"]); phase=self.phase(bar); reasons=[]
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
            start,end=self.session_window(session)
            # SIP and BOATS are independent historical reads. Two workers keep
            # request pressure bounded while removing their sequential wait.
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix="ndr-feed") as pool:
                sip_future=pool.submit(self.alpaca.bars,symbols,start,end,"sip")
                boats_future=pool.submit(self.alpaca.bars,symbols,start,end,"boats")
                sip=sip_future.result();boats=boats_future.result()
            time.sleep(self.delay)
            fields=[f"{session}|{symbol}|{mode}" for symbol in symbols for mode in self.modes]
            legacy_values=self.redis.command("HMGET",self.key("results"),*fields) or [None]*len(fields)
            shard_key=self.key(f"results:{session}")
            shard_values=self.redis.command("HMGET",shard_key,*fields) or [None]*len(fields)
            existing={field for field,legacy,shard in zip(fields,legacy_values,shard_values) if legacy is not None or shard is not None}
            writes=[]
            for symbol in symbols:
                bars=self.merge_bars(sip.get(symbol,[]),boats.get(symbol,[]))
                for mode in self.modes:
                    field=f"{session}|{symbol}|{mode}"
                    if field not in existing:writes.append((field,json.dumps(self.replay(session,symbol,bars,mode),separators=(",",":"),ensure_ascii=False)))
            # Bounded hash writes and one completion-set write per batch.
            # Existing fields are never recalculated or overwritten on resume.
            if writes:self.hset_bounded(shard_key,writes)
            self.redis.command("SADD",self.key(f"completed:{session}"),*symbols)
        if nxt=="0":si+=1
        cursor={"session_index":si,"sscan_cursor":nxt,"processed":int(cursor.get("processed",0))+len(symbols)};self.redis.set_json(self.key("detail_cursor"),cursor);total=int(self.redis.command("SCARD",self.key("coarse_candidates")) or 0)
        return self.save_status(phase="DETAIL_REPLAYING",message=f"Causal 1-minute replay: {session}",detail_cursor=cursor,detail_processed=cursor["processed"],detail_progress_pct=round(cursor["processed"]/max(1,total)*100,2))
    def iter_results(self):
        manifest=self.redis.get_json(self.key("manifest"),{})
        keys=[self.key("results")]+[self.key(f"results:{session}") for session in manifest.get("sessions",[])]
        for result_key in keys:
            cursor="0"
            while True:
                scanned=self.redis.command("HSCAN",result_key,cursor,"COUNT",500);cursor=str(scanned[0]);rows=scanned[1] or []
                for i in range(0,len(rows),2):yield json.loads(rows[i+1])
                if cursor=="0":break
    def raw_case(self,session,symbol,mode):
        field=f"{session}|{symbol}|{mode}"
        raw=self.redis.command("HGET",self.key(f"results:{session}"),field)
        if raw is None:raw=self.redis.command("HGET",self.key("results"),field)
        return json.loads(raw) if raw is not None else None
    def raw_symbol(self,symbol,offset=0,limit=10):
        manifest=self.redis.get_json(self.key("manifest"),{});sessions=manifest.get("sessions",[]);page=sessions[offset:offset+limit];results=[]
        for session in page:
            fields=[f"{session}|{symbol}|strict",f"{session}|{symbol}|approx"]
            values=self.redis.command("HMGET",self.key(f"results:{session}"),*fields)
            missing=[i for i,value in enumerate(values) if value is None]
            if missing:
                legacy=self.redis.command("HMGET",self.key("results"),*[fields[i] for i in missing])
                for i,value in zip(missing,legacy):values[i]=value
            for value in values:
                if value is not None:results.append(json.loads(value))
        next_offset=offset+len(page)
        return {"symbol":symbol,"offset":offset,"limit":limit,"sessions_scanned":len(page),"results":results,"next_offset":next_offset if next_offset<len(sessions) else None,"total_sessions":len(sessions)}
    def raw_session(self,session,source="legacy",cursor="0",count=100):
        key=self.key("results") if source=="legacy" else self.key(f"results:{session}")
        scanned=self.redis.command("HSCAN",key,cursor,"MATCH",f"{session}|*","COUNT",count);next_cursor=str(scanned[0]);pairs=scanned[1] or [];results=[json.loads(pairs[i+1]) for i in range(0,len(pairs),2)]
        if next_cursor=="0" and source=="legacy":next_source="shard"
        elif next_cursor=="0":next_source=None
        else:next_source=source
        return {"session":session,"source":source,"cursor":str(cursor),"count":count,"results":results,"next_source":next_source,"next_cursor":"0" if next_source=="shard" else (next_cursor if next_source else None)}
    @staticmethod
    def _selected_summary(signals):
        mfes=sorted(float(x["mfe_pct"]) for x in signals);maes=sorted(float(x["mae_pct"]) for x in signals);n=len(signals)
        phases=Counter(str(x.get("phase") or "unknown") for x in signals);sessions={}
        for signal in signals:
            cell=sessions.setdefault(str(signal["session"]),{"count":0,"mfe_sum":0.0,"mae_sum":0.0,"mfe_ge_5":0,"mfe_ge_10":0})
            cell["count"]+=1;cell["mfe_sum"]+=float(signal["mfe_pct"]);cell["mae_sum"]+=float(signal["mae_pct"]);cell["mfe_ge_5"]+=float(signal["mfe_pct"])>=5;cell["mfe_ge_10"]+=float(signal["mfe_pct"])>=10
        per_session=[]
        for session,cell in sorted(sessions.items()):
            count=cell["count"];per_session.append({"session":session,"count":count,"avg_mfe_pct":round(cell["mfe_sum"]/count,4),"avg_mae_pct":round(cell["mae_sum"]/count,4),"mfe_ge_5_rate":round(cell["mfe_ge_5"]/count*100,2),"mfe_ge_10_rate":round(cell["mfe_ge_10"]/count*100,2)})
        ge5=sum(x>=5 for x in mfes);ge10=sum(x>=10 for x in mfes);avg_mfe=sum(mfes)/max(1,n);avg_mae=sum(maes)/max(1,n)
        return {"count":n,"avg_mfe_pct":round(avg_mfe,4),"median_mfe_pct":round(mfes[n//2],4) if n else 0,"avg_mae_pct":round(avg_mae,4),"median_mae_pct":round(maes[n//2],4) if n else 0,"mfe_ge_5_count":ge5,"mfe_ge_5_rate":round(ge5/max(1,n)*100,2),"mfe_ge_10_count":ge10,"mfe_ge_10_rate":round(ge10/max(1,n)*100,2),"mfe_to_abs_mae_ratio":round(avg_mfe/max(abs(avg_mae),1e-9),4) if n else 0,"phases":dict(phases.most_common()),"sessions":per_session}
    def threshold_analysis(self,opportunity=93.0,failure=35.0,progress=None):
        if float(opportunity)!=93 or float(failure)!=35:raise ValueError("this one-time validation is frozen at opportunity 93 / failure 35")
        if opportunity<88:raise ValueError("opportunity below 88 cannot be evaluated from stored READY signals")
        selected={(part,name):[] for part in ("development","holdout") for name in ("breakout_ready","confirmed_entry")};scanned=0
        for row in self.iter_results():
            scanned+=1
            if row.get("mode")=="approx" and row.get("partition") in ("development","holdout"):
                for name in ("breakout_ready","confirmed_entry"):
                    signal=row.get(name)
                    if signal and float(signal.get("opportunity",-1))>=opportunity and float(signal.get("failure_pressure",101))<=failure:
                        selected[(row["partition"],name)].append(dict(signal,session=row.get("session")))
            if progress and scanned%5000==0:progress(scanned)
        report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"mode":"approx","threshold":{"opportunity_min":opportunity,"failure_max":failure},"policy":{"threshold_selected_on":"development","holdout_use":"one-time frozen validation","minimum_evaluable_opportunity":88,"note":"Stored results can validate stricter READY filters only; thresholds below 88 require replay."},"source_rows_scanned":scanned,"partitions":{}}
        for part in ("development","holdout"):
            report["partitions"][part]={name:self._selected_summary(selected[(part,name)]) for name in ("breakout_ready","confirmed_entry")}
        report["comparison"]={}
        for part in ("development","holdout"):
            ready=report["partitions"][part]["breakout_ready"];entry=report["partitions"][part]["confirmed_entry"]
            report["comparison"][part]={"ready_minus_entry_count":ready["count"]-entry["count"],"ready_minus_entry_avg_mfe_pct":round(ready["avg_mfe_pct"]-entry["avg_mfe_pct"],4),"ready_minus_entry_avg_mae_pct":round(ready["avg_mae_pct"]-entry["avg_mae_pct"],4),"ready_minus_entry_mfe_ge_5_rate_points":round(ready["mfe_ge_5_rate"]-entry["mfe_ge_5_rate"],2)}
        self.redis.set_json(self.key("analysis:threshold:93:35"),report)
        if progress:progress(scanned)
        return report
    def threshold_analysis_result(self):return self.redis.get_json(self.key("analysis:threshold:93:35"),None)
    def simulation_candidates(self):
        cached=self.redis.get_json(self.key("simulation:93:35:candidates"),None)
        if cached is not None:return cached
        candidates=[]
        for row in self.iter_results():
            signal=row.get("confirmed_entry")
            if row.get("mode")=="approx" and signal and float(signal.get("opportunity",-1))>=93 and float(signal.get("failure_pressure",101))<=35:
                candidates.append({"session":row["session"],"partition":row["partition"],"symbol":row["symbol"],"signal":signal})
        candidates.sort(key=lambda x:(x["session"],x["signal"]["ts"],x["symbol"]));self.redis.set_json(self.key("simulation:93:35:candidates"),candidates);return candidates
    @staticmethod
    def simulate_trade(candidate,bars,assumption="conservative",policy="scaled"):
        signal=candidate["signal"];future=[b for b in bars if b.get("t") and b["t"]>signal["ts"]]
        base={"session":candidate["session"],"partition":candidate["partition"],"symbol":candidate["symbol"],"signal_ts":signal["ts"],"assumption":assumption,"policy":policy}
        if not future:return {**base,"status":"no_future_bars","entry":None,"exit":None,"return_pct":0.0,"duration_minutes":None,"ambiguous_bars":0,"targets_hit":[]}
        entry=float(future[0].get("o") or future[0]["c"]);original_stop=float(signal["stop"])
        if entry<=original_stop:return {**base,"status":"gap_below_stop","entry":entry,"exit":entry,"return_pct":0.0,"duration_minutes":0.0,"ambiguous_bars":0,"targets_hit":[]}
        risk=entry-original_stop;targets=[entry+1.5*risk,entry+2.5*risk,entry+4*risk];weights=[1.0,0.0,0.0] if policy=="full_t1" else [0.5,0.3,0.2]
        remaining=1.0;realized=0.0;stop=original_stop;hit=[];ambiguous=0;exit_price=None;exit_ts=None;status="end_of_window"
        for bar in future:
            o=float(bar.get("o") or bar["c"]);h=float(bar["h"]);l=float(bar["l"])
            next_target=targets[len(hit)] if len(hit)<3 and weights[len(hit)]>0 else None
            stop_hit=l<=stop or o<=stop;target_hit=next_target is not None and (h>=next_target or o>=next_target)
            if stop_hit and target_hit:ambiguous+=1
            def take_targets():
                nonlocal remaining,realized,stop,exit_price,exit_ts,status
                while len(hit)<3 and weights[len(hit)]>0 and (h>=targets[len(hit)] or o>=targets[len(hit)]):
                    i=len(hit);portion=min(remaining,weights[i]);fill=max(targets[i],o) if o>=targets[i] else targets[i];realized+=portion*(fill/entry-1);remaining-=portion;hit.append(f"T{i+1}");exit_price=fill;exit_ts=bar["t"]
                    if policy=="full_t1":remaining=0;status="t1_exit";break
                    stop=entry if i==0 else (targets[0] if i==1 else stop)
            def take_stop():
                nonlocal remaining,realized,exit_price,exit_ts,status
                if remaining>1e-12:
                    fill=min(stop,o) if o<=stop else stop;realized+=remaining*(fill/entry-1);remaining=0;exit_price=fill;exit_ts=bar["t"];status="stop_after_"+(hit[-1].lower() if hit else "entry")
            if assumption=="conservative":
                if stop_hit:take_stop()
                else:take_targets()
            else:
                take_targets()
                if remaining>1e-12 and (l<=stop or o<=stop):take_stop()
            if remaining<=1e-12:
                if len(hit)==3:status="t3_exit"
                break
        if remaining>1e-12:
            exit_price=float(future[-1]["c"]);exit_ts=future[-1]["t"];realized+=remaining*(exit_price/entry-1);remaining=0
        duration=(parse_dt(exit_ts)-parse_dt(future[0]["t"])).total_seconds()/60 if exit_ts else None
        return {**base,"status":status,"entry":round(entry,6),"initial_stop":round(original_stop,6),"targets":[round(x,6) for x in targets],"exit":round(exit_price,6) if exit_price is not None else None,"return_pct":round(realized*100,4),"duration_minutes":round(duration,1) if duration is not None else None,"ambiguous_bars":ambiguous,"targets_hit":hit}
    @staticmethod
    def simulation_summary(rows):
        values=[float(x["return_pct"]) for x in rows];n=len(values);wins=[x for x in values if x>0];losses=[x for x in values if x<0];ordered=sorted(values);gross_profit=sum(wins);gross_loss=abs(sum(losses));equity=1.0;peak=1.0;max_dd=0.0;streak=max_streak=current=0
        for value in values:
            equity*=1+value/100;peak=max(peak,equity);max_dd=max(max_dd,(peak-equity)/peak*100)
            current=current+1 if value<0 else 0;max_streak=max(max_streak,current)
        return {"trades":n,"wins":len(wins),"losses":len(losses),"flat":n-len(wins)-len(losses),"win_rate":round(len(wins)/max(1,n)*100,2),"avg_return_pct":round(avg(values),4),"median_return_pct":round(ordered[n//2],4) if n else 0,"profit_factor":round(gross_profit/max(gross_loss,1e-9),4),"compounded_return_pct":round((equity-1)*100,4),"max_drawdown_pct":round(max_dd,4),"max_loss_streak":max_streak,"ambiguous_trades":sum(x.get("ambiguous_bars",0)>0 for x in rows),"statuses":dict(Counter(x["status"] for x in rows).most_common())}
    def run_trade_simulation(self,progress=None):
        candidates=self.simulation_candidates();grouped={}
        for candidate in candidates:grouped.setdefault(candidate["session"],[]).append(candidate)
        case_key=self.key("simulation:93:35:cases");processed=0
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session)
            for begin in range(0,len(items),self.batch_size):
                batch=items[begin:begin+self.batch_size];symbols=[x["symbol"] for x in batch];fields=[f"{x['session']}|{x['symbol']}" for x in batch];existing=self.redis.command("HMGET",case_key,*fields) or [None]*len(fields);todo=[x for x,value in zip(batch,existing) if value is None]
                if todo:
                    sip=self.alpaca.bars([x["symbol"] for x in todo],start,end,"sip");writes=[]
                    for candidate in todo:
                        bars=sip.get(candidate["symbol"],[]);runs={f"{policy}_{assumption}":self.simulate_trade(candidate,bars,assumption,policy) for policy in ("full_t1","scaled") for assumption in ("conservative","optimistic")};writes.append((f"{candidate['session']}|{candidate['symbol']}",json.dumps({"candidate":candidate,"runs":runs},separators=(",",":"))))
                    self.hset_bounded(case_key,writes);time.sleep(self.delay)
                processed+=len(batch)
                if progress:progress(processed,len(candidates),session)
        rows=[];cursor="0"
        while True:
            scanned=self.redis.command("HSCAN",case_key,cursor,"COUNT",200);cursor=str(scanned[0]);pairs=scanned[1] or [];rows.extend(json.loads(pairs[i+1]) for i in range(0,len(pairs),2))
            if cursor=="0":break
        rows.sort(key=lambda x:(x["candidate"]["session"],x["candidate"]["signal"]["ts"],x["candidate"]["symbol"]));report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"threshold":{"opportunity_min":93,"failure_max":35},"fill_model":"entry at next SIP one-minute bar open; targets recomputed at 1.5R/2.5R/4R from stored stop","costs":"gross returns; commissions, spread and slippage not deducted","intrabar_bounds":{"conservative":"stop before target when both occur in one minute","optimistic":"targets before stop when both occur in one minute"},"candidate_count":len(candidates),"cases":rows,"summary":{}}
        for partition in ("development","holdout","all"):
            subset=rows if partition=="all" else [x for x in rows if x["candidate"]["partition"]==partition];report["summary"][partition]={}
            for key in ("full_t1_conservative","full_t1_optimistic","scaled_conservative","scaled_optimistic"):report["summary"][partition][key]=self.simulation_summary([x["runs"][key] for x in subset])
        self.redis.set_json(self.key("simulation:93:35:report"),report);return report
    def trade_simulation_result(self):return self.redis.get_json(self.key("simulation:93:35:report"),None)
    def trade_simulation_status(self):return self.redis.get_json(self.key("simulation:93:35:status"),None)
    def stop_width_report(self,stop_widths=None,policies=None,progress=None):
        stop_widths=stop_widths or [1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0];policies=policies or [("full_t1","conservative"),("scaled","conservative")]
        candidates=self.simulation_candidates();bars_key=self.key("stopwidth_test:bars");grouped={}
        for c in candidates:grouped.setdefault(c["session"],[]).append(c)
        all_bars={};total=len(candidates);processed=0
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session)
            for begin in range(0,len(items),self.batch_size):
                batch=items[begin:begin+self.batch_size];fields=[f"{session}|{x['symbol']}" for x in batch];existing=self.redis.command("HMGET",bars_key,*fields) or [None]*len(fields);todo=[x for x,value in zip(batch,existing) if value is None]
                for x,value in zip(batch,existing):
                    if value is not None:all_bars[f"{session}|{x['symbol']}"]=json.loads(value)
                if todo:
                    sip=self.alpaca.bars([x["symbol"] for x in todo],start,end,"sip");writes=[]
                    for x in todo:
                        bars=sip.get(x["symbol"],[]);all_bars[f"{session}|{x['symbol']}"]=bars;writes.append((f"{session}|{x['symbol']}",json.dumps(bars,separators=(",",":"))))
                    self.hset_bounded(bars_key,writes);time.sleep(self.delay)
                processed+=len(batch)
                if progress:progress(processed,total,session)
        report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"note":"Stop-width sensitivity test; reuses cached candidates, does not re-run the collector.","stop_widths_tested":stop_widths,"policies_tested":[f"{p}_{a}" for p,a in policies],"results":{}}
        for stop_pct in stop_widths:
            report["results"][str(stop_pct)]={}
            for policy,assumption in policies:
                key=f"{policy}_{assumption}";rows_all,rows_dev,rows_hold=[],[],[]
                for c in candidates:
                    bars=all_bars.get(f"{c['session']}|{c['symbol']}",[]);c_override=json.loads(json.dumps(c));c_override["signal"]["stop"]=round(float(c["signal"]["price"])*(1-stop_pct/100),6);c_override["signal"]["stop_pct"]=stop_pct
                    run=self.simulate_trade(c_override,bars,assumption=assumption,policy=policy);rows_all.append(run);(rows_dev if c["partition"]=="development" else rows_hold).append(run)
                report["results"][str(stop_pct)][key]={"all":self.simulation_summary(rows_all),"development":self.simulation_summary(rows_dev),"holdout":self.simulation_summary(rows_hold)}
        self.redis.set_json(self.key("stopwidth:93:35:report"),report);return report
    def stop_width_result(self):return self.redis.get_json(self.key("stopwidth:93:35:report"),None)
    def stop_width_status(self):return self.redis.get_json(self.key("stopwidth:93:35:status"),None)
    MICRO_FEATURE_CASES=[
        {"group":"1_clean_explosion","symbol":"MF","session":"2026-07-09","t":"2026-07-09T13:32:00Z","price":4.02,"mfe":38.68,"mae":-1.49},
        {"group":"1_clean_explosion","symbol":"SDOT","session":"2026-06-15","t":"2026-06-15T16:33:00Z","price":22.74,"mfe":18.43,"mae":-0.57},
        {"group":"1_clean_explosion","symbol":"BXBL","session":"2026-06-22","t":"2026-06-22T19:13:00Z","price":12.75,"mfe":19.62,"mae":-0.55},
        {"group":"1_clean_explosion","symbol":"CAST","session":"2026-07-17","t":"2026-07-17T18:10:00Z","price":2.46,"mfe":17.89,"mae":-0.61},
        {"group":"1_clean_explosion","symbol":"COCH","session":"2026-06-30","t":"2026-06-30T14:20:00Z","price":0.7298,"mfe":16.47,"mae":-1.32},
        {"group":"1_clean_explosion","symbol":"KITT","session":"2026-08-03","t":"2026-08-03T14:04:00Z","price":0.92,"mfe":15.22,"mae":0.01},
        {"group":"1_clean_explosion","symbol":"TURB","session":"2026-07-07","t":"2026-07-07T18:49:00Z","price":1.55,"mfe":10.96,"mae":-0.97},
        {"group":"1_clean_explosion","symbol":"VEEE","session":"2026-08-10","t":"2026-08-10T19:13:00Z","price":10.0498,"mfe":24.28,"mae":0.50},
        {"group":"1_clean_explosion","symbol":"ZYBT","session":"2026-08-19","t":"2026-08-19T13:43:00Z","price":1.60,"mfe":34.71,"mae":-1.25},
        {"group":"1_clean_explosion","symbol":"RDGT","session":"2026-08-26","t":"2026-08-26T16:58:00Z","price":0.8999,"mfe":20.01,"mae":-2.21},
        {"group":"2_similar_no_explosion","symbol":"BLDP","session":"2026-06-16","t":"2026-06-16T13:35:00Z","price":4.195,"mfe":5.60,"mae":-3.46},
        {"group":"2_similar_no_explosion","symbol":"ASYS","session":"2026-06-12","t":"2026-06-12T16:49:00Z","price":24.37,"mfe":5.49,"mae":-1.89},
        {"group":"2_similar_no_explosion","symbol":"ANL","session":"2026-07-06","t":"2026-07-06T18:59:00Z","price":10.47,"mfe":5.06,"mae":-1.10},
        {"group":"2_similar_no_explosion","symbol":"PCSA","session":"2026-08-07","t":"2026-08-07T18:17:00Z","price":2.51,"mfe":5.18,"mae":-0.40},
        {"group":"2_similar_no_explosion","symbol":"RKTO","session":"2026-07-29","t":"2026-07-29T14:54:00Z","price":0.7463,"mfe":6.57,"mae":-2.18},
        {"group":"2_similar_no_explosion","symbol":"ORIO","session":"2026-06-08","t":"2026-06-08T14:09:00Z","price":0.92,"mfe":6.52,"mae":-3.47},
        {"group":"2_similar_no_explosion","symbol":"INMB","session":"2026-06-18","t":"2026-06-18T19:08:00Z","price":1.375,"mfe":5.45,"mae":0.00},
        {"group":"2_similar_no_explosion","symbol":"PAVS","session":"2026-08-10","t":"2026-08-10T18:13:00Z","price":7.2581,"mfe":6.78,"mae":0.03},
        {"group":"2_similar_no_explosion","symbol":"CNDT","session":"2026-08-11","t":"2026-08-11T14:10:00Z","price":1.535,"mfe":5.54,"mae":-0.98},
        {"group":"2_similar_no_explosion","symbol":"AGIG","session":"2026-08-20","t":"2026-08-20T15:23:00Z","price":0.9895,"mfe":5.10,"mae":-4.47},
        {"group":"3_exploded_no_confirm","symbol":"PDC","session":"2026-07-14","t":"2026-07-14T13:59:00Z","price":3.80,"mfe":10.26,"mae":-6.58},
        {"group":"3_exploded_no_confirm","symbol":"EFOR","session":"2026-07-30","t":"2026-07-30T13:30:00Z","price":25.39,"mfe":11.70,"mae":1.57},
        {"group":"3_exploded_no_confirm","symbol":"OESX","session":"2026-08-05","t":"2026-08-05T13:34:00Z","price":14.21,"mfe":23.15,"mae":-0.42},
        {"group":"3_exploded_no_confirm","symbol":"CMMB","session":"2026-07-07","t":"2026-07-07T18:59:00Z","price":2.50,"mfe":13.20,"mae":-2.80},
        {"group":"3_exploded_no_confirm","symbol":"ZONE","session":"2026-06-29","t":"2026-06-29T14:10:00Z","price":0.76,"mfe":15.78,"mae":-1.34},
        {"group":"3_exploded_no_confirm","symbol":"GWH","session":"2026-06-29","t":"2026-06-29T13:39:00Z","price":0.89,"mfe":23.60,"mae":0.00},
        {"group":"3_exploded_no_confirm","symbol":"MASK","session":"2026-08-06","t":"2026-08-06T19:05:00Z","price":1.495,"mfe":13.71,"mae":-2.34},
        {"group":"3_exploded_no_confirm","symbol":"XHLD","session":"2026-08-27","t":"2026-08-27T15:37:00Z","price":8.64,"mfe":10.53,"mae":-6.25},
        {"group":"3_exploded_no_confirm","symbol":"AIRE","session":"2026-08-25","t":"2026-08-25T13:36:00Z","price":1.69,"mfe":10.06,"mae":-2.37},
        {"group":"3_exploded_no_confirm","symbol":"APRE","session":"2026-08-24","t":"2026-08-24T14:01:00Z","price":0.70,"mfe":21.43,"mae":-1.43},
    ]
    @staticmethod
    def extract_micro_features(bars,t_iso,session_open_iso):
        """كل الميزات تُحسب بشرط زمني صريح T-45m <= bar_time <= T (وقت الشمعة، مو عدد الشموع)،
        بدون أي معلومة بعد T. VWAP يُحسب حصراً من افتتاح الجلسة الرسمي (09:30 ET) إلى T،
        حتى لو امتدت نافذة الميزات الأخرى قبل الافتتاح (لالتقاط نشاط ما قبل السوق بالحالات القريبة من الافتتاح)."""
        t_dt=parse_dt(t_iso);window45_start=(t_dt-timedelta(minutes=45)).isoformat().replace("+00:00","Z")
        full_window=[b for b in bars if b["t"]<=t_iso]
        last45=[b for b in full_window if b["t"]>=window45_start]
        if len(last45)<5:return None
        vwap_bars=[b for b in full_window if b["t"]>=session_open_iso]
        cum_pv=0.0;cum_v=0.0
        for b in vwap_bars:
            typical=(float(b["h"])+float(b["l"])+float(b["c"]))/3.0;v=float(b["v"])
            cum_pv+=typical*v;cum_v+=v
        vwap_at_t=(cum_pv/cum_v) if cum_v>0 else None
        last_price=float(last45[-1]["c"])
        n=len(last45);mid=max(1,n//2);first_half=last45[:mid];second_half=last45[mid:]
        vol_first=sum(float(b["v"]) for b in first_half)/max(1,len(first_half))
        vol_second=sum(float(b["v"]) for b in second_half)/max(1,len(second_half))
        volume_acceleration=round(vol_second/vol_first,3) if vol_first>0 else None
        def dollar_vol(minutes):
            cutoff=(t_dt-timedelta(minutes=minutes)).isoformat().replace("+00:00","Z")
            recent=[b for b in last45 if b["t"]>=cutoff]
            return round(sum(float(b["v"])*float(b["c"]) for b in recent),2)
        range_first=[float(b["h"])-float(b["l"]) for b in first_half];range_second=[float(b["h"])-float(b["l"]) for b in second_half]
        avg_range_first=sum(range_first)/max(1,len(range_first));avg_range_second=sum(range_second)/max(1,len(range_second))
        range_expansion_ratio=round(avg_range_second/avg_range_first,3) if avg_range_first>0 else None
        bars_above_vwap=sum(1 for b in last45 if vwap_at_t is not None and float(b["c"])>=vwap_at_t)
        pct_time_above_vwap=round(bars_above_vwap/len(last45)*100,1) if vwap_at_t is not None else None
        lows=[float(b["l"]) for b in last45];window_low=min(lows);low_idx=lows.index(window_low)
        higher_lows_streak=0;prev_low=window_low
        for l in lows[low_idx+1:]:
            if l>=prev_low:higher_lows_streak+=1;prev_low=l
            else:break
        bars_since_window_low=len(last45)-1-low_idx
        highs=[float(b["h"]) for b in last45]
        resistance=max(highs[:-1]) if len(highs)>1 else highs[-1]
        distance_to_resistance_pct=round((resistance-last_price)/last_price*100,3) if last_price>0 else None
        touches=sum(1 for h in highs if resistance>0 and h>=resistance*0.995)
        close_locations=[];upper_wicks=[]
        for b in last45:
            o,h,l,c=float(b["o"]),float(b["h"]),float(b["l"]),float(b["c"]);rng=h-l
            close_locations.append((c-l)/rng if rng>0 else 0.5);upper_wicks.append((h-max(o,c))/rng if rng>0 else 0.0)
        avg_close_location=round(sum(close_locations)/len(close_locations),3);avg_upper_wick=round(sum(upper_wicks)/len(upper_wicks),3)
        up_vol=sum(float(b["v"]) for b in last45 if float(b["c"])>=float(b["o"]));down_vol=sum(float(b["v"]) for b in last45 if float(b["c"])<float(b["o"]))
        up_down_vol_ratio=round(up_vol/down_vol,3) if down_vol>0 else None
        first_open=float(last45[0]["o"])
        price_change_pct=round((last_price-first_open)/first_open*100,3) if first_open>0 else None
        avg_vol_45=sum(float(b["v"]) for b in last45)/len(last45)
        price_to_volume_efficiency=round(price_change_pct/(avg_vol_45/1000),6) if price_change_pct is not None and avg_vol_45>0 else None
        t_ny=t_dt.astimezone(NY);minutes_since_open=t_ny.hour*60+t_ny.minute-9*60-30;minutes_remaining_in_session=max(0,390-minutes_since_open)
        return {"bars_used_for_45m_window":len(last45),"window45_start_ts":window45_start,
            "vwap_bars_from_session_open":len(vwap_bars),
            "volume_acceleration_2nd_half_vs_1st_half":volume_acceleration,
            "dollar_volume_last_5m":dollar_vol(5),"dollar_volume_last_15m":dollar_vol(15),"dollar_volume_last_30m":dollar_vol(30),
            "range_expansion_ratio_2nd_half_vs_1st_half":range_expansion_ratio,
            "pct_time_above_vwap_last45m":pct_time_above_vwap,"vwap_at_t":round(vwap_at_t,4) if vwap_at_t else None,
            "bars_since_window_low":bars_since_window_low,"consecutive_higher_lows_after_window_low":higher_lows_streak,
            "resistance_proxy_price":round(resistance,4),"distance_to_resistance_pct":distance_to_resistance_pct,"resistance_touch_count_last45m":touches,
            "avg_close_location_in_candle_0to1":avg_close_location,"avg_upper_wick_ratio":avg_upper_wick,
            "up_volume_to_down_volume_ratio_last45m":up_down_vol_ratio,
            "price_change_pct_last45m":price_change_pct,"price_to_volume_efficiency":price_to_volume_efficiency,
            "minutes_remaining_in_regular_session":minutes_remaining_in_session}
    def micro_feature_report(self,progress=None):
        cases=self.MICRO_FEATURE_CASES;grouped={}
        for c in cases:grouped.setdefault(c["session"],[]).append(c)
        rows=[];raw_bars_key=self.key("micro_features:raw_bars");processed=0
        for session,items in sorted(grouped.items()):
            _,end=self.session_window(session)
            session_open=datetime.combine(date.fromisoformat(session),dtime(9,30),tzinfo=NY).astimezone(UTC).isoformat().replace("+00:00","Z")
            symbols=list({x["symbol"] for x in items})
            earliest_t=min(parse_dt(x["t"]) for x in items)
            fetch_start_dt=min(parse_dt(session_open),earliest_t-timedelta(minutes=60))
            sip=self.alpaca.bars(symbols,fetch_start_dt,end,"sip")
            writes=[]
            for c in items:
                bars=sorted(sip.get(c["symbol"],[]),key=lambda b:b["t"])
                writes.append((f"{c['session']}|{c['symbol']}|{c['t']}",json.dumps(bars,separators=(",",":"))))
                features=self.extract_micro_features(bars,c["t"],session_open)
                rows.append({"group":c["group"],"symbol":c["symbol"],"session":c["session"],"t":c["t"],"signal_price":c["price"],
                    "outcome_mfe_pct":c["mfe"],"outcome_mae_pct":c["mae"],"features":features})
                processed+=1
                if progress:progress(processed,len(cases),session)
            self.hset_bounded(raw_bars_key,writes)
            time.sleep(self.delay)
        report={"schema":1,"generated_at":now_iso(),"note":"Exploratory 30-case time-series feature extraction. Feature window is time-based (T-45m<=bar_time<=T), VWAP is the true regular-session VWAP from 09:30 ET to T, and near-open cases correctly include pre-market bars fetched back to T-60m. Raw bars are cached under 'micro_features:raw_bars' for audit. Not a statistical proof; a shape-checking pass before scaling. A fresh temporal holdout must be built before any rule is adopted.","cases":len(cases),"rows":rows}
        self.redis.set_json(self.key("micro_features:report"),report);return report
    def micro_feature_result(self):return self.redis.get_json(self.key("micro_features:report"),None)
    def micro_feature_status(self):return self.redis.get_json(self.key("micro_features:status"),None)
    OVERNIGHT_FOLLOWTHROUGH_CASES=[
        {"symbol":"OTLK","session":"2026-06-16","signal_ts":"2026-06-16T00:14:00Z","signal_price":1.17},
        {"symbol":"RXT","session":"2026-06-17","signal_ts":"2026-06-17T03:50:00Z","signal_price":6.30},
        {"symbol":"CDT","session":"2026-06-22","signal_ts":"2026-06-22T05:05:00Z","signal_price":1.28},
        {"symbol":"SOC","session":"2026-07-01","signal_ts":"2026-07-01T07:00:00Z","signal_price":3.34},
        {"symbol":"BJDX","session":"2026-07-07","signal_ts":"2026-07-07T01:59:00Z","signal_price":1.32},
        {"symbol":"BATL","session":"2026-07-08","signal_ts":"2026-07-08T02:07:00Z","signal_price":1.78},
        {"symbol":"BRNX","session":"2026-07-08","signal_ts":"2026-07-08T06:30:00Z","signal_price":1.07},
        {"symbol":"EHGO","session":"2026-07-13","signal_ts":"2026-07-13T05:58:00Z","signal_price":1.96},
        {"symbol":"IREN","session":"2026-07-20","signal_ts":"2026-07-20T03:51:00Z","signal_price":34.18},
        {"symbol":"VIVK","session":"2026-07-23","signal_ts":"2026-07-23T06:47:00Z","signal_price":1.58},
        {"symbol":"LESL","session":"2026-07-23","signal_ts":"2026-07-23T06:01:00Z","signal_price":1.42},
        {"symbol":"VIVK","session":"2026-07-24","signal_ts":"2026-07-24T07:25:00Z","signal_price":2.51},
        {"symbol":"SKHU","session":"2026-07-30","signal_ts":"2026-07-30T01:50:00Z","signal_price":12.83},
        {"symbol":"APLD","session":"2026-07-30","signal_ts":"2026-07-30T01:24:00Z","signal_price":23.70},
        {"symbol":"HYFM","session":"2026-08-05","signal_ts":"2026-08-05T02:47:00Z","signal_price":1.93},
        {"symbol":"AREC","session":"2026-08-07","signal_ts":"2026-08-07T02:43:00Z","signal_price":2.29},
        {"symbol":"SION","session":"2026-08-11","signal_ts":"2026-08-11T00:06:00Z","signal_price":4.45},
        {"symbol":"SCKT","session":"2026-08-12","signal_ts":"2026-08-12T03:46:00Z","signal_price":1.48},
        {"symbol":"BRNX","session":"2026-08-17","signal_ts":"2026-08-17T02:11:00Z","signal_price":3.70},
        {"symbol":"TRUG","session":"2026-08-17","signal_ts":"2026-08-17T07:35:00Z","signal_price":1.62},
        {"symbol":"BRNX","session":"2026-08-28","signal_ts":"2026-08-28T03:55:00Z","signal_price":3.65},
    ]
    def overnight_followthrough_report(self):
        cached=self.redis.get_json(self.key("overnight_followthrough:report"),None)
        if cached is not None:return cached
        cases=self.OVERNIGHT_FOLLOWTHROUGH_CASES;grouped={}
        for c in cases:grouped.setdefault(c["session"],[]).append(c)
        rows=[]
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session);symbols=list({x["symbol"] for x in items})
            sip=self.alpaca.bars(symbols,start,end,"sip")
            for c in items:
                bars=sorted(sip.get(c["symbol"],[]),key=lambda b:b["t"])
                pm_open_utc=datetime.combine(date.fromisoformat(session),dtime(4,0),tzinfo=NY).astimezone(UTC).isoformat().replace("+00:00","Z")
                mkt_open_utc=datetime.combine(date.fromisoformat(session),dtime(9,30),tzinfo=NY).astimezone(UTC).isoformat().replace("+00:00","Z")
                mkt_close_utc=datetime.combine(date.fromisoformat(session),dtime(16,0),tzinfo=NY).astimezone(UTC).isoformat().replace("+00:00","Z")
                overnight_bars=[b for b in bars if b["t"]<pm_open_utc]
                premarket_bars=[b for b in bars if pm_open_utc<=b["t"]<mkt_open_utc]
                regular_bars=[b for b in bars if mkt_open_utc<=b["t"]<mkt_close_utc]
                price=c["signal_price"]
                overnight_close=float(overnight_bars[-1]["c"]) if overnight_bars else None
                premarket_high=max((float(b["h"]) for b in premarket_bars),default=None)
                premarket_close=float(premarket_bars[-1]["c"]) if premarket_bars else None
                regular_open=float(regular_bars[0]["o"]) if regular_bars else None
                regular_high=max((float(b["h"]) for b in regular_bars),default=None)
                regular_close=float(regular_bars[-1]["c"]) if regular_bars else None
                day_high=max([v for v in [premarket_high,regular_high] if v is not None],default=None)
                def pct(x):return round((x/price-1)*100,2) if x is not None else None
                rows.append({"symbol":c["symbol"],"session":session,"signal_ts":c["signal_ts"],"signal_price":price,
                    "overnight_close_pct":pct(overnight_close),
                    "premarket_high_pct":pct(premarket_high),"premarket_close_pct":pct(premarket_close),
                    "regular_open_pct":pct(regular_open),"regular_high_pct":pct(regular_high),"regular_close_pct":pct(regular_close),
                    "day_high_pct":pct(day_high),
                    "had_premarket_bars":len(premarket_bars)>0,"had_regular_bars":len(regular_bars)>0})
            time.sleep(self.delay)
        report={"schema":1,"generated_at":now_iso(),"note":"For each overnight explosion, shows % move (relative to the overnight signal price) reached by end of premarket and by end of the regular session, plus each segment's high.","rows":rows}
        self.redis.set_json(self.key("overnight_followthrough:report"),report);return report
    @staticmethod
    def synth_entry_plan(price,past_bars,entry_max_stop_pct=6.0,entry_min_rr_t1=1.4):
        """يعيد نفس منطق build_entry_plan/calculate_atr من next_day_explosion_radar.py
        تماماً، مستخدم فقط لأن BREAKOUT_READY لا يخزّن وقفاً أصلاً (يُبنى فقط عند CONFIRMED_ENTRY)."""
        entry=float(price)
        if entry<=0 or len(past_bars)<8:return None
        period=14
        if len(past_bars)<period+1:atr=0.0
        else:
            trs=[];prev_close=float(past_bars[0].get("c") or past_bars[0].get("o") or entry)
            for b in past_bars[1:]:
                h=float(b["h"]);l=float(b["l"]);c=float(b.get("c") or b.get("o") or entry);trs.append(max(h-l,abs(h-prev_close),abs(l-prev_close)));prev_close=c
            recent=trs[-period:];atr=(sum(recent)/len(recent)) if recent else 0.0
        recent_lows=[float(b["l"]) for b in past_bars[-8:] if float(b["l"])>0];swing_low=min(recent_lows) if recent_lows else entry*0.97
        atr_stop=entry-max(atr*1.3,entry*0.015);stop=max(swing_low*0.998,atr_stop)
        stop_pct=max(0.0,(entry-stop)/entry*100)
        if stop>=entry or stop_pct<=0 or stop_pct>entry_max_stop_pct:return None
        risk=entry-stop
        return {"stop":round(stop,6),"stop_pct":round(stop_pct,4),"t1":round(entry+risk*max(entry_min_rr_t1,1.5),6),"t2":round(entry+risk*2.5,6),"t3":round(entry+risk*4.0,6)}
    def entry_compare_candidates(self,signal_field):
        cache_key=self.key(f"entrycompare:93:35:candidates:{signal_field}");cached=self.redis.get_json(cache_key,None)
        if cached is not None:return cached
        candidates=[]
        for row in self.iter_results():
            signal=row.get(signal_field)
            if row.get("mode")=="approx" and signal and float(signal.get("opportunity",-1))>=93 and float(signal.get("failure_pressure",101))<=35:
                candidates.append({"session":row["session"],"partition":row["partition"],"symbol":row["symbol"],"signal":signal})
        candidates.sort(key=lambda x:(x["session"],x["signal"]["ts"],x["symbol"]));self.redis.set_json(cache_key,candidates);return candidates
    def entry_compare_report(self,policies=None,progress=None):
        policies=policies or [("full_t1","conservative"),("scaled","conservative")]
        variants={"breakout_ready_entry":self.entry_compare_candidates("breakout_ready"),"confirmed_entry_entry":self.entry_compare_candidates("confirmed_entry")}
        bars_key=self.key("entrycompare_test:bars");all_candidates=[c for group in variants.values() for c in group];grouped={}
        for c in all_candidates:grouped.setdefault(c["session"],[]).append(c)
        all_bars={};total=len(all_candidates);processed=0
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session);symbols_seen=set()
            for begin in range(0,len(items),self.batch_size):
                batch=[x for x in items[begin:begin+self.batch_size] if x["symbol"] not in symbols_seen]
                for x in batch:symbols_seen.add(x["symbol"])
                if not batch:continue
                fields=[f"{session}|{x['symbol']}" for x in batch];existing=self.redis.command("HMGET",bars_key,*fields) or [None]*len(fields);todo=[x for x,value in zip(batch,existing) if value is None]
                for x,value in zip(batch,existing):
                    if value is not None:all_bars[f"{session}|{x['symbol']}"]=json.loads(value)
                if todo:
                    sip=self.alpaca.bars([x["symbol"] for x in todo],start,end,"sip");writes=[]
                    for x in todo:
                        bars=sip.get(x["symbol"],[]);all_bars[f"{session}|{x['symbol']}"]=bars;writes.append((f"{session}|{x['symbol']}",json.dumps(bars,separators=(",",":"))))
                    self.hset_bounded(bars_key,writes);time.sleep(self.delay)
                processed+=len(batch)
                if progress:progress(processed,total,session)
        report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"note":"Compares entering at BREAKOUT_READY (stop synthesized with the bot's own ATR/swing-low formula, since BREAKOUT_READY stores no plan) vs waiting for CONFIRMED_ENTRY. Reuses stored results; does not re-run the collector.","policies_tested":[f"{p}_{a}" for p,a in policies],"candidate_counts":{name:len(group) for name,group in variants.items()},"results":{}}
        for name,group in variants.items():
            report["results"][name]={}
            excluded_no_plan=0;usable=[]
            for c in group:
                bars=all_bars.get(f"{c['session']}|{c['symbol']}",[])
                if name=="breakout_ready_entry":
                    ts=c["signal"]["ts"];past=[b for b in bars if b.get("t") and b["t"]<=ts]
                    plan=self.synth_entry_plan(c["signal"]["price"],past)
                    if plan is None:excluded_no_plan+=1;continue
                    c=json.loads(json.dumps(c));c["signal"].update(plan)
                usable.append((c,bars))
            for policy,assumption in policies:
                key=f"{policy}_{assumption}";rows_all,rows_dev,rows_hold=[],[],[]
                for c,bars in usable:
                    run=self.simulate_trade(c,bars,assumption=assumption,policy=policy);rows_all.append(run);(rows_dev if c["partition"]=="development" else rows_hold).append(run)
                report["results"][name][key]={"all":self.simulation_summary(rows_all),"development":self.simulation_summary(rows_dev),"holdout":self.simulation_summary(rows_hold)}
            report["results"][name]["excluded_no_valid_stop_plan"]=excluded_no_plan;report["results"][name]["usable_candidates"]=len(usable)
        self.redis.set_json(self.key("entrycompare:93:35:report"),report);return report
    def entry_compare_result(self):return self.redis.get_json(self.key("entrycompare:93:35:report"),None)
    def entry_compare_status(self):return self.redis.get_json(self.key("entrycompare:93:35:status"),None)
    @staticmethod
    def diagnose_trade(candidate,bars):
        signal=candidate["signal"];past=[b for b in bars if b.get("t") and b["t"]<=signal["ts"]];future=[b for b in bars if b.get("t") and b["t"]>signal["ts"]];base={"session":candidate["session"],"partition":candidate["partition"],"symbol":candidate["symbol"],"signal_ts":signal["ts"],"opportunity":signal["opportunity"],"failure_pressure":signal["failure_pressure"],"stored_stop_pct":signal.get("stop_pct")}
        if not future:return {**base,"classification":"no_future_bars"}
        entry=float(future[0].get("o") or future[0]["c"]);stop=float(signal["stop"]);risk=entry-stop
        if risk<=0:return {**base,"classification":"gap_below_stop","entry":entry,"stop":stop}
        targets=[entry+1.5*risk,entry+2.5*risk,entry+4*risk];stop_i=None;target_i=[None,None,None]
        for i,bar in enumerate(future):
            o=float(bar.get("o") or bar["c"]);h=float(bar["h"]);l=float(bar["l"])
            if stop_i is None and (l<=stop or o<=stop):stop_i=i
            for j,target in enumerate(targets):
                if target_i[j] is None and (h>=target or o>=target):target_i[j]=i
        t1_i=target_i[0];ambiguous=stop_i is not None and t1_i==stop_i
        if stop_i is None and t1_i is not None:classification="t1_before_stop"
        elif stop_i is not None and t1_i is None:classification="stop_never_recovered_t1"
        elif stop_i is None:classification="neither"
        elif t1_i<stop_i:classification="t1_before_stop"
        elif t1_i>stop_i:classification="stop_then_recovered_t1"
        else:classification="same_minute_ambiguous"
        after_stop=future[stop_i+1:] if stop_i is not None else [];post_high=max((float(x["h"]) for x in after_stop),default=entry);signal_bar=past[-1] if past else None;prior=past[-21:-1];prior_vol=avg(float(x.get("v",0)) for x in prior);signal_vol=float(signal_bar.get("v",0)) if signal_bar else 0;bar_range=float(signal_bar["h"])-float(signal_bar["l"]) if signal_bar else 0;close=float(signal_bar["c"]) if signal_bar else entry;open_=float(signal_bar["o"]) if signal_bar else entry
        features={"entry_gap_pct":round((entry/float(signal["price"])-1)*100,4),"risk_pct":round(risk/entry*100,4),"signal_upper_wick_ratio":round((float(signal_bar["h"])-max(open_,close))/max(bar_range,1e-9),4) if signal_bar else None,"signal_close_location":round((close-float(signal_bar["l"]))/max(bar_range,1e-9),4) if signal_bar else None,"signal_volume_ratio":round(signal_vol/max(prior_vol,1),4),"momentum_5m_pct":round((close/float(past[-6]["c"])-1)*100,4) if len(past)>=6 else None,"resistance_distance_pct":round((float(signal["price"])/max(float(signal["resistance"]),1e-9)-1)*100,4)}
        return {**base,"classification":classification,"entry":round(entry,6),"stop":round(stop,6),"targets":[round(x,6) for x in targets],"stop_time":future[stop_i]["t"] if stop_i is not None else None,"t1_time":future[t1_i]["t"] if t1_i is not None else None,"minutes_to_stop":round((parse_dt(future[stop_i]["t"])-parse_dt(future[0]["t"])).total_seconds()/60,1) if stop_i is not None else None,"minutes_to_t1":round((parse_dt(future[t1_i]["t"])-parse_dt(future[0]["t"])).total_seconds()/60,1) if t1_i is not None else None,"post_stop_mfe_pct":round((post_high/entry-1)*100,4) if stop_i is not None else None,"event_same_minute":ambiguous,"features":features}
    @staticmethod
    def diagnostic_summary(rows):
        groups=Counter(x.get("classification","unknown") for x in rows);recovered=[x for x in rows if x.get("classification")=="stop_then_recovered_t1"];failed=[x for x in rows if x.get("classification")=="stop_never_recovered_t1"]
        feature_names=("entry_gap_pct","risk_pct","signal_upper_wick_ratio","signal_close_location","signal_volume_ratio","momentum_5m_pct","resistance_distance_pct")
        def feature_means(items):return {name:round(avg(x.get("features",{}).get(name) for x in items if x.get("features",{}).get(name) is not None),4) for name in feature_names}
        stopped=[x for x in rows if x.get("stop_time")]
        return {"cases":len(rows),"classifications":dict(groups.most_common()),"stopped_cases":len(stopped),"stop_then_recovered_t1":len(recovered),"stop_recovery_rate":round(len(recovered)/max(1,len(stopped))*100,2),"median_minutes_to_stop":round(sorted(x["minutes_to_stop"] for x in stopped if x.get("minutes_to_stop") is not None)[len([x for x in stopped if x.get("minutes_to_stop") is not None])//2],2) if stopped else None,"same_minute_ambiguous":sum(bool(x.get("event_same_minute")) for x in rows),"feature_means":{"t1_before_stop":feature_means([x for x in rows if x.get("classification")=="t1_before_stop"]),"stop_then_recovered_t1":feature_means(recovered),"stop_never_recovered_t1":feature_means(failed)}}
    def run_stop_diagnostic(self,progress=None):
        candidates=self.simulation_candidates();grouped={}
        for candidate in candidates:grouped.setdefault(candidate["session"],[]).append(candidate)
        case_key=self.key("diagnostic:93:35:cases");processed=0
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session)
            for begin in range(0,len(items),self.batch_size):
                batch=items[begin:begin+self.batch_size];fields=[f"{x['session']}|{x['symbol']}" for x in batch];existing=self.redis.command("HMGET",case_key,*fields) or [None]*len(fields);todo=[x for x,value in zip(batch,existing) if value is None]
                if todo:
                    sip=self.alpaca.bars([x["symbol"] for x in todo],start,end,"sip");writes=[(f"{x['session']}|{x['symbol']}",json.dumps(self.diagnose_trade(x,sip.get(x["symbol"],[])),separators=(",",":"))) for x in todo];self.hset_bounded(case_key,writes);time.sleep(self.delay)
                processed+=len(batch)
                if progress:progress(processed,len(candidates),session)
        rows=[];cursor="0"
        while True:
            scanned=self.redis.command("HSCAN",case_key,cursor,"COUNT",200);cursor=str(scanned[0]);pairs=scanned[1] or [];rows.extend(json.loads(pairs[i+1]) for i in range(0,len(pairs),2))
            if cursor=="0":break
        rows.sort(key=lambda x:(x["session"],x["signal_ts"],x["symbol"]));report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"threshold":{"opportunity_min":93,"failure_max":35},"purpose":"determine whether losses come from premature stops or failed entries","policy":"descriptive diagnosis; do not tune on holdout","summary":{},"cases":rows}
        for partition in ("development","holdout","all"):report["summary"][partition]=self.diagnostic_summary(rows if partition=="all" else [x for x in rows if x["partition"]==partition])
        self.redis.set_json(self.key("diagnostic:93:35:report"),report);return report
    def stop_diagnostic_result(self):return self.redis.get_json(self.key("diagnostic:93:35:report"),None)
    def stop_diagnostic_status(self):return self.redis.get_json(self.key("diagnostic:93:35:status"),None)
    def build_explosion_catalog(self,progress=None):
        cases=[];scanned=0
        for row in self.iter_results():
            scanned+=1
            if row.get("mode")=="approx":
                for signal_type in ("breakout_ready","confirmed_entry"):
                    signal=row.get(signal_type)
                    if signal and float(signal.get("mfe_pct",0))>=5:
                        cases.append({"session":row["session"],"partition":row["partition"],"symbol":row["symbol"],"signal_type":signal_type,"signal_ts":signal["ts"],"phase":signal.get("phase"),"price":signal.get("price"),"opportunity":signal.get("opportunity"),"failure_pressure":signal.get("failure_pressure"),"mfe_pct":signal.get("mfe_pct"),"mae_pct":signal.get("mae_pct"),"time_to_mfe_minutes":signal.get("time_to_mfe_minutes"),"forward_bars":signal.get("forward_bars")})
            if progress and scanned%5000==0:progress(scanned)
        cases.sort(key=lambda x:(-float(x["mfe_pct"]),x["session"],x["symbol"],x["signal_type"]));summary={}
        for signal_type in ("breakout_ready","confirmed_entry"):
            selected=[x for x in cases if x["signal_type"]==signal_type];summary[signal_type]={"mfe_ge_5":len(selected),"mfe_ge_10":sum(float(x["mfe_pct"])>=10 for x in selected),"mfe_ge_20":sum(float(x["mfe_pct"])>=20 for x in selected),"mfe_ge_50":sum(float(x["mfe_pct"])>=50 for x in selected),"development_ge_5":sum(x["partition"]=="development" for x in selected),"holdout_ge_5":sum(x["partition"]=="holdout" for x in selected),"unique_symbol_sessions_ge_5":len({(x["session"],x["symbol"]) for x in selected})}
        report={"schema":1,"generated_at":now_iso(),"source_prefix":self.prefix,"source_rows_scanned":scanned,"scope":"approx signals with MFE >= 5%, sorted by strongest MFE","limitation":"Rejected cases without BREAKOUT_READY have no stored reference outcome and cannot be classified as missed explosions without additional price retrieval.","summary":summary,"cases":cases};self.redis.set_json(self.key("explosions:catalog"),report)
        if progress:progress(scanned)
        return report
    def explosion_catalog(self):return self.redis.get_json(self.key("explosions:catalog"),None)
    def explosion_catalog_status(self):return self.redis.get_json(self.key("explosions:status"),None)
    @staticmethod
    def big_move_summary(cases):
        with_entry=[x for x in cases if x["entry"] is not None];delays=sorted(x["entry_delay_minutes"] for x in with_entry);times=sorted(x["ready"]["time_to_mfe_minutes"] for x in cases if x["ready"].get("time_to_mfe_minutes") is not None)
        return {"cases":len(cases),"mfe_ge_50":sum(float(x["ready"]["mfe_pct"])>=50 for x in cases),"with_confirmed_entry":len(with_entry),"without_confirmed_entry":len(cases)-len(with_entry),"entry_conversion_rate":round(len(with_entry)/max(1,len(cases))*100,2),"entry_retained_mfe_ge_5":sum(float(x["entry"].get("mfe_pct",0))>=5 for x in with_entry),"entry_retained_mfe_ge_10":sum(float(x["entry"].get("mfe_pct",0))>=10 for x in with_entry),"entry_retained_mfe_ge_20":sum(float(x["entry"].get("mfe_pct",0))>=20 for x in with_entry),"median_entry_delay_minutes":round(delays[len(delays)//2],2) if delays else None,"median_time_to_ready_mfe_minutes":round(times[len(times)//2],2) if times else None,"ready_mfe_within_15_minutes":sum(x["ready"].get("time_to_mfe_minutes") is not None and float(x["ready"]["time_to_mfe_minutes"])<=15 for x in cases),"ready_mfe_within_60_minutes":sum(x["ready"].get("time_to_mfe_minutes") is not None and float(x["ready"]["time_to_mfe_minutes"])<=60 for x in cases),"ready_phases":dict(Counter(x["ready"].get("phase","unknown") for x in cases).most_common()),"ready_opportunity_bands":{"below_90":sum(float(x["ready"].get("opportunity",0))<90 for x in cases),"90_to_below_93":sum(90<=float(x["ready"].get("opportunity",0))<93 for x in cases),"93_plus":sum(float(x["ready"].get("opportunity",0))>=93 for x in cases)}}
    def build_big_move_review(self,progress=None):
        raw_cases=[];scanned=0
        for row in self.iter_results():
            scanned+=1
            ready=row.get("breakout_ready")
            if row.get("mode")=="approx" and ready and float(ready.get("mfe_pct",0))>=20:
                entry=row.get("confirmed_entry");delay=round((parse_dt(entry["ts"])-parse_dt(ready["ts"])).total_seconds()/60,1) if entry else None
                raw_cases.append({"session":row["session"],"partition":row["partition"],"symbol":row["symbol"],"raw_ready":ready,"raw_entry":entry,"entry_delay_minutes":delay,"block_reasons_minute_occurrences":row.get("block_reasons",{}),"unavailable_features":row.get("unavailable_features",{})})
            if progress and scanned%5000==0:progress(scanned)
        grouped={}
        for item in raw_cases:grouped.setdefault(item["session"],[]).append(item)
        cases=[];excluded=[]
        for session,items in sorted(grouped.items()):
            start,end=self.session_window(session)
            for begin in range(0,len(items),self.batch_size):
                batch=items[begin:begin+self.batch_size];symbols=[x["symbol"] for x in batch];sip=self.alpaca.bars(symbols,start,end,"sip","split");boats=self.alpaca.bars(symbols,start,end,"boats","split")
                for item in batch:
                    bars=self.merge_bars(sip.get(item["symbol"],[]),boats.get(item["symbol"],[]));by_time={x["t"]:x for x in bars}
                    def adjusted(signal):
                        if not signal or signal["ts"] not in by_time:return None
                        rebased={**signal,"price":float(by_time[signal["ts"]]["c"])};return self.outcome(rebased,bars)
                    ready=adjusted(item["raw_ready"]);entry=adjusted(item["raw_entry"]);raw_mfe=float(item["raw_ready"]["mfe_pct"]);adjusted_mfe=float(ready["mfe_pct"]) if ready else None;contaminated=adjusted_mfe is None or abs(raw_mfe-adjusted_mfe)>max(5,abs(raw_mfe)*.25)
                    case={**item,"ready":ready,"entry":entry,"raw_ready_mfe_pct":raw_mfe,"split_adjusted_ready_mfe_pct":adjusted_mfe,"corporate_action_contaminated":contaminated}
                    (cases if ready and adjusted_mfe>=20 else excluded).append(case)
                time.sleep(self.delay)
                if progress:progress(scanned)
        cases.sort(key=lambda x:(-float(x["ready"]["mfe_pct"]),x["session"],x["symbol"]));excluded.sort(key=lambda x:-float(x["raw_ready_mfe_pct"]));report={"schema":2,"generated_at":now_iso(),"source_prefix":self.prefix,"source_rows_scanned":scanned,"scope":"Split-adjusted Approx BREAKOUT_READY cases with MFE >= 20%","bar_adjustment":"split","raw_candidate_count":len(raw_cases),"clean_case_count":len(cases),"excluded_after_split_adjustment":len(excluded),"caution":"block_reasons_minute_occurrences are aggregate failed-minute counts, not a definitive single reason that prevented entry","summary":{},"cases":cases,"excluded_cases":excluded}
        for partition in ("development","holdout","all"):report["summary"][partition]=self.big_move_summary(cases if partition=="all" else [x for x in cases if x["partition"]==partition])
        self.redis.set_json(self.key("big_moves:report"),report)
        if progress:progress(scanned)
        return report
    def big_move_review(self):return self.redis.get_json(self.key("big_moves:report"),None)
    def big_move_status(self):return self.redis.get_json(self.key("big_moves:status"),None)
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
