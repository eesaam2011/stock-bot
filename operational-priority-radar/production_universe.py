class UniverseUnavailable(RuntimeError):pass
def build_operational_universe(rest):
 rows=rest.active_us_equity_assets()
 if not isinstance(rows,list):raise UniverseUnavailable("ALPACA_ASSET_UNIVERSE_INVALID")
 out=[]
 for a in rows:
  if not isinstance(a,dict):continue
  s=str(a.get("symbol") or "").strip().upper()
  # Transferable operational eligibility only; no research/ranking/tuning filters.
  if (a.get("status")=="active" and a.get("class")=="us_equity"
      and bool(a.get("tradable")) and s):
   out.append(s)
 out=sorted(set(out))
 if not out:raise UniverseUnavailable("ALPACA_ASSET_UNIVERSE_EMPTY")
 return out
