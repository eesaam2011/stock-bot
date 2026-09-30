from __future__ import annotations

import json, os, urllib.parse, urllib.request
from datetime import datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Sequence
from zoneinfo import ZoneInfo

from mhr_engine_frozen_c import Bar, CorporateAction, ProtocolError, SessionData

NY = ZoneInfo('America/New_York')
DATA_BASE = 'https://data.alpaca.markets'
TRADING_BASE = os.getenv('ALPACA_TRADING_BASE_URL', 'https://paper-api.alpaca.markets')

# Freeze-C: the real 583-case run deliberately uses NO halt evidence.
# Empty expected exchange sessions fail closed as UNRESOLVED_DATA.
REAL_RUN_HALT_EVIDENCE_POLICY = 'NONE_FAIL_CLOSED'

KNOWN_CA_GROUPS = {
    'reverse_splits', 'forward_splits', 'unit_splits',
    'cash_dividends', 'stock_dividends', 'spin_offs',
    'cash_mergers', 'stock_mergers', 'stock_and_cash_mergers',
    'redemptions', 'name_changes', 'worthless_removals',
    'rights_distributions', 'reorganizations', 'partial_calls',
    'capital_gains_distributions',
}


def _headers() -> dict[str, str]:
    key = os.getenv('ALPACA_API_KEY') or os.getenv('APCA_API_KEY_ID')
    secret = os.getenv('ALPACA_SECRET_KEY') or os.getenv('APCA_API_SECRET_KEY')
    if not key or not secret:
        raise ProtocolError('ALPACA_CREDENTIALS_MISSING')
    return {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}


def _json_get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read(), parse_float=str)


def alpaca_calendar(start_date: str, end_date: str, getter=_json_get) -> list[str]:
    q = urllib.parse.urlencode({'start': start_date, 'end': end_date})
    data = getter(f'{TRADING_BASE}/v2/calendar?{q}')
    if not isinstance(data, list):
        raise ProtocolError('CALENDAR_RESPONSE_SHAPE_INVALID')
    dates = [str(x['date']) for x in data]
    if dates != sorted(set(dates)):
        raise ProtocolError('CALENDAR_NOT_STRICTLY_SORTED_UNIQUE')
    return dates


def stock_bars(symbol: str, timeframe: str, start: str, end: str,
               adjustment: str, asof: str, getter=_json_get) -> list[dict]:
    token = None
    out: list[dict] = []
    seen_tokens = set()
    while True:
        params = {
            'timeframe': timeframe, 'start': start, 'end': end, 'limit': 10000,
            'feed': 'sip', 'adjustment': adjustment, 'sort': 'asc', 'asof': asof,
        }
        if token:
            params['page_token'] = token
        url = f"{DATA_BASE}/v2/stocks/{urllib.parse.quote(symbol, safe='')}/bars?{urllib.parse.urlencode(params)}"
        data = getter(url)
        if not isinstance(data, Mapping):
            raise ProtocolError('BAR_RESPONSE_SHAPE_INVALID')
        bars = data.get('bars')
        if not isinstance(bars, list):
            raise ProtocolError('BAR_RESPONSE_SHAPE_INVALID')
        out.extend(bars)
        token = data.get('next_page_token')
        if not token:
            break
        if not isinstance(token, str):
            raise ProtocolError('BAR_PAGE_TOKEN_INVALID')
        if token in seen_tokens:
            raise ProtocolError('BAR_PAGINATION_LOOP')
        seen_tokens.add(token)
    return out


def regular_session_minute_map(bars: Sequence[Mapping[str, Any]]) -> Dict[str, tuple[Bar, ...]]:
    by_date: Dict[str, list[Bar]] = {}
    last_ts = None
    for b in bars:
        ts = datetime.fromisoformat(str(b['t']).replace('Z', '+00:00')).astimezone(NY)
        if last_ts is not None and ts < last_ts:
            raise ProtocolError('NONCHRONOLOGICAL_MINUTE_BARS')
        last_ts = ts
        if time(9, 30) <= ts.time() < time(16, 0):
            by_date.setdefault(ts.date().isoformat(), []).append(
                Bar.from_strings(str(b['o']), str(b['h']), str(b['l']), str(b['c']))
            )
    return {d: tuple(v) for d, v in by_date.items()}


