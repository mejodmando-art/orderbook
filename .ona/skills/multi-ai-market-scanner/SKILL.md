---
name: multi-ai-market-scanner
description: Build a multi-AI market scanner for a Telegram trading bot. Fetches top coins by market cap from CoinGecko (with CoinPaprika fallback), runs multiple free OpenRouter LLM models as independent analysts, merges consensus picks, and formats a ranked report for Telegram. Includes patterns for rate-limit handling: retry/backoff, in-process cache, sequential analyst calls, and model diversity across providers. Use when asked to add a market scanner, /scan command, AI-powered coin analysis, multi-model consensus trading signal, or OpenRouter free-tier integration to a Telegram bot.
---

# Multi-AI Market Scanner

Adds a `/scan` command to a Telegram bot that autonomously scans the top N coins by market cap, runs them through three independent AI analysts, and produces a ranked shortlist of the best opportunities.

## Architecture

```
Market Data (CoinGecko → CoinPaprika fallback)
        │
        ▼
Pre-filter: top 20 coins by momentum score
        │
        ├──► Analyst 1 (LLM A) ──┐
        ├──► Analyst 2 (LLM B) ──┼─ sequential, 3s gap between each
        └──► Analyst 3 (LLM C) ──┘
                                  │
                          Consensus merge
                    (coins in 2+ lists = strong signal)
                                  │
                                  ▼
                          Judge LLM → final top 3
                                  │
                                  ▼
                        Telegram Markdown report
```

## Implementation Steps

### 1. Market data fetch

Use CoinGecko `/coins/markets` — free, no API key:

```python
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc&per_page={count}&page=1"
    "&sparkline=false&price_change_percentage=24h,7d"
)
```

Always wrap in retry with backoff and an in-process cache. See `references/rate-limit-patterns.md` for the full pattern.

**CoinPaprika fallback** when CoinGecko returns 429:

```python
def _fetch_coinpaprika_fallback(count: int) -> List[CoinData]:
    raw = _http_get_json("https://api.coinpaprika.com/v1/tickers")
    # raw is sorted by rank; slice after filtering stablecoins
```

CoinPaprika returns all tickers in one call, sorted by rank. No API key needed.

### 2. Pre-filter coins before sending to AI

Never send all 50 coins to the LLM — prompts get too long (400 errors). Score and keep top 20:

```python
def _prefilter_coins(coins, limit=20):
    def score(c):
        vol_ratio = (c.volume_24h / c.market_cap) if c.market_cap > 0 else 0
        momentum  = c.change_24h + (c.change_7d * 0.3)
        return vol_ratio * 100 + momentum
    return sorted(coins, key=score, reverse=True)[:limit]
```

### 3. Stablecoin filter

Exclude before scoring. Maintain an explicit set — new stablecoins appear regularly:

```python
STABLECOINS = {
    "usdt", "usdc", "busd", "dai", "tusd", "usdp", "usdd",
    "frax", "lusd", "susd", "gusd", "fdusd", "pyusd",
    "usd1", "usde", "usds", "crvusd",
    "xaut", "paxg",           # gold-backed
    "wbtc", "steth", "weth", "cbbtc",  # wrapped/staked
}
```

### 4. Analyst prompt — keep it short

```python
_ANALYST_SYSTEM = (
    "You are a crypto trading analyst. "
    "Respond with ONLY a valid JSON array. No markdown, no text outside the JSON."
)

def _build_analyst_prompt(coins, top_n):
    coin_lines = "\n".join(c.summary_line() for c in coins)
    return (
        f"Pick the TOP {top_n} best BUY opportunities from this list.\n\n"
        f"{coin_lines}\n\n"
        f"Reply with ONLY this JSON (no other text):\n"
        f'[{{"symbol":"BTC","name":"Bitcoin","signal":"BUY","confidence":75,'
        f'"reason":"one sentence"}}, ...]\n'
        f"Exactly {top_n} items. signal=BUY/SELL/HOLD, confidence=0-100."
    )
```

Target prompt size: under 2000 characters. Above that, models start returning 400 errors.

### 5. Run analysts sequentially — not in parallel

Parallel calls to the same or similar models trigger simultaneous 429s:

