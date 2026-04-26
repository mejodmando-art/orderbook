# Rate Limit Patterns

## Table of Contents
1. [In-process cache](#in-process-cache)
2. [CoinGecko fetch with retry](#coingecko-fetch-with-retry)
3. [CoinPaprika fallback](#coinpaprika-fallback)
4. [Combined fetch with automatic fallback](#combined-fetch-with-automatic-fallback)
5. [OpenRouter call with retry](#openrouter-call-with-retry)

---

## In-process cache

Keyed by `count` (number of coins requested). Shared across all `/scan` calls within the same process lifetime.

```python
_market_cache: Dict[int, tuple] = {}  # count → (monotonic_ts, List[CoinData])
MARKET_CACHE_TTL = 300                # 5 minutes

def _get_cached(count: int):
    cached = _market_cache.get(count)
    if cached:
        cached_at, coins = cached
        if time.monotonic() - cached_at < MARKET_CACHE_TTL:
            return coins
    return None

def _set_cache(count: int, coins: list):
    _market_cache[count] = (time.monotonic(), coins)
```

**Why monotonic?** `time.time()` can jump backwards on clock adjustments. `time.monotonic()` is safe for duration measurement.

---

## CoinGecko fetch with retry

CoinGecko free tier: ~30 req/min per IP. Returns 429 when exceeded.
Backoff: 10s, 20s, 30s (linear — aggressive enough to clear the window).

```python
def _fetch_coingecko_raw(count: int, retries: int = 3) -> list:
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={count}&page=1"
        "&sparkline=false&price_change_percentage=24h,7d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "MyBot/1.0"})
    last_exc = RuntimeError("No attempts")

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                wait = 10 * attempt
                logger.warning("CoinGecko 429 — sleeping %ds", wait)
                time.sleep(wait)
            else:
                raise   # 4xx other than 429 → don't retry
        except Exception as exc:
            last_exc = exc
            time.sleep(3)

    raise last_exc
```

---

## CoinPaprika fallback

Free, no API key, 25 000 req/month. Returns all tickers sorted by rank in one call.

```python
def _fetch_coinpaprika_fallback(count: int) -> List[CoinData]:
    with urllib.request.urlopen(
        urllib.request.Request(
            "https://api.coinpaprika.com/v1/tickers",
            headers={"User-Agent": "MyBot/1.0"}
        ),
        timeout=30,
    ) as r:
        raw = json.loads(r.read())

    coins, rank = [], 0
    for item in raw:
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        rank += 1
        if rank > count:
            break
        q = item.get("quotes", {}).get("USD", {})
        coins.append(CoinData(
            id=item.get("id", ""),
            symbol=sym,
            name=item.get("name", ""),
            price=float(q.get("price") or 0),
            market_cap=float(q.get("market_cap") or 0),
            volume_24h=float(q.get("volume_24h") or 0),
            change_24h=float(q.get("percent_change_24h") or 0),
            change_7d=float(q.get("percent_change_7d") or 0),
            rank=rank,
        ))
    return coins
```

**Field mapping differences from CoinGecko:**

| CoinGecko field | CoinPaprika field |
|---|---|
| `current_price` | `quotes.USD.price` |
| `total_volume` | `quotes.USD.volume_24h` |
| `price_change_percentage_24h` | `quotes.USD.percent_change_24h` |
| `price_change_percentage_7d_in_currency` | `quotes.USD.percent_change_7d` |

---

## Combined fetch with automatic fallback

```python
def _fetch_top_coins(count: int = 50) -> List[CoinData]:
    # Cache hit
    coins = _get_cached(count)
    if coins:
        return coins

    # Try CoinGecko, fall back to CoinPaprika
    try:
        raw   = _fetch_coingecko_raw(count)
        coins = _parse_coingecko(raw)
        source = "CoinGecko"
    except Exception as cg_exc:
        logger.warning("CoinGecko failed (%s) — trying CoinPaprika", cg_exc)
        try:
            coins  = _fetch_coinpaprika_fallback(count)
            source = "CoinPaprika"
        except Exception as cp_exc:
            raise RuntimeError(
                f"Both sources failed. CoinGecko: {cg_exc} | CoinPaprika: {cp_exc}"
            ) from cp_exc

    _set_cache(count, coins)
    logger.info("%s: %d coins cached for %ds", source, len(coins), MARKET_CACHE_TTL)
    return coins
```

---

## OpenRouter call with retry

OpenRouter free models: rate limits vary per model. 429 is common on popular models (LLaMA-70B).
Also handle 502/503/504 (transient gateway errors).

```python
def _call_openrouter(
    model: str, system: str, user: str,
    max_tokens: int = 500, retries: int = 2
) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    payload = json.dumps({
        "model": model,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }).encode()

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/your/repo",
        "X-Title":       "YourBotName",
    }

    last_exc = RuntimeError("No attempts")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())

            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise ValueError(f"Empty response from {model}")
            return content.strip()

        except urllib.error.HTTPError as exc:
            last_exc = exc
            body = ""
            try: body = exc.read().decode()[:200]
            except Exception: pass

            if exc.code == 429:
                wait = 15 * attempt   # 15s, 30s
                logger.warning("OpenRouter 429 [%s] — waiting %ds", model, wait)
                time.sleep(wait)
            elif exc.code in (502, 503, 504):
                logger.warning("OpenRouter %d [%s] — retrying", exc.code, model)
                time.sleep(5)
            else:
                raise RuntimeError(f"HTTP {exc.code} [{model}]: {body}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(3)

    raise last_exc
```

**Timeout guidance:**
- Market data APIs: 25s
- OpenRouter LLM calls: 90s (free models can be slow under load)
