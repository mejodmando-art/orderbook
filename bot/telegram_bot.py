"""
Telegram bot interface.

Commands:
    /setup  <symbol> <quote_amount> [sl_pct] [tp_pct]
        Configure and start the market-maker strategy.
        Example: /setup BTCUSDT 100 1.5 2.0

    /status
        Show current trade state, SMA, and session P&L.

    /emergency_stop
        Immediately cancel all open orders and market-sell any open position.

All commands are restricted to ALLOWED_USER_IDS from .env.
The bot uses python-telegram-bot v20+ (Application / async handlers).
"""

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config.settings import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from core import trade_exec
from core.analyzer import MarketAnalyzer
from core.mexc_ws import MexcWebSocket
from config.settings import get_precision

if TYPE_CHECKING:
    from core.trade_exec import VirtualStopLossWatcher

logger = logging.getLogger(__name__)

STATE_PATH = "data/state.json"


# ── Shared bot state ───────────────────────────────────────────────────────────

class BotState:
    """Mutable runtime state shared across command handlers."""

    def __init__(self) -> None:
        self.ws = MexcWebSocket()
        self.analyzer = MarketAnalyzer()
        self.ws_task: asyncio.Task | None = None
        self.sl_watcher: "VirtualStopLossWatcher | None" = None
        self.sl_task: asyncio.Task | None = None
        self.fill_poll_task: asyncio.Task | None = None
        self.running: bool = False

        # Loaded from / persisted to state.json
        self.config: dict = {}
        self.trade: dict = {}
        self.session: dict = {}

        self._load()

    def _load(self) -> None:
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
            self.config = data.get("config", {})
            self.trade = data.get("trade", {})
            self.session = data.get("session", {})
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = {}
            self.trade = {"status": "idle"}
            self.session = {"total_trades": 0, "total_pnl": 0.0, "wins": 0, "losses": 0}

    def save(self) -> None:
        with open(STATE_PATH, "w") as f:
            json.dump(
                {"config": self.config, "trade": self.trade, "session": self.session},
                f, indent=2,
            )

    def reset_trade(self) -> None:
        self.trade = {
            "status": "idle",
            "entry_order_id": None,
            "exit_order_id": None,
            "entry_price": None,
            "entry_qty": None,
            "stop_loss_price": None,
            "take_profit_price": None,
            "filled_at": None,
            "closed_at": None,
            "pnl": None,
            "exit_reason": None,
        }
        self.save()


_state = BotState()


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ALLOWED_USER_IDS


async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ Unauthorized.")


# ── Telegram notifier (used by VirtualSL and other modules) ───────────────────