```python
ANALYST_DELAY_SECONDS = 3

reports = []
for i, (name, model) in enumerate(ANALYST_MODELS):
    if i > 0:
        await asyncio.sleep(ANALYST_DELAY_SECONDS)
    report = await _run_analyst(name, model, coins)
    reports.append(report)
```

### 6. Model selection — spread across providers

Pick one model per provider to distribute rate-limit load:

```python
ANALYST_MODELS = [
    ("LLaMA-70B",  "meta-llama/llama-3.3-70b-instruct:free"),   # Meta
    ("Gemma-12B",  "google/gemma-3-12b-it:free"),                # Google
    ("GPT-OSS-20B","openai/gpt-oss-20b:free"),                   # OpenAI OSS
]
JUDGE_MODEL = "openai/gpt-oss-120b:free"
```

Check live availability before choosing: `GET https://openrouter.ai/api/v1/models` — filter by `":free"` in id.

### 7. Robust JSON extraction

Models return markdown fences, leading text, or partial JSON. Handle all cases:

```python
def _extract_json_array(text: str) -> list:
    if not text or not text.strip():
        raise ValueError("Empty response from model")
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    # Walk character by character to find outermost [...]
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0: start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start is not None:
                try: return json.loads(text[start:i+1])
                except json.JSONDecodeError: pass
    raise ValueError(f"No JSON array found: {text[:200]}")
```

### 8. Consensus merge

```python
def _merge_analyst_reports(reports):
    merged = {}
    for report in reports:
        if report.error or not report.picks:
            continue
        for pick in report.picks:
            sym = pick.symbol
            if sym not in merged:
                merged[sym] = {"name": pick.name, "signal": pick.signal,
                               "votes": 0, "confidence": 0,
                               "analysts": [], "reasons": []}
            merged[sym]["votes"]      += 1
            merged[sym]["confidence"] += pick.confidence
            merged[sym]["analysts"].append(report.analyst_name)
    for data in merged.values():
        data["confidence"] //= data["votes"]
    return merged
```

Coins with `votes >= 2` are consensus picks — prioritise these in the judge prompt.

### 9. Judge fallback

If the judge LLM fails, return top consensus picks directly — never fail silently:

```python
except Exception as exc:
    fallback = []
    for i, (sym, data) in enumerate(
        sorted(merged.items(), key=lambda x: (-x[1]["votes"], -x[1]["confidence"])),
        start=1,
    ):
        if i > SCAN_FINAL_TOP: break
        fallback.append(FinalPick(rank=i, symbol=sym, ...
            reason=f"Consensus ({data['votes']} analysts). Judge unavailable: {exc}"))
    return fallback
```

### 10. Telegram command handler

```python
async def cmd_scan(update, ctx):
    count = 50  # default
    if ctx.args:
        try: count = max(10, min(100, int(ctx.args[0])))
        except ValueError: pass

    wait_msg = await update.message.reply_text("🔍 جاري المسح...")
    try:
        result = await SMART_SCANNER.scan(coin_count=count)
        await wait_msg.delete()
        await update.message.reply_text(_build_scan_report(result), parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        await wait_msg.delete()
        await update.message.reply_text(f"❌ خطأ: {exc}")
```

Register with: `app.add_handler(CommandHandler("scan", cmd_scan))`

## Reference Files

- **`references/rate-limit-patterns.md`** — Full retry/backoff/cache implementations for CoinGecko and OpenRouter. Read when implementing the data fetch layer.
- **`references/openrouter-free-models.md`** — Verified free models list with notes on reliability. Read when choosing analyst/judge models or when a model returns 404/400.
- **`references/telegram-report-format.md`** — Telegram Markdown formatter for scan results with confidence bar and medal ranking. Read when building the report display.

## Key Constants

| Constant | Default | Purpose |
|---|---|---|
| `SCAN_COIN_COUNT` | 50 | Coins fetched from market API |
| `PROMPT_COIN_LIMIT` | 20 | Coins sent to AI after pre-filter |
| `SCAN_TOP_PICKS` | 5 | Each analyst picks this many |
| `SCAN_FINAL_TOP` | 3 | Judge outputs this many |
| `ANALYST_DELAY_SECONDS` | 3 | Gap between sequential analyst calls |
| `MARKET_CACHE_TTL` | 300 | Seconds to reuse market data |
