import re,json
from pathlib import Path
from types import SimpleNamespace
def classify_name(name,patterns):
    if not isinstance(name,str) or not name.strip():return 'TYPE_UNKNOWN',(),()
    rx=lambda p:bool(re.search(p,name,re.I))
    hits=[(i,t) for i,t,p in patterns if p.search(name)];suppressed=[]
    def remove(i,reason):
        nonlocal hits
        if any(x[0]==i for x in hits):suppressed.append((i,reason));hits=[h for h in hits if h[0]!=i]
    if rx(r'\b(?:common|corporate)\s+units?\b') and not rx(r'\b(?:acquisition|SPAC)\b'):remove('N03','OPERATING_UNITS')
    if rx(r'\bcommon\s+(?:stock|shares)\b'):
        for i in ('N04','N05','N08','N09','N12','N13'):remove(i,'COMMON_STOCK_OR_SHARES')
    if rx(r'^\s*Invesco\s+(?:LTD\.?|Limited|Inc\.?)\s*$'):remove('N08','ISSUER_COMPANY')
    if rx(r'\b(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited)\b') and not rx(r'\b(?:Funds?|ETFs?|ETNs?)\b') and not (rx(r'\bTrust\b') and rx(r'\b(?:Shares|Series|Portfolio)\b')):remove('N08','OPERATING_ISSUER_CONTEXT')
    if rx(r'\b(?:Mortgage|Realty|REIT|Bank|Bancorp|Properties|Property)\b|\breal\s+estate\b'):remove('N11','PROPERTY_OR_BANK')
    if rx(r'\bpreferred\b|\bpreference\s+shares?\b'):remove('N11','PREFERRED_UNDERLYING_TRUST')
    if any(i=='N10' for i,t in hits):remove('N03','CRYPTO_PRODUCT_UNIT')
    types={t for i,t in hits}
    if 'UNIT' in types:
        for i in ('N01','N02','N04'):remove(i,'UNIT_COMPONENT')
    if 'ADR' in types:remove('N02','ADR_ENTITLEMENT')
    if 'ETF_ETN_FUND' in types:remove('N05','FUND_HOLDING')
    if types & {'ETF_ETN_FUND','WARRANT','UNIT'}:remove('N07','ADR_UNDERLYING')
    if any(t=='ADR' for i,t in hits):remove('N04','ADR_PREFERRED_UNDERLYING')
    return (hits[0][1] if hits else 'TYPE_UNKNOWN'),tuple(hits),tuple(suppressed)

class Classifier:
 def __init__(self,assets):
  root=Path(__file__).resolve().parents[1]/"classifications"
  rules=json.loads((root/"NAME_RULES.json").read_bytes());manual=json.loads((root/"TYPE_MANUAL.json").read_bytes())
  activity=frozenset((root/"ACTIVITY.txt").read_text().splitlines());assert len(activity)==656
  self.frozen=SimpleNamespace(activity=activity,manual=frozenset((root/"ADR_MANUAL.txt").read_text().splitlines()),type_manual={r["symbol"]:r for r in manual["records"]},day={"assets":assets},patterns=[(r["rule_id"],r["type"],re.compile(r["regex"],re.I)) for r in rules["rules"]])
  self._static={s:self._classify_asset(a) for s,a in assets.items()}
 def _classify_asset(self,a):return classify_name(a.get("name"),self.frozen.patterns)
 def static_reasons(self,symbol,asset=None):
         f=self.frozen;asset=f.day['assets'].get(symbol) if asset is None else asset;reasons=set()
         if asset is None:return {'ASSET_STATUS_UNKNOWN'},'TYPE_UNKNOWN',(),()
         if symbol in f.activity:reasons.add('ACTIVITY_DENYLIST_EXACT_MATCH')
         if symbol in f.manual:reasons.add('TYPE_ADR_MANUAL')
         exception=f.type_manual.get(symbol)
         if exception:
             identity_matches=asset.get('id')==exception['asset_id'] and asset.get('name')==exception['name']
             reasons.add('TYPE_MANUAL_REVIEWED_EXCEPTION' if identity_matches else 'TYPE_MANUAL_IDENTITY_MISMATCH')
         for field,value,reason in [('exchange','OTC','MARKET_OTC'),('status','active','ASSET_INACTIVE'),('tradable',True,'ASSET_NOT_TRADABLE')]:
             v=asset.get(field)
             if v is None or (field=='exchange' and v not in {'NYSE','NASDAQ','ARCA','BATS','AMEX','OTC'}):reasons.add('ASSET_STATUS_UNKNOWN')
             elif field=='exchange' and v==value:reasons.add(reason)
             elif field=='status' and v!=value:reasons.add(reason)
             elif field=='tradable' and v is not True:reasons.add(reason)
         typ,hits,suppressed=self._classify_asset(asset) if asset is not f.day['assets'].get(symbol) else self._static[symbol]
         reasons.update('TYPE_'+t for i,t in hits)
         return reasons,typ,hits,suppressed
 def eligible(self,s):return not self.static_reasons(s)[0]