async def send_alert(text: str, application: Application | None = None) -> None:
    """
    Send a Markdown message to all allowed users.
    Falls back to logging if the application is not available.
    """
    if not ALLOWED_USER_IDS:
        logger.warning("No ALLOWED_USER_IDS configured — cannot send alert")
        return
    if application is None:
        logger.info("Alert (no app): %s", text)
        return
    for uid in ALLOWED_USER_IDS:
        try:
            await application.bot.send_message(
                chat_id=uid, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as exc:
            logger.error("Failed to send alert to %s: %s", uid, exc)


# ── WebSocket handlers ─────────────────────────────────────────────────────────

async def _on_ticker(msg: dict) -> None:
    """
    Handle spot@public.miniTicker.v3.api messages.
    Updates the analyzer price and feeds the SL watcher.
    """
    data = msg.get("d", {})
    price_str = data.get("c") or data.get("p")  # last price or close
    if not price_str:
        return
    try:
        price = float(price_str)
    except ValueError:
        return

    _state.analyzer.update_price(price)

    if _state.sl_watcher:
        _state.sl_watcher.update_price(price)


async def _on_depth(msg: dict) -> None:
    """Handle spot@public.increase.depth.v3.api order-book messages."""
    data = msg.get("d", {})
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if bids or asks:
        _state.analyzer.update_depth(bids, asks)


# ── Strategy orchestration ─────────────────────────────────────────────────────

async def _on_order_filled(order: dict, application: Application) -> None:
    """
    Called when the limit buy order is confirmed FILLED.
    1. Records entry details in state.
    2. Places the limit take-profit sell.
    3. Arms the Virtual Stop-Loss watcher.
    """
    symbol: str = _state.config["symbol"]
    entry_price = float(order.get("price", 0))
    entry_qty = float(order.get("executedQty", 0))
    sl_pct: float = _state.config.get("stop_loss_pct", 0.985)
    tp_pct: float = _state.config.get("take_profit_pct", 1.02)

    tp_price = round(entry_price * tp_pct, get_precision(symbol)["price_precision"])

    # Place take-profit limit sell
    try:
        tp_resp = await trade_exec.place_limit_sell(symbol, tp_price, entry_qty)
        exit_order_id = tp_resp.get("orderId")
    except Exception as exc:
        logger.error("Failed to place TP sell: %s", exc)
        exit_order_id = None

    # Persist trade state
    _state.trade.update({
        "status": "open",
        "entry_price": entry_price,
        "entry_qty": entry_qty,
        "exit_order_id": exit_order_id,
        "stop_loss_price": round(entry_price * sl_pct, 8),
        "take_profit_price": tp_price,
        "filled_at": int(time.time()),
    })
    _state.save()

    await send_alert(
        f"✅ *Order Filled*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Entry: `{entry_price}`\n"
        f"Qty: `{entry_qty}`\n"
        f"TP: `{tp_price}`\n"
        f"SL: `{_state.trade['stop_loss_price']}`",
        application,
    )

    # Arm Virtual Stop-Loss
    async def _notifier(text: str) -> None:
        await send_alert(text, application)

    async def _on_sl_triggered() -> None:
        _state.reset_trade()
        _state.sl_watcher = None

    watcher = trade_exec.VirtualStopLossWatcher(
        symbol=symbol,
        entry_price=entry_price,
        qty=entry_qty,
        exit_order_id=exit_order_id,
        stop_loss_pct=sl_pct,
        notifier=_notifier,
        on_triggered=_on_sl_triggered,
    )
    _state.sl_watcher = watcher
    _state.sl_task = asyncio.create_task(watcher.run())
    logger.info("Virtual SL watcher task created")


async def _start_strategy(application: Application) -> None:
    """
    Subscribe to WS streams, start the WS task, and place the initial
    limit buy order based on the analyzer's entry signal.
    """
    symbol: str = _state.config["symbol"]
    quote_amount: float = _state.config["quote_amount"]
    tick_size: float = float(get_precision(symbol)["tick_size"])

    # Subscribe to ticker and depth streams
    ticker_topic = f"spot@public.miniTicker.v3.api@{symbol}"
    depth_topic = f"spot@public.increase.depth.v3.api@{symbol}@5"

    _state.ws.subscribe(ticker_topic, _on_ticker)
    _state.ws.subscribe(depth_topic, _on_depth)

    if _state.ws_task is None or _state.ws_task.done():
        _state.ws_task = asyncio.create_task(_state.ws.run())
        logger.info("WebSocket task started")

    # Wait briefly for initial data
    await asyncio.sleep(3)

    # Reconfigure analyzer with current settings
    _state.analyzer = MarketAnalyzer(
        wall_multiplier=_state.config.get("wall_multiplier", 3.0),
        sma_period=_state.config.get("sma_period", 20),
        price_precision=get_precision(symbol)["price_precision"],
    )

    # Scan for entry signal (retry loop — waits for SMA to warm up)
    signal = None
    for attempt in range(30):
        signal = _state.analyzer.find_entry_signal(tick_size)
        if signal:
            break
        logger.debug("No signal yet (attempt %d/30) — waiting 2s", attempt + 1)
        await asyncio.sleep(2)

    if not signal:
        await send_alert(
            "⚠️ *No entry signal found after 60s.*\n"
            "Check market conditions or adjust wall multiplier.",
            application,
        )
        return

    # Place limit buy
    try:
        buy_resp = await trade_exec.place_limit_buy(symbol, signal.entry_price, quote_amount)
    except Exception as exc:
        await send_alert(f"❌ *Limit buy failed:* `{exc}`", application)
        return

    order_id = buy_resp.get("orderId")
    _state.trade.update({
        "status": "pending_fill",
        "entry_order_id": order_id,
        "entry_price": signal.entry_price,
    })
    _state.save()

    await send_alert(
        f"📋 *Limit Buy Placed*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Price: `{signal.entry_price}`\n"
        f"Wall: `{signal.wall_price}` (vol `{signal.wall_volume:.2f}`)\n"
        f"SMA-20: `{signal.sma:.8f}`\n"
        f"Order ID: `{order_id}`",
        application,
    )

    # Start fill poller
    async def _filled_cb(order: dict) -> None:
        await _on_order_filled(order, application)

    _state.fill_poll_task = asyncio.create_task(
        trade_exec.poll_order_fill(symbol, order_id, _filled_cb)
    )


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setup <symbol> <quote_amount> [sl_pct] [tp_pct]

    Configures the bot and launches the strategy.
    sl_pct and tp_pct are percentages (e.g. 1.5 means 1.5% stop-loss).
    """
    if not _is_allowed(update):
        await _deny(update)
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/setup <symbol> <quote_amount> [sl_pct] [tp_pct]`\n"
            "Example: `/setup BTCUSDT 100 1.5 2.0`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    symbol = args[0].upper()
    try:
        quote_amount = float(args[1])
        sl_pct = 1 - float(args[2]) / 100 if len(args) > 2 else 0.985
        tp_pct = 1 + float(args[3]) / 100 if len(args) > 3 else 1.02
    except ValueError:
        await update.message.reply_text("❌ Invalid numeric argument.")
        return

    if _state.running:
        await update.message.reply_text("⚠️ Bot is already running. Use /emergency_stop first.")
        return

    # Fetch precision from MEXC
    await update.message.reply_text(f"🔍 Fetching precision for `{symbol}`…",
                                    parse_mode=ParseMode.MARKDOWN)
    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to fetch symbol info: `{exc}`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    prec = get_precision(symbol)
    _state.config = {
        "symbol": symbol,
        "quote_amount": quote_amount,
        "wall_multiplier": 3.0,
        "sma_period": 20,
        "stop_loss_pct": sl_pct,
        "take_profit_pct": tp_pct,
        "price_precision": prec["price_precision"],
        "qty_precision": prec["qty_precision"],
        "tick_size": prec["tick_size"],
    }
    _state.session["started_at"] = int(time.time())
    _state.save()
    _state.running = True

    await update.message.reply_text(
        f"✅ *Setup complete*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Quote: `{quote_amount}`\n"
        f"SL: `{(1 - sl_pct) * 100:.2f}%`\n"
        f"TP: `{(tp_pct - 1) * 100:.2f}%`\n"
        f"Tick: `{prec['tick_size']}`\n\n"
        f"Starting strategy…",
        parse_mode=ParseMode.MARKDOWN,
    )

    asyncio.create_task(_start_strategy(context.application))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show current trade and session summary."""
    if not _is_allowed(update):
        await _deny(update)
        return

    trade = _state.trade
    cfg = _state.config
    sess = _state.session
    symbol = cfg.get("symbol", "—")

    sma = _state.analyzer.sma()
    sma_str = f"`{sma:.8f}`" if sma else "_not ready_"

    lines = [
        f"📊 *Bot Status*",
        f"",
        f"*Symbol:* `{symbol}`",
        f"*Running:* `{_state.running}`",
        f"*Trade status:* `{trade.get('status', 'idle')}`",
        f"",
        f"*Entry price:* `{trade.get('entry_price', '—')}`",
        f"*Entry qty:* `{trade.get('entry_qty', '—')}`",
        f"*Stop-loss:* `{trade.get('stop_loss_price', '—')}`",
        f"*Take-profit:* `{trade.get('take_profit_price', '—')}`",
        f"",
        f"*SMA-20:* {sma_str}",
        f"",
        f"*Session trades:* `{sess.get('total_trades', 0)}`",
        f"*Session P&L:* `{sess.get('total_pnl', 0.0):.4f}`",
        f"*Wins / Losses:* `{sess.get('wins', 0)} / {sess.get('losses', 0)}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emergency_stop — cancel all orders and market-sell the open position."""
    if not _is_allowed(update):
        await _deny(update)
        return

    await update.message.reply_text("🛑 *Emergency stop initiated…*", parse_mode=ParseMode.MARKDOWN)

    symbol = _state.config.get("symbol")
    if not symbol:
        await update.message.reply_text("No active symbol configured.")
        return

    # Cancel SL watcher
    if _state.sl_watcher:
        _state.sl_watcher.cancel()
        _state.sl_watcher = None

    # Cancel fill poller
    if _state.fill_poll_task and not _state.fill_poll_task.done():
        _state.fill_poll_task.cancel()

    # Cancel open entry order if still pending
    entry_order_id = _state.trade.get("entry_order_id")
    if entry_order_id and _state.trade.get("status") == "pending_fill":
        try:
            await trade_exec.cancel_order(symbol, entry_order_id)
            await update.message.reply_text(f"✅ Entry order `{entry_order_id}` cancelled.",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Could not cancel entry order: `{exc}`",
                                            parse_mode=ParseMode.MARKDOWN)

    # Cancel open exit order
    exit_order_id = _state.trade.get("exit_order_id")
    if exit_order_id and _state.trade.get("status") == "open":
        try:
            await trade_exec.cancel_order(symbol, exit_order_id)
            await update.message.reply_text(f"✅ Exit order `{exit_order_id}` cancelled.",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Could not cancel exit order: `{exc}`",
                                            parse_mode=ParseMode.MARKDOWN)

    # Market sell if position is open
    entry_qty = _state.trade.get("entry_qty")
    if entry_qty and _state.trade.get("status") == "open":
        try:
            resp = await trade_exec.market_sell(symbol, float(entry_qty))
            await update.message.reply_text(
                f"✅ Market sell executed. Order ID: `{resp.get('orderId')}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Market sell failed: `{exc}`",
                                            parse_mode=ParseMode.MARKDOWN)

    # Stop WebSocket
    _state.ws.stop()
    if _state.ws_task and not _state.ws_task.done():
        _state.ws_task.cancel()

    _state.running = False
    _state.reset_trade()

    await update.message.reply_text("🔴 *Bot stopped. All positions closed.*",
                                    parse_mode=ParseMode.MARKDOWN)


# ── Application factory ────────────────────────────────────────────────────────

def build_application() -> Application:
    """Build and return the configured telegram Application."""
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))
    return app
