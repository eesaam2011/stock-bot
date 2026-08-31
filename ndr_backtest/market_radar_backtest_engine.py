from __future__ import annotations

import json, math, os, statistics, time
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta

from ndr_backtest_engine import Alpaca, NY, RedisREST, UTC, now_iso, parse_dt


def safe(value, default=0.0):
    try:return float(value)
    except (TypeError,ValueError):return float(default)


def linear(value,minimum,maximum,points):
    value=safe(value)
    if value<=minimum:return 0.0
    if value>=maximum:return float(points)
    return (value-minimum)/(maximum-minimum)*points


class MarketRadarBacktest:
    """Resumable technical approximation of Market Radar over the completed NDR universe."""
    def __init__(self,redis_client=None,alpaca_client=None):
        self.redis=redis_client or RedisREST();self.alpaca=alpaca_client or Alpaca()
        self.source_prefix=os.getenv("NDR_BT_REDIS_PREFIX","next_day_radar_backtest_v3")
        self.prefix=os.getenv("MR_BT_REDIS_PREFIX","market_radar_backtest_v1")
        self.batch_size=max(1,int(os.getenv("MR_BT_SYMBOL_BATCH_SIZE","30")))
        self.delay=max(0,float(os.getenv("MR_BT_REQUEST_DELAY_SECONDS","0.15")))
        self.pause_requested=False;self._manifest=None
    def key(self,suffix):return f"{self.prefix}:{suffix}"
    def source_key(self,suffix):return f"{self.source_prefix}:{suffix}"
    def status(self):return self.redis.get_json(self.key("status"),{"status":"IDLE","phase":"READY","processed":0})
    def save_status(self,**updates):
        state=self.status();state.update(updates);state["updated_at"]=now_iso();self.redis.set_json(self.key("status"),state);return state
    def hset_bounded(self,key,pairs,max_fields=10,max_bytes=200000):
        chunk=[];size=0
        for field,value in pairs:
            pair_size=len(field.encode())+len(value.encode())
            if chunk and (len(chunk)>=max_fields or size+pair_size>max_bytes):
                self.redis.command("HSET",key,*[item for pair in chunk for item in pair]);chunk=[];size=0
            chunk.append((field,value));size+=pair_size
        if chunk:self.redis.command("HSET",key,*[item for pair in chunk for item in pair])
    def manifest(self):
        if self._manifest is None:self._manifest=self.redis.get_json(self.source_key("manifest"),None)
        if not self._manifest:raise RuntimeError("NDR manifest is missing")
        return self._manifest
    @staticmethod
    def session_window(session):
        day=date.fromisoformat(session)
        return datetime.combine(day-timedelta(days=1),dtime(16),NY).astimezone(UTC),datetime.combine(day,dtime(16),NY).astimezone(UTC)
    @staticmethod
    def merge_bars(sip,boats):
        merged={}
        for feed,rows in (("sip",sip),("boats",boats)):
            for raw in rows or []:
                if not raw.get("t"):continue
                et=parse_dt(raw["t"]).astimezone(NY);minute=et.hour*60+et.minute;overnight=minute>=1200 or minute<240
                if (feed=="boats")==overnight:merged[raw["t"]]=dict(raw,feed=feed)
        return sorted(merged.values(),key=lambda x:x["t"])
    @staticmethod
    def ema(values,span):
        if not values:return 0.0
        alpha=2/(span+1);out=float(values[0])
        for value in values[1:]:out=alpha*float(value)+(1-alpha)*out
        return out
    @staticmethod
    def vwap(rows):
        volume=sum(max(0,safe(x.get("v"))) for x in rows)
        return sum(((safe(x.get("h"))+safe(x.get("l"))+safe(x.get("c")))/3)*max(0,safe(x.get("v"))) for x in rows)/volume if volume>0 else 0.0
    @staticmethod
    def atr(rows,period=14):
        if len(rows)<period+2:return 0.0
        tr=[]
        for i,row in enumerate(rows):
            high,low=safe(row.get("h")),safe(row.get("l"));previous=safe(rows[i-1].get("c")) if i else safe(row.get("c"))
            tr.append(max(abs(high-low),abs(high-previous),abs(low-previous)))
        return sum(tr[-period:])/period
    @staticmethod
    def rvol(rows):
        if len(rows)<30:return 0.0
        volumes=[safe(x.get("v")) for x in rows];recent=sum(volumes[-5:])/5;historical=volumes[:-5][-120:]
        if not historical:return 0.0
        base=statistics.median(historical) or sum(historical)/len(historical)
        return recent/base if base>0 else 0.0
    @staticmethod
    def volume_accel(rows):
        if len(rows)<12:return 0.0
        volumes=[safe(x.get("v")) for x in rows];last=sum(volumes[-3:])/3;previous=sum(volumes[-10:-3])/7
        return last/previous if previous>0 else 0.0
    @staticmethod
    def obv_rising(rows):
        if len(rows)<15:return False
        values=[0.0]
        for previous,current in zip(rows,rows[1:]):
            direction=1 if safe(current.get("c"))>safe(previous.get("c")) else (-1 if safe(current.get("c"))<safe(previous.get("c")) else 0)
            values.append(values[-1]+direction*safe(current.get("v")))
        return values[-1]>MarketRadarBacktest.ema(values,10) and values[-1]>values[-3]
    @staticmethod
    def resistance(rows,lookback=80):
        if len(rows)<30:return {"resistance":0.0,"distance_pct":999.0,"breakout":False,"touches":0}
        recent=rows[-lookback:];current_close=safe(recent[-1].get("c"));current_open=safe(recent[-1].get("o"))
        if current_close<=0:return {"resistance":0.0,"distance_pct":999.0,"breakout":False,"touches":0}
        history=recent[:-1];levels=[max(safe(x.get("o")),safe(x.get("c"))) for x in history];atr=MarketRadarBacktest.atr(history)
        clusters=[]
        for position,level in enumerate(levels):
            match=None
            for cluster in clusters:
                center=statistics.median(cluster["levels"]);tolerance=min(max(center*.003,atr*.15),center*.01)
                if abs(level-center)<=tolerance:match=cluster;break
            if match:match["levels"].append(level);match["last_position"]=position
            else:clusters.append({"levels":[level],"last_position":position})
        candidates=[]
        for cluster in clusters:
            level=statistics.median(cluster["levels"]);touches=len(cluster["levels"])
            if level<=0 or touches<2:continue
            distance=(level-current_close)/current_close*100
            if -4<=distance<=8:candidates.append({"level":level,"touches":touches,"distance_pct":distance,"quality_score":touches*10+cluster["last_position"]/max(1,len(levels))*5-abs(distance)})
        if not candidates:return {"resistance":0.0,"distance_pct":999.0,"breakout":False,"touches":0}
        above=[x for x in candidates if x["distance_pct"]>=-.2];selected=min(above,key=lambda x:(max(x["distance_pct"],0),-x["touches"],-x["quality_score"])) if above else max(candidates,key=lambda x:x["quality_score"])
        resistance=selected["level"];buffer=max(resistance*.001,atr*.05);previous=[safe(x.get("c")) for x in recent[-4:-1]]
        breakout=current_close>resistance+buffer and current_close>current_open and any(x<=resistance+buffer for x in previous if x>0) and selected["touches"]>=2
        return {"resistance":resistance,"distance_pct":selected["distance_pct"],"breakout":breakout,"touches":selected["touches"]}
    @staticmethod
    def trend_15m(rows):
        buckets=[]
        for row in rows:
            stamp=parse_dt(row["t"]);bucket=stamp.replace(minute=stamp.minute//15*15,second=0,microsecond=0)
            if not buckets or buckets[-1][0]!=bucket:buckets.append([bucket,safe(row.get("c"))])
            else:buckets[-1][1]=safe(row.get("c"))
        closes=[x[1] for x in buckets]
        if len(closes)<25:return False
        now=MarketRadarBacktest.ema(closes,20);previous=MarketRadarBacktest.ema(closes[:-2],20)
        return closes[-1]>now or now>previous
    @staticmethod
    def core_score(price,vwap,rvol,accel,obv,atr_pct,trend,breakout,distance,spread_points=0,float_points=0,news_bonus=0):
        score=linear(rvol,2.5,8,20)+linear(accel,1.2,4,16)+(12 if price>vwap else 0)+(10 if obv else 0)+linear(atr_pct,1,8,8)+(8 if trend else 0)+float_points+(12 if breakout else (linear(2-distance,0,2,5) if 0<=distance<=2 else 0))+spread_points+min(news_bonus,6)
        return min(score,100)
    @staticmethod
    def score_variant(base,variant,rvol,accel,price,vwap,obv,trend,breakout,distance,atr_pct):
        if variant=="technical_lower":float_points=spread_points=news=0;low_float=positive=False
        else:float_points=10;spread_points=4;news=6;low_float=positive=True
        core=MarketRadarBacktest.core_score(price,vwap,rvol,accel,obv,atr_pct,trend,breakout,distance,spread_points,float_points,news)
        multiplier=1.0
        if low_float and rvol>=4:multiplier+=.05
        if rvol>=4 and accel>=2:multiplier+=.06
        if price>vwap and obv and trend:multiplier+=.04
        if breakout and -2<=distance<=0 and atr_pct>=1.5:multiplier+=.05
        if positive and rvol>=4:multiplier+=.03
        penalties=5 if atr_pct<1.5 else 0
        if not breakout and 0<=distance<=.3:penalties+=8
        elif not breakout and distance<-.3:penalties+=6
        return min(100,max(0,core*min(multiplier,1.2)-penalties)),core
    @staticmethod
    def entry_quality(rows,resistance):
        if len(rows)<25:return "allow",[]
        bar,previous=rows[-1],rows[-2];o,h,l,c=(safe(bar.get(k)) for k in ("o","h","l","c"));span=max(0,h-l);wick=(h-max(o,c))/span if span else 0;closepos=(c-l)/span if span else .5
        volumes=[safe(x.get("v")) for x in rows[-22:-2]];ratio=safe(bar.get("v"))/(sum(volumes)/len(volumes)) if volumes and sum(volumes)>0 else 0;progress=(c/o-1)*100 if o>0 else 0
        pierced=resistance>0 and h>resistance*1.001;below=resistance>0 and c<resistance;lower_high=safe(previous.get("h"))>0 and h<safe(previous.get("h"))*.998;lost=c<MarketRadarBacktest.vwap(rows)
        upper=wick>=.45 and closepos<=.55;absorption=ratio>=1.8 and abs(progress)<=.2 and (wick>=.25 or below);weak=ratio<.9 and (pierced or (resistance>0 and c>resistance))
        signals=[]
        if upper:signals.append("upper_wick")
        if pierced and below:signals.append("failed_breakout")
        if absorption:signals.append("absorption")
        if lower_high:signals.append("lower_high")
        if lost:signals.append("lost_vwap")
        if weak:signals.append("weak_breakout_volume")
        delay=(upper and (below or absorption or lower_high or lost)) or (pierced and below and (weak or lower_high or upper)) or (weak and (upper or below))
        return ("delay" if delay else "allow"),signals
    @staticmethod
    def trade_plan(price,atr,vwap,resistance,breakout):
        candidates=[];atr_stop=price-max(atr*1.5,price*.015)
        if 0<atr_stop<price:candidates.append(atr_stop)
        if 0<vwap<price:candidates.append(vwap*.995)
        if breakout and 0<resistance<price:candidates.append(resistance*.995)
        if not candidates:return None
        stop=max(candidates);stop=min(stop,price*.9875);stop=max(stop,price*.94);risk=price-stop
        if risk<=0:return None
        return {"stop":stop,"t1":price+max(atr*1.2,risk*1.5)}
    @staticmethod
    def simulate_full_t1(entry,future,plan):
        for bar in future:
            opened=safe(bar.get("o") or bar.get("c"));low=safe(bar.get("l"));high=safe(bar.get("h"))
            if opened<=plan["stop"]:return "gap_below_stop",(opened/entry-1)*100
            if opened>=plan["t1"]:return "t1",(opened/entry-1)*100
            if low<=plan["stop"]:return "stop",(plan["stop"]/entry-1)*100
            if high>=plan["t1"]:return "t1",(plan["t1"]/entry-1)*100
        close=safe(future[-1].get("c"));return "end_of_window",(close/entry-1)*100
    def replay(self,session,symbol,bars):
        regular=[]
        for i,bar in enumerate(bars):
            et=parse_dt(bar["t"]).astimezone(NY);minute=et.hour*60+et.minute
            if 570<=minute<960:regular.append(i)
        thresholds=(78,82,86,90,93);signals={name:{**{str(level):None for level in thresholds},"live_policy":None} for name in ("technical_lower","technical_upper")};blocks=Counter();evaluated=0
        for i in regular:
            bar=bars[i]
            history=bars[max(0,i-159):i+1]
            if len(history)<40:blocks["insufficient_bars"]+=1;continue
            price=safe(history[-1].get("c"));day_rows=[x for x in bars[:i+1] if parse_dt(x["t"]).astimezone(NY).date()==date.fromisoformat(session)]
            dollar_volume=price*sum(safe(x.get("v")) for x in day_rows)
            if not .5<=price<=40:blocks["price"]+=1;continue
            if dollar_volume<250000:blocks["dollar_volume_all_variants"]+=1;continue
            day_volume=sum(safe(x.get("v")) for x in day_rows);day_high=max((safe(x.get("h")) for x in day_rows),default=0);minute_volume=safe(bar.get("v"));observable_hot=minute_volume>=20000 or (day_high>0 and price>=day_high*.96)
            vwap=self.vwap(history)
            if vwap<=0 or price<vwap:blocks["below_vwap"]+=1;continue
            rvol=self.rvol(history)
            if rvol<2.5:blocks["rvol"]+=1;continue
            atr=self.atr(history);atr_pct=atr/price*100 if price else 0
            if atr_pct<1:blocks["atr"]+=1;continue
            accel=self.volume_accel(history);obv=self.obv_rising(history);trend=self.trend_15m(bars[:i+1]);res=self.resistance(history);evaluated+=1
            quality,quality_signals=self.entry_quality(history,res["resistance"])
            if quality!="allow":blocks["entry_quality_delay"]+=1;continue
            next_bar=bars[i+1] if i+1<len(bars) else None
            if not next_bar:continue
            entry=safe(next_bar.get("o") or next_bar.get("c"));future=[x for x in bars[i+1:] if parse_dt(x["t"]).astimezone(NY).date()==date.fromisoformat(session) and parse_dt(x["t"]).astimezone(NY).hour*60+parse_dt(x["t"]).astimezone(NY).minute<960]
            if entry<=0 or not future:continue
            mfe=(max(safe(x.get("h")) for x in future)/entry-1)*100;mae=(min(safe(x.get("l")) for x in future)/entry-1)*100
            plan=self.trade_plan(price,atr,vwap,res["resistance"],res["breakout"])
            if not plan:blocks["trade_plan_invalid"]+=1;continue
            trade_status,trade_return=self.simulate_full_t1(entry,future,plan)
            for variant in signals:
                if variant=="technical_lower" and dollar_volume<1000000:blocks["technical_lower_dollar_volume"]+=1;continue
                if variant=="technical_lower" and not observable_hot:blocks["technical_lower_priority_gate"]+=1;continue
                score,core=self.score_variant(None,variant,rvol,accel,price,vwap,obv,trend,res["breakout"],res["distance_pct"],atr_pct)
                payload={"ts":bar["t"],"entry_ts":next_bar["t"],"entry_price":entry,"score":round(score,4),"core_score":round(core,4),"stop":round(plan["stop"],6),"t1":round(plan["t1"],6),"trade_status":trade_status,"trade_return_pct":round(trade_return,4),"mfe_pct":round(mfe,4),"mae_pct":round(mae,4),"rvol":round(rvol,4),"volume_acceleration":round(accel,4),"atr_pct":round(atr_pct,4),"above_vwap":True,"obv_rising":obv,"trend_15m_ok":trend,"breakout":res["breakout"],"resistance_distance_pct":round(res["distance_pct"],4),"entry_quality_signals":quality_signals}
                for level in thresholds:
                    if score>=level and signals[variant][str(level)] is None:signals[variant][str(level)]=payload
                required=93 if minute>=900 else 86
                if score>=required and signals[variant]["live_policy"] is None:signals[variant]["live_policy"]=payload
            if all(signal is not None for by_threshold in signals.values() for signal in by_threshold.values()):break
        return {"schema":1,"session":session,"symbol":symbol,"partition":"holdout" if session in set(self.manifest().get("holdout_sessions",[])) else "development","signals":signals,"block_reasons":dict(blocks),"regular_minutes_evaluated":evaluated,"unavailable_features":["historical_quotes_spread","point_in_time_float","point_in_time_news","exact_live_snapshot"]}
    def step(self):
        manifest=self.manifest();cursor=self.redis.get_json(self.key("cursor"),{"session_index":0,"sscan_cursor":"0","processed":0});si=int(cursor.get("session_index",0))
        if si>=len(manifest["sessions"]):return self.finalize()
        session=manifest["sessions"][si];scan=self.redis.command("SSCAN",self.source_key(f"detail_session:{session}"),str(cursor.get("sscan_cursor","0")),"COUNT",self.batch_size);nxt,symbols=str(scan[0]),sorted(set(scan[1] or []))
        completed=self.key(f"completed:{session}");symbols=[s for s in symbols if not int(self.redis.command("SISMEMBER",completed,s) or 0)]
        start,end=self.session_window(session);sip=self.alpaca.bars(symbols,start,end,"sip") if symbols else {};time.sleep(self.delay);boats=self.alpaca.bars(symbols,start,end,"boats") if symbols else {}
        pairs=[]
        for symbol in symbols:pairs.append((symbol,json.dumps(self.replay(session,symbol,self.merge_bars(sip.get(symbol,[]),boats.get(symbol,[]))),separators=(",",":"))))
        if pairs:self.hset_bounded(self.key(f"results:{session}"),pairs);self.redis.command("SADD",completed,*symbols)
        processed=int(cursor.get("processed",0))+len(symbols)
        if nxt=="0":si+=1
        cursor={"session_index":si,"sscan_cursor":nxt,"processed":processed};self.redis.set_json(self.key("cursor"),cursor)
        total=int(self.redis.command("SCARD",self.source_key("coarse_candidates")) or 139575)
        return self.save_status(status="RUNNING",phase="REPLAYING",message=f"Market Radar causal replay: {session}",cursor=cursor,processed=processed,total=total,progress_pct=round(processed/max(1,total)*100,2))
    def iter_results(self):
        for session in self.manifest().get("sessions",[]):
            cursor="0";key=self.key(f"results:{session}")
            while True:
                scan=self.redis.command("HSCAN",key,cursor,"COUNT",500);cursor=str(scan[0]);rows=scan[1] or []
                for i in range(0,len(rows),2):yield json.loads(rows[i+1])
                if cursor=="0":break
    @staticmethod
    def summarize(signals):
        n=len(signals);mfes=sorted(x["mfe_pct"] for x in signals);maes=sorted(x["mae_pct"] for x in signals);returns=[x["trade_return_pct"] for x in signals];gross_win=sum(max(0,x) for x in returns);gross_loss=abs(sum(min(0,x) for x in returns));wins=sum(x>0 for x in returns)
        return {"signals":n,"avg_mfe_pct":round(sum(mfes)/max(1,n),4),"median_mfe_pct":round(statistics.median(mfes),4) if n else 0,"avg_mae_pct":round(sum(maes)/max(1,n),4),"median_mae_pct":round(statistics.median(maes),4) if n else 0,"avg_trade_return_pct":round(sum(returns)/max(1,n),4),"median_trade_return_pct":round(statistics.median(returns),4) if n else 0,"profit_factor":round(gross_win/gross_loss,4) if gross_loss else (None if not gross_win else "infinite"),"win_rate":round(wins/max(1,n)*100,2),"trade_statuses":dict(Counter(x["trade_status"] for x in signals)),**{f"mfe_ge_{level}_rate":round(sum(x>=level for x in mfes)/max(1,n)*100,2) for level in (2,5,10,20)}}
    def finalize(self):
        threshold_names=("78","82","86","90","93","live_policy");collected={(variant,part,threshold):[] for variant in ("technical_lower","technical_upper") for part in ("development","holdout","all") for threshold in threshold_names};rows=0;blocks=Counter()
        for row in self.iter_results():
            rows+=1;blocks.update(row.get("block_reasons",{}))
            for variant,by_threshold in row.get("signals",{}).items():
                for threshold in threshold_names:
                    signal=by_threshold.get(threshold)
                    if signal:collected[(variant,row["partition"],threshold)].append(signal);collected[(variant,"all",threshold)].append(signal)
        report={"schema":1,"generated_at":now_iso(),"source_prefix":self.source_prefix,"result_prefix":self.prefix,"rows":rows,"methodology":{"causal":True,"entry":"next 1-minute bar open","exit":"Original Market Radar stop/T1; full exit at T1; stop-first when both occur inside one minute.","market_window":"09:30-16:00 America/New_York","live_policy":"Score 86 before 15:00 ET and 93 from 15:00 ET.","technical_lower":"Missing float/news/spread receive zero points; $1m liquidity floor and only observable priority triggers are used.","technical_upper":"Missing float/news/spread receive their maximum possible positive points; $250k liquidity floor and missing priority triggers may pass. This is a ceiling, not a realistic estimate.","not_reproducible":["historical quote spread","point-in-time float","point-in-time news","exact live snapshot and discovery timing"],"selection_warning":"The universe is the stored NDR candidate universe, not an independently reconstructed Market Radar universe.","warning":"This is a technical approximation and must not be described as an exact Market Radar backtest."},"block_reasons":dict(blocks),"variants":{}}
        for variant in ("technical_lower","technical_upper"):
            report["variants"][variant]={part:{threshold:self.summarize(collected[(variant,part,threshold)]) for threshold in threshold_names} for part in ("development","holdout","all")}
        self.redis.set_json(self.key("report"),report);self.save_status(status="COMPLETED",phase="COMPLETED",message="Market Radar technical backtest completed",processed=rows,total=rows,progress_pct=100.0,report_ready=True);return self.status()
    def report(self):return self.redis.get_json(self.key("report"),None)