def daily_close_map(bars: Sequence[Mapping[str, Any]]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for b in bars:
        d = str(b['t'])[:10]
        if d in out:
            raise ProtocolError('DUPLICATE_DAILY_BAR')
        out[d] = Decimal(str(b['c']))
    return out


def _extract_ca_groups(payload: Mapping[str, Any]) -> Mapping[str, list]:
    # Raw current endpoint uses corporate_actions. The Alpaca connector used for
    # out-of-population dry-run normalizes the same data as announcements.
    has_current = 'corporate_actions' in payload
    has_legacy = 'announcements' in payload
    if has_current and has_legacy:
        raise ProtocolError('CORPORATE_ACTION_RESPONSE_AMBIGUOUS_ROOT')
    if has_current:
        root = payload['corporate_actions']
    elif has_legacy:
        root = payload['announcements']
    else:
        raise ProtocolError('CORPORATE_ACTION_RESPONSE_ROOT_MISSING')
    if not isinstance(root, Mapping):
        raise ProtocolError('CORPORATE_ACTION_RESPONSE_SHAPE_INVALID')
    for key, rows in root.items():
        if key not in KNOWN_CA_GROUPS:
            raise ProtocolError(f'UNKNOWN_CORPORATE_ACTION_GROUP:{key}')
        if not isinstance(rows, list):
            raise ProtocolError('CORPORATE_ACTION_RESPONSE_SHAPE_INVALID')
    return root


def fetch_corporate_actions(symbols: Sequence[str], start: str, end: str,
                            getter=_json_get) -> dict:
    token = None
    seen_tokens = set()
    merged: dict[str, list] = {}
    while True:
        params = {
            'symbols': ','.join(sorted(set(symbols))), 'start': start, 'end': end,
            'sort': 'asc', 'limit': 1000, 'data_quality': 'complete',
        }
        if token:
            params['page_token'] = token
        url = f"{DATA_BASE}/v1/corporate-actions?{urllib.parse.urlencode(params)}"
        payload = getter(url)
        if not isinstance(payload, Mapping):
            raise ProtocolError('CORPORATE_ACTION_RESPONSE_SHAPE_INVALID')
        root = _extract_ca_groups(payload)
        for key, rows in root.items():
            bucket = merged.setdefault(key, [])
            known = {str(x.get('id', '')) for x in bucket if isinstance(x, Mapping)}
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ProtocolError('CORPORATE_ACTION_ROW_SHAPE_INVALID')
                rid = str(row.get('id', ''))
                if rid and rid in known:
                    continue
                bucket.append(dict(row))
                if rid:
                    known.add(rid)
        token = payload.get('next_page_token')
        if not token:
            break
        if not isinstance(token, str):
            raise ProtocolError('CORPORATE_ACTION_PAGE_TOKEN_INVALID')
        if token in seen_tokens:
            raise ProtocolError('CORPORATE_ACTION_PAGINATION_LOOP')
        seen_tokens.add(token)
    return {'corporate_actions': merged}


def fetch_corporate_actions_chain(symbol: str, start: str, end: str,
                                   max_hops: int = 5, getter=_json_get) -> tuple[dict, list[str]]:
    """Follow same-security name changes; all pages are consumed per hop."""
    pending = [symbol]
    seen = set()
    merged: dict[str, list] = {}
    while pending:
        cur = pending.pop(0)
        if cur in seen:
            continue
        if len(seen) >= max_hops:
            raise ProtocolError('CORPORATE_ACTION_SYMBOL_CHAIN_HOP_LIMIT')
        seen.add(cur)
        payload = fetch_corporate_actions([cur], start, end, getter=getter)
        root = _extract_ca_groups(payload)
        for key, rows in root.items():
            bucket = merged.setdefault(key, [])
            known = {str(x.get('id', '')) for x in bucket if isinstance(x, Mapping)}
            for x in rows:
                xid = str(x.get('id', ''))
                if xid and xid not in known:
                    bucket.append(dict(x)); known.add(xid)
                typ = str(x.get('corporate_action_type') or key)
                if key == 'name_changes' or typ in ('name_changes', 'name_change'):
                    old = str(x.get('old_symbol') or '')
                    new = str(x.get('new_symbol') or '')
                    if old == cur and new and new not in seen and new not in pending:
                        pending.append(new)
    return {'corporate_actions': merged}, sorted(seen)


def normalize_actions(payload: Mapping[str, Any], relevant_symbols: Sequence[str]) -> list[CorporateAction]:
    rel = set(relevant_symbols)
    root = _extract_ca_groups(payload)
    out: list[CorporateAction] = []
    for key, rows in root.items():
        for x in rows:
            typ = str(x.get('corporate_action_type') or key)
            symbol = str(x.get('symbol') or x.get('old_symbol') or x.get('source_symbol') or x.get('acquiree_symbol') or '')
            new_symbol = str(x.get('new_symbol') or '')
            if symbol and symbol not in rel and new_symbol not in rel:
                continue
            eff = str(x.get('ex_date') or x.get('effective_date') or x.get('process_date') or '')
            if not eff:
                raise ProtocolError('CORPORATE_ACTION_EFFECTIVE_DATE_MISSING')

            if typ in ('reverse_splits', 'reverse_split', 'forward_splits', 'forward_split', 'unit_splits', 'unit_split'):
                try:
                    new_rate = Decimal(str(x['new_rate'])); old_rate = Decimal(str(x['old_rate']))
                    factor = new_rate / old_rate
                    if factor <= 0:
                        raise ValueError
                except Exception as exc:
                    raise ProtocolError('CORPORATE_ACTION_SPLIT_FACTOR_INVALID') from exc
                at = 'reverse_split' if factor < 1 else 'split'
                out.append(CorporateAction(eff, at, share_factor=factor))
            elif typ in ('cash_dividends', 'cash_dividend'):
                rate = x.get('rate')
                if rate is None:
                    out.append(CorporateAction(eff, 'cash_dividend', resolvable=False))
                else:
                    out.append(CorporateAction(eff, 'cash_dividend', cash_per_share=Decimal(str(rate))))
            elif typ in ('name_changes', 'name_change'):
                if not new_symbol:
                    out.append(CorporateAction(eff, 'symbol_change', resolvable=False))
                else:
                    out.append(CorporateAction(eff, 'symbol_change', successor_symbol=new_symbol))
            elif typ in ('stock_dividends', 'stock_dividend'):
                if x.get('new_rate') is not None and x.get('old_rate') is not None:
                    factor = Decimal(str(x['new_rate'])) / Decimal(str(x['old_rate']))
                    out.append(CorporateAction(eff, 'stock_dividend', share_factor=factor))
                else:
                    out.append(CorporateAction(eff, 'stock_dividend', resolvable=False))
            elif typ in (
                'spin_offs', 'spin_off', 'cash_mergers', 'cash_merger',
                'stock_mergers', 'stock_merger', 'stock_and_cash_mergers', 'stock_and_cash_merger',
                'redemptions', 'redemption', 'worthless_removals', 'worthless_removal',
                'rights_distributions', 'rights_distribution', 'reorganizations', 'reorganization',
                'partial_calls', 'partial_call', 'capital_gains_distributions', 'capital_gains_distribution',
            ):
                # Known but not safely reducible to a plain stock price threshold here.
                out.append(CorporateAction(eff, 'terminal', resolvable=False))
            else:
                # Never silently ignore a future/unknown event type.
                raise ProtocolError(f'UNKNOWN_CORPORATE_ACTION_TYPE:{typ}')
    return sorted(out, key=lambda x: x.effective_session)


def build_session_map(expected_dates: Sequence[str], minute_by_date: Mapping[str, tuple[Bar, ...]],
                      daily_close_by_date: Mapping[str, Decimal]) -> Dict[str, SessionData]:
    # Freeze-C real-run policy: no discretionary halt evidence.
    # Every expected calendar date is explicit; zero bars => explained_halt=False.
    out = {}
    for d in expected_dates:
        bars = minute_by_date.get(d, ())
        out[d] = SessionData(
            d, bars, daily_close_by_date.get(d),
            explained_halt=False, retrieval_complete=True,
        )
    return out