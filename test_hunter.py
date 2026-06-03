from hunter.hunter_bot import run_hunter_scan


if __name__ == "__main__":
    result = run_hunter_scan()

    print(
        f"✅ Hunter finished. Saved: {result}",
        flush=True,
    )
