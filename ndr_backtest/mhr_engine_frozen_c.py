from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Any
import hashlib
import json

getcontext().prec = 34

D = Decimal
ZERO = D("0")
ONE = D("1")
ENTRY_FRICTION = D("0.00125")
EXIT_FRICTION = D("0.00125")
EPS = D("0.000000000001")
UNRESOLVED_CAP = D("0.10")


class ProtocolError(ValueError):
    pass


def dec(value: str | Decimal) -> Decimal:
    """Prices/fractions must arrive as strings or Decimal; floats are rejected."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    raise ProtocolError(f"decimal input must be str/Decimal, got {type(value).__name__}")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    role: str
    stop_fraction: Decimal
    target_fraction: Decimal
    max_sessions: int


M1 = Rule("M1", "PRIMARY", D("0.08"), D("0.15"), 10)
M2 = Rule("M2", "SECONDARY", D("0.05"), D("0.10"), 5)
M3 = Rule("M3", "SECONDARY", D("0.10"), D("0.20"), 20)
RULES = (M1, M2, M3)


@dataclass(frozen=True)
class Bar:
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal

    @classmethod
    def from_strings(cls, o: str, h: str, l: str, c: str) -> "Bar":
        return cls(dec(o), dec(h), dec(l), dec(c))


@dataclass(frozen=True)
class SessionData:
    date: str
    bars: Tuple[Bar, ...] = ()
    daily_close: Optional[Decimal] = None
    explained_halt: bool = False
    retrieval_complete: bool = True


@dataclass(frozen=True)
class CorporateAction:
    effective_session: str
    action_type: str
    share_factor: Optional[Decimal] = None
    cash_per_share: Optional[Decimal] = None
    successor_symbol: Optional[str] = None
    resolvable: bool = True


@dataclass(frozen=True)
class Case:
    case_id: str
    partition: str
    entry_session: str
    entry_price: Decimal


@dataclass
class Outcome:
    case_id: str
    partition: str
    rule_id: str
    status: str
    exit_type: Optional[str] = None
    exit_session: Optional[str] = None
    exit_price: Optional[Decimal] = None
    gross_return: Optional[Decimal] = None
    net_return: Optional[Decimal] = None
    unresolved_type: Optional[str] = None
    reason: Optional[str] = None
    overrun_sessions: int = 0
    corporate_action_normalized: bool = False
    same_minute_stop_first: bool = False


def validate_manifest(canonical_txt_bytes: bytes, manifest: Mapping[str, Any]) -> None:
    actual = hashlib.sha256(canonical_txt_bytes).hexdigest()
    expected = manifest.get("canonical_contract_sha256")
    if expected != actual:
        raise ProtocolError("PROTOCOL_MANIFEST_MISMATCH")
    for rid, r in manifest["rules"].items():
        sf = dec(str(r["stop_fraction"]))
        tf = dec(str(r["target_fraction"]))
        if sf <= 0 or tf <= 0:
            raise ProtocolError("PROTOCOL_MANIFEST_MISMATCH")
        frozen = {x.rule_id: x for x in RULES}[rid]
        if sf != frozen.stop_fraction or tf != frozen.target_fraction or int(r["max_sessions"]) != frozen.max_sessions:
            raise ProtocolError("PROTOCOL_MANIFEST_MISMATCH")


def map_partition(legacy_partition: str, fold: Optional[int]) -> str:
    if legacy_partition == "development" and fold in (1, 2, 3):
        return f"P{fold}"
    if legacy_partition == "holdout" and fold is None:
        return "P4"
    raise ProtocolError("UNMAPPED_PARTITION")


def derive_calendar_sessions(
    full_calendar: Sequence[str], entry_session: str, max_sessions: int
) -> Tuple[str, ...]:
    if len(set(full_calendar)) != len(full_calendar):
        raise ProtocolError("DUPLICATE_CALENDAR_SESSION")
    if tuple(full_calendar) != tuple(sorted(full_calendar)):
        raise ProtocolError("UNSORTED_CALENDAR")
    try:
        start = full_calendar.index(entry_session)
    except ValueError as exc:
        raise ProtocolError("ENTRY_SESSION_NOT_IN_CALENDAR") from exc
    needed = tuple(full_calendar[start : start + max_sessions])
    if len(needed) != max_sessions:
        raise ProtocolError("INSUFFICIENT_CALENDAR_COVERAGE")
    return needed


def _actions_by_session(actions: Sequence[CorporateAction]) -> Dict[str, List[CorporateAction]]:
    out: Dict[str, List[CorporateAction]] = {}
    for a in actions:
        out.setdefault(a.effective_session, []).append(a)
    return out


def _apply_actions(
    actions: Sequence[CorporateAction],
    shares: Decimal,
    stop: Decimal,
    target: Decimal,
    cash: Decimal,
) -> Tuple[Decimal, Decimal, Decimal, Decimal, bool, Optional[str]]:
    changed = False
    for a in actions:
        if not a.resolvable:
            return shares, stop, target, cash, changed, "UNRESOLVED_CORPORATE_ACTION"
        t = a.action_type
        if t in ("split", "reverse_split", "stock_dividend"):
            if a.share_factor is None or a.share_factor <= 0:
                return shares, stop, target, cash, changed, "UNRESOLVED_CORPORATE_ACTION"
            f = a.share_factor
            shares *= f
            stop /= f
            target /= f
            changed = True
        elif t == "cash_dividend":
            if a.cash_per_share is None:
                return shares, stop, target, cash, changed, "UNRESOLVED_CORPORATE_ACTION"
            cash += shares * a.cash_per_share
            changed = True
        elif t == "symbol_change":
            changed = True
        elif t in ("merger", "spin_off", "cvr", "redemption", "delisting", "terminal"):
            # These require externally normalized proceeds/continuity before path replay.
            return shares, stop, target, cash, changed, "UNRESOLVED_CORPORATE_ACTION"
        else:
            return shares, stop, target, cash, changed, "UNRESOLVED_CORPORATE_ACTION"
    return shares, stop, target, cash, changed, None


def _returns(
    entry_price: Decimal,
    shares: Decimal,
    exit_price: Decimal,
    cash: Decimal,
) -> Tuple[Decimal, Decimal]:
    gross_proceeds = shares * exit_price + cash
    gross = gross_proceeds / entry_price - ONE
    net_proceeds = shares * exit_price * (ONE - EXIT_FRICTION) + cash
    net_entry = entry_price * (ONE + ENTRY_FRICTION)
    net = net_proceeds / net_entry - ONE
    return gross, net


def evaluate_case(
    case: Case,
    rule: Rule,
    full_calendar: Sequence[str],
    session_map: Mapping[str, SessionData],
    actions: Sequence[CorporateAction] = (),
) -> Outcome:
    scheduled = derive_calendar_sessions(full_calendar, case.entry_session, rule.max_sessions)
    action_map = _actions_by_session(actions)

    shares = ONE
    cash = ZERO
    stop = case.entry_price * (ONE - rule.stop_fraction)
    target = case.entry_price * (ONE + rule.target_fraction)
    ca_changed = False
    halted_at_max = False
    max_index = full_calendar.index(scheduled[-1])

    def resolved(exit_type: str, date: str, px: Decimal, *, ambiguous: bool=False, overrun: int=0) -> Outcome:
        gross, net = _returns(case.entry_price, shares, px, cash)
        return Outcome(
            case.case_id, case.partition, rule.rule_id, "RESOLVED",
            exit_type=exit_type, exit_session=date, exit_price=px,
            gross_return=gross, net_return=net, overrun_sessions=overrun,
            corporate_action_normalized=ca_changed,
            same_minute_stop_first=ambiguous,
        )

    for idx, date in enumerate(scheduled, start=1):
        # Entry-date actions are already reflected in EHR entry price.
        if idx > 1:
            shares, stop, target, cash, changed, unresolved = _apply_actions(
                action_map.get(date, ()), shares, stop, target, cash
            )
            ca_changed = ca_changed or changed
            if unresolved:
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type=unresolved, reason="corporate_action")

        sess = session_map.get(date)
        if sess is None:
            # Calendar exists but data object was omitted: fail closed.
            return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                           unresolved_type="UNRESOLVED_DATA", reason="CALENDAR_SESSION_OMITTED")
        if not sess.retrieval_complete:
            return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                           unresolved_type="UNRESOLVED_DATA", reason="RETRIEVAL_INCOMPLETE")

        if not sess.bars:
            if sess.explained_halt:
                if idx == rule.max_sessions:
                    halted_at_max = True
                continue
            return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                           unresolved_type="UNRESOLVED_DATA", reason="EMPTY_REQUIRED_SESSION")

        for j, bar in enumerate(sess.bars):
            first = j == 0

            if idx == 1 and first:
                # Entry-session first bar: high/low only, no open-gap execution.
                if bar.l <= stop and bar.h >= target:
                    return resolved("STOP_FIRST_AMBIGUOUS", date, stop, ambiguous=True)
                if bar.l <= stop:
                    return resolved("STOP", date, stop)
                if bar.h >= target:
                    return resolved("TARGET", date, target)
                continue

            # Bar-open gap check first.
            if bar.o <= stop:
                et = "GAP_STOP" if (idx > 1 and first) else "INTRASESSION_GAP_STOP"
                return resolved(et, date, bar.o)
            if bar.o >= target:
                et = "TARGET_ON_OPEN" if (idx > 1 and first) else "TARGET"
                return resolved(et, date, target)

            # Same bar high/low after the open test.
            if bar.l <= stop and bar.h >= target:
                return resolved("STOP_FIRST_AMBIGUOUS", date, stop, ambiguous=True)
            if bar.l <= stop:
                return resolved("STOP", date, stop)
            if bar.h >= target:
                return resolved("TARGET", date, target)

        if idx == rule.max_sessions:
            if sess.daily_close is None:
                if sess.explained_halt:
                    halted_at_max = True
                    break
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type="UNRESOLVED_DATA", reason="MISSING_MAX_HOLD_CLOSE")
            return resolved("MAX_HOLD", date, sess.daily_close)

    if halted_at_max:
        # Search subsequent calendar sessions only for first executable regular-session open.
        # Corporate actions are applied on EVERY intervening calendar session, including
        # sessions with no prints while the security remains halted.
        for overrun_idx, date in enumerate(full_calendar[max_index + 1 :], start=1):
            shares, stop, target, cash, changed, unresolved = _apply_actions(
                action_map.get(date, ()), shares, stop, target, cash
            )
            ca_changed = ca_changed or changed
            if unresolved:
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type=unresolved, reason="corporate_action")
            sess = session_map.get(date)
            if sess is None:
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type="UNRESOLVED_DATA", reason="OVERRUN_SESSION_OMITTED")
            if not sess.retrieval_complete:
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type="UNRESOLVED_DATA", reason="OVERRUN_RETRIEVAL_INCOMPLETE")
            if sess.bars:
                return resolved("HALT_OVERRUN_EXIT", date, sess.bars[0].o, overrun=overrun_idx)
            if not sess.explained_halt:
                return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                               unresolved_type="UNRESOLVED_DATA", reason="UNEXPLAINED_OVERRUN_EMPTY_SESSION")
        return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                       unresolved_type="UNRESOLVED_DATA", reason="NO_VERIFIED_RESUMPTION")

    return Outcome(case.case_id, case.partition, rule.rule_id, "UNRESOLVED",
                   unresolved_type="UNRESOLVED_DATA", reason="NO_EXIT")


def _pf(values: Sequence[Decimal]) -> Optional[Decimal]:
    pos = sum((x for x in values if x > ZERO), ZERO)
    neg = sum((x for x in values if x < ZERO), ZERO)
    if neg == ZERO:
        return None
    return pos / abs(neg)


def classify_return(x: Decimal) -> str:
    if x > EPS:
        return "WIN"
    if x < -EPS:
        return "LOSS"
    return "FLAT"


def partition_metrics(
    outcomes: Sequence[Outcome],
    selected_count: int,
    rule: Rule,
) -> Dict[str, Any]:
    resolved = [x for x in outcomes if x.status == "RESOLVED"]
    unresolved = [x for x in outcomes if x.status != "RESOLVED"]
    net = [x.net_return for x in resolved if x.net_return is not None]
    gross = [x.gross_return for x in resolved if x.gross_return is not None]
    labels = [classify_return(x) for x in net]
    wins = labels.count("WIN")
    losses = labels.count("LOSS")
    flats = labels.count("FLAT")
    unresolved_rate = D(len(unresolved)) / D(selected_count)

    # Mandatory worst-case: assign every unresolved the ordinary full-static-stop net return.
    stop_net = ((ONE - rule.stop_fraction) * (ONE - EXIT_FRICTION) / (ONE + ENTRY_FRICTION)) - ONE
    worst_net = list(net) + [stop_net] * len(unresolved)

    def mean(xs):
        return sum(xs, ZERO) / D(len(xs)) if xs else None

    def median(xs):
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / D(2)

    counters: Dict[str, int] = {}
    for x in resolved:
        counters[x.exit_type or "UNKNOWN"] = counters.get(x.exit_type or "UNKNOWN", 0) + 1

    unresolved_data = sum(1 for x in unresolved if x.unresolved_type == "UNRESOLVED_DATA")
    unresolved_ca = sum(1 for x in unresolved if x.unresolved_type == "UNRESOLVED_CORPORATE_ACTION")
    ca_count = sum(1 for x in resolved if x.corporate_action_normalized)

    return {
        "selected": selected_count,
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "unresolved_rate": str(unresolved_rate),
        "unresolved_data": unresolved_data,
        "unresolved_corporate_action": unresolved_ca,
        "wins": wins, "losses": losses, "flat": flats,
        "gross_pf": None if _pf(gross) is None else str(_pf(gross)),
        "net_pf": None if _pf(net) is None else str(_pf(net)),
        "worst_case_unresolved_stop_pf": None if _pf(worst_net) is None else str(_pf(worst_net)),
        "mean_net": None if mean(net) is None else str(mean(net)),
        "median_net": None if median(net) is None else str(median(net)),
        "win_rate": None if not resolved else str(D(wins) / D(len(resolved))),
        "exit_counts": counters,
        "corporate_action_normalized": ca_count,
        "coverage_invalid": unresolved_rate > UNRESOLVED_CAP,
        "sample_insufficient": len(resolved) < 30 or losses < 10,
    }


def classify_rule(partitions: Mapping[str, Dict[str, Any]], role: str) -> str:
    # FAIL precedence: among individually decision-eligible partitions.
    for p in ("P1", "P2", "P3", "P4"):
        m = partitions[p]
        if not m["coverage_invalid"] and not m["sample_insufficient"]:
            pf = m["net_pf"]
            if pf is not None and dec(pf) <= ONE:
                return "FAIL"
    if any(partitions[p]["coverage_invalid"] for p in ("P1","P2","P3","P4")):
        return "INVALID_DATA_COVERAGE"
    if any(partitions[p]["sample_insufficient"] for p in ("P1","P2","P3","P4")):
        return "INSUFFICIENT_SAMPLE"

    if all(partitions[p]["net_pf"] is not None and dec(partitions[p]["net_pf"]) > ONE
           for p in ("P1","P2","P3","P4")):
        return "PASS" if role == "PRIMARY" else "EXPLORATORY_SIGNAL"
    return "FAIL"


def build_report(
    outcomes_by_rule: Mapping[str, Sequence[Outcome]],
    selected_counts: Mapping[str, int],
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"rules": {}}
    by_id = {r.rule_id: r for r in RULES}
    for rid, outcomes in outcomes_by_rule.items():
        rule = by_id[rid]
        pm = {}
        for p in ("P1","P2","P3","P4"):
            subset = [x for x in outcomes if x.partition == p]
            pm[p] = partition_metrics(subset, selected_counts[p], rule)
        status = classify_rule(pm, rule.role)
        report["rules"][rid] = {
            "role": rule.role,
            "partitions": pm,
            "classification": status,
        }
        if rule.role == "PRIMARY" and status == "PASS":
            fragile = any(
                pm[p]["worst_case_unresolved_stop_pf"] is not None
                and dec(pm[p]["worst_case_unresolved_stop_pf"]) <= ONE
                for p in ("P1","P2","P3","P4")
            )
            report["rules"][rid]["fragility_label"] = (
                "FRAGILE_TO_UNRESOLVED_CASES" if fragile else "NOT_FRAGILE_BY_FROZEN_STRESS"
            )
    return report


# ENGINE-FREEZE-B strict input/population layer
from dataclasses import dataclass as _b_dataclass
from typing import Mapping as _BMapping, Sequence as _BSequence, Any as _BAny, Dict as _BDict

def expected_case_partition_map(entry_records: _BSequence[_BMapping[str, _BAny]]) -> _BDict[str, str]:
    out = {}
    for rec in entry_records:
        cid = str(rec["case_id"]); part = str(rec["partition"])
        if cid in out: raise ProtocolError("DUPLICATE_EXPECTED_CASE_ID")
        if part not in {"P1","P2","P3","P4"}: raise ProtocolError("UNMAPPED_EXPECTED_PARTITION")
        out[cid] = part
    if len(out) != 583: raise ProtocolError("EXPECTED_POPULATION_NOT_583")
    return out

def validate_exact_outcome_population(outcomes, expected_map, rule_id):
    seen = {}
    for o in outcomes:
        if o.rule_id != rule_id: raise ProtocolError("WRONG_RULE_ID_IN_OUTCOMES")
        if o.case_id in seen: raise ProtocolError("DUPLICATE_OUTCOME_CASE_ID")
        if o.case_id not in expected_map: raise ProtocolError("UNEXPECTED_OUTCOME_CASE_ID")
        if o.partition != expected_map[o.case_id]: raise ProtocolError("OUTCOME_PARTITION_MISMATCH")
        seen[o.case_id] = o
    if set(expected_map) - set(seen): raise ProtocolError("MISSING_OUTCOME_CASE_ID")
    if set(seen) - set(expected_map): raise ProtocolError("UNEXPECTED_OUTCOME_CASE_ID")
    if len(seen) != len(expected_map): raise ProtocolError("OUTCOME_POPULATION_SIZE_MISMATCH")

def build_report_strict(outcomes_by_rule, entry_records):
    expected_map = expected_case_partition_map(entry_records)
    if set(outcomes_by_rule) != {"M1","M2","M3"}: raise ProtocolError("MISSING_OR_EXTRA_RULE_RESULTS")
    for rid in ("M1","M2","M3"):
        validate_exact_outcome_population(outcomes_by_rule[rid], expected_map, rid)
    selected_counts = {p:sum(1 for x in expected_map.values() if x==p) for p in ("P1","P2","P3","P4")}
    if selected_counts != {"P1":98,"P2":109,"P3":145,"P4":231}: raise ProtocolError("PARTITION_COUNTS_MISMATCH")
    return build_report(outcomes_by_rule, selected_counts)

@_b_dataclass(frozen=True)
class SplitAudit:
    status: str
    suspicious_dates: tuple[str, ...]
    details: tuple[dict, ...]

def audit_split_adjustment_consistency(raw_close_by_date, split_close_by_date, split_actions, end_session, relative_tolerance=D("0.005")):
    raw_dates=set(raw_close_by_date); split_dates=set(split_close_by_date)
    if raw_dates != split_dates:
        missing=sorted(raw_dates ^ split_dates)
        return SplitAudit("UNRESOLVED_CORPORATE_ACTION",tuple(missing),({"reason":"RAW_SPLIT_DATESET_MISMATCH","dates":missing},))
    actions=[a for a in split_actions if a.action_type in ("split","reverse_split")]
    details=[]; suspicious=[]
    for date in sorted(raw_dates):
        raw=raw_close_by_date[date]; adj=split_close_by_date[date]
        if raw<=0 or adj<=0:
            suspicious.append(date); details.append({"date":date,"reason":"NONPOSITIVE_DAILY_CLOSE"}); continue
        expected=D("1")
        for a in actions:
            if date < a.effective_session <= end_session:
                if a.share_factor is None or a.share_factor<=0:
                    suspicious.append(date); details.append({"date":date,"reason":"INVALID_SPLIT_FACTOR"}); expected=None; break
                expected *= D("1")/a.share_factor
        if expected is None: continue
        observed=adj/raw; rel=abs(observed-expected)/abs(expected)
        details.append({"date":date,"observed_adjusted_to_raw_ratio":str(observed),"expected_ratio_from_recorded_splits":str(expected),"relative_error":str(rel)})
        if rel>relative_tolerance: suspicious.append(date)
    return SplitAudit("PASS" if not suspicious else "UNRESOLVED_CORPORATE_ACTION",tuple(sorted(set(suspicious))),tuple(details))

HALT_SOURCE_ID = "NASDAQ_TRADER_TRADING_HALT_HISTORY_RSS"

@_b_dataclass(frozen=True)
class HaltEvidence:
    symbol: str
    halt_date: str
    resume_date: str | None
    source_id: str
    source_url: str

def explained_halt_for_session(symbol, session, evidence):
    for e in evidence:
        if e.source_id != HALT_SOURCE_ID or e.symbol != symbol: continue
        if e.halt_date <= session and (e.resume_date is None or session < e.resume_date): return True
    return False