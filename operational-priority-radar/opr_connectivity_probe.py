import os, json, ssl, urllib.request, urllib.error

def result(name, ok, detail):
    print(json.dumps({"check": name, "ok": bool(ok), "detail": detail}, ensure_ascii=False), flush=True)

key = os.getenv("APCA_API_KEY_ID", "")
secret = os.getenv("APCA_API_SECRET_KEY", "")
redis_url = os.getenv("REDIS_URL", "")

# Never print secrets; only presence/length.
result("environment", bool(key and secret and redis_url),
       {"APCA_API_KEY_ID_loaded": bool(key),
        "APCA_API_KEY_ID_length": len(key),
        "APCA_API_SECRET_KEY_loaded": bool(secret),
        "APCA_API_SECRET_KEY_length": len(secret),
        "REDIS_URL_loaded": bool(redis_url)})

headers = {
    "APCA-API-KEY-ID": key,
    "APCA-API-SECRET-KEY": secret,
    "User-Agent": "opr-shadow-connectivity-probe/1.0",
}

def http_check(name, url):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(256)
            result(name, 200 <= r.status < 300,
                   {"status": r.status, "response_bytes_sampled": len(body)})
    except urllib.error.HTTPError as e:
        body = e.read(256).decode("utf-8", "replace")
        # Alpaca error text is safe; credentials are never included.
        result(name, False, {"status": e.code, "alpaca_error": body[:200]})
    except Exception as e:
        result(name, False, {"error_type": type(e).__name__, "error": str(e)[:200]})

http_check("trading_api_assets_auth",
           "https://api.alpaca.markets/v2/assets?status=active&asset_class=us_equity")
http_check("market_data_api_auth",
           "https://data.alpaca.markets/v2/stocks/AAPL/bars?timeframe=1Min&limit=1&feed=sip")

try:
    import redis
    r = redis.Redis.from_url(redis_url, socket_connect_timeout=10, socket_timeout=10)
    pong = r.ping()
    result("redis_tcp_tls", pong is True, {"ping": bool(pong)})
except Exception as e:
    result("redis_tcp_tls", False, {"error_type": type(e).__name__, "error": str(e)[:200]})
