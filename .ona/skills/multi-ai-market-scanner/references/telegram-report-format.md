# Telegram Report Format

## Full formatter

```python
def _signal_emoji(sig: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig.upper(), "⚪")

def _conf_bar(confidence: int) -> str:
    filled = round(confidence / 10)
    return "█" * filled + "░" * (10 - filled)

def _build_scan_report(result: ScanResult) -> str:
    lines = [
        "🔍 *تقرير الماسح الذكي — Smart Scanner*",
        f"📊 عملات تم فحصها: `{result.coins_scanned}`",
        f"⏱️ وقت التحليل: `{result.scan_duration_s:.0f}` ثانية",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *أفضل الفرص (قرار القاضي النهائي)*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for pick in result.final_picks:
        medal = medals[pick.rank - 1] if pick.rank <= len(medals) else f"{pick.rank}."
        analysts_str = " & ".join(pick.analysts) if pick.analysts else "—"
        lines += [
            "",
            f"{medal} *{pick.name}* (`{pick.symbol}`)",
            f"  {_signal_emoji(pick.signal)} الإشارة: `{pick.signal}` | "
            f"الثقة: `{_conf_bar(pick.confidence)}` {pick.confidence}%",
            f"  💬 _{pick.reason}_",
            f"  👥 اتفق عليها: `{analysts_str}`",
        ]

    # Analyst summaries
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧠 *تقارير المحللين الثلاثة*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for report in result.analyst_reports:
        if report.error:
            lines.append(f"  ⚠️ *{report.analyst_name}*: خطأ — `{report.error[:60]}`")
            continue
        picks_str = ", ".join(f"{p.symbol}({p.confidence}%)" for p in report.picks)
        lines.append(f"  🤖 *{report.analyst_name}*: {picks_str}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ _هذا تحليل AI للمعلومات فقط — ليس نصيحة مالية._",
    ]
    return "\n".join(lines)
```

## Sample output

```
🔍 *تقرير الماسح الذكي — Smart Scanner*
📊 عملات تم فحصها: `46`
⏱️ وقت التحليل: `27` ثانية

━━━━━━━━━━━━━━━━━━━━━━━━
🏆 *أفضل الفرص (قرار القاضي النهائي)*
━━━━━━━━━━━━━━━━━━━━━━━━

🥇 *Bitcoin* (`BTC`)
  🟢 الإشارة: `BUY` | الثقة: `████████░░` 78%
  💬 _Strong institutional inflows and positive macro outlook_
  👥 اتفق عليها: `Hermes-405B & LLaMA-70B`

🥈 *Ethereum* (`ETH`)
  🟢 الإشارة: `BUY` | الثقة: `███████░░░` 74%
  💬 _Upcoming network upgrades and rising DeFi demand_
  👥 اتفق عليها: `Hermes-405B & Gemma-27B`

🥉 *Solana* (`SOL`)
  ⚪ الإشارة: `HOLD` | الثقة: `██████░░░░` 65%
  💬 _Recent technical stabilization after volatility_
  👥 اتفق عليها: `LLaMA-70B & Gemma-27B`

━━━━━━━━━━━━━━━━━━━━━━━━
🧠 *تقارير المحللين الثلاثة*
━━━━━━━━━━━━━━━━━━━━━━━━
  🤖 *LLaMA-70B*: BTC(82%), ETH(75%), SOL(70%)
  ⚠️ *Gemma-12B*: خطأ — `HTTP Error 429`
  🤖 *GPT-OSS-20B*: ETH(80%), BTC(78%), ADA(65%)

━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ _هذا تحليل AI للمعلومات فقط — ليس نصيحة مالية._
```

## Telegram Markdown gotchas

- Use `ParseMode.MARKDOWN` (not MARKDOWN_V2) for simpler escaping
- Wrap dynamic text in backticks `` `value` `` to avoid special char issues
- Wrap reasons in `_italic_` — if the reason contains underscores, it will break; sanitize with `reason.replace("_", " ")`
- Separator line `━━━━━━━━━━━━━━━━━━━━━━━━` renders as a visual divider in Telegram
- Max message length: 4096 chars. If report exceeds this, split at the analyst section

## Sanitizing reason text for Markdown

```python
def _safe_reason(text: str) -> str:
    # Remove characters that break Telegram Markdown
    return text.replace("_", " ").replace("*", "").replace("`", "").replace("[", "").replace("]", "")
```

Apply before inserting into `_{reason}_`.
