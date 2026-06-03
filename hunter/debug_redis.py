from hunter.redis_store import get_all_candidates


def main():
    candidates = get_all_candidates()

    print("\n🔍 REDIS CANDIDATES DEBUG")
    print(f"Total candidates in Redis: {len(candidates)}")

    for symbol, candidate in candidates.items():
        print("\n-------------------------")
        print(f"Symbol: {symbol}")
        print(f"Score: {candidate.get('candidate_score')}")
        print(f"Price: {candidate.get('price')}")
        print(f"RVOL: {candidate.get('rvol')}")
        print(f"Gap %: {candidate.get('gap_pct')}")
        print(f"Gain %: {candidate.get('gain_pct')}")
        print(f"Day Volume: {candidate.get('day_volume')}")
        print(f"Dollar Volume: {candidate.get('dollar_volume')}")
        print(f"Near High: {candidate.get('near_high')}")
        print(f"Above VWAP: {candidate.get('above_vwap')}")
        print(f"Sources: {candidate.get('sources')}")
        print(f"Created At: {candidate.get('created_at')}")
        print(f"Updated At: {candidate.get('updated_at')}")
        print(f"Last Seen At: {candidate.get('last_seen_at')}")
        print(f"Expires At: {candidate.get('expires_at')}")


if __name__ == "__main__":
    main()
