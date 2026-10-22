"""Telegram front-end: on-demand signals, push alerts and Crypto Pay billing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from .billing import BillingError, CryptoPayClient, Plan, plans_by_code
from .config import TIMEFRAME_MINUTES, Settings
from .data import CcxtProvider, MarketDataError
from .engine import SignalEngine
from .formatting import RISK_FOOTER, format_scan, format_signal
from .models import Signal
from .ratelimit import RateLimiter
from .storage import Invoice, Store, Subscriber
from .webhook import BillingWebhook

log = logging.getLogger(__name__)

HELP = """<b>PocketTrader signal assistant</b>

<b>Signals</b>
/signal BTC/USDT [5m] — analyse one pair now
/scan [5m] — run the whole watchlist
/watch — show your watchlist
/watch add BTC/USDT — add a pair
/watch remove BTC/USDT — drop a pair
/watch reset — back to the default list

<b>Alerts</b>
/alerts on|off — push new setups as they appear
/settings minconf 0.65 — only alert above this confidence
/settings tf 15m — timeframe used for your alerts
/status — access, settings and watchlist

<b>Access</b>
/buy — purchase access with Crypto Pay
/plans — what is on offer

Direction is in option terms: <b>CALL</b> = expecting up at expiry,
<b>PUT</b> = expecting down. Suggested expiry is three candles of the
timeframe you are on."""


class PocketTraderBot:
    """Wires the engine, the store and Telegram together."""

    def __init__(
        self,
        settings: Settings,
        engine: SignalEngine | None = None,
        provider: CcxtProvider | None = None,
        store: Store | None = None,
        plans: Sequence[Plan] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine or SignalEngine(settings)
        self.provider = provider or CcxtProvider(
            settings.exchange, cache_seconds=settings.cache_seconds
        )
        self.store = store or Store(settings.subscribers_path)
        self.plans = plans_by_code(tuple(plans)) if plans else plans_by_code()
        self.limiter = RateLimiter(limit=settings.rate_limit_per_minute, window=60.0)
        self.billing: CryptoPayClient | None = None
        self.webhook: BillingWebhook | None = None
        self._application: Application | None = None
        if settings.billing_enabled:
            self.billing = CryptoPayClient(
                settings.cryptobot_token, testnet=settings.cryptobot_testnet
            )
            self.webhook = BillingWebhook(
                settings, self.store, self.plans, on_paid=self._announce_payment
            )

    # -- application wiring ----------------------------------------------

    def build_application(self) -> Application:
        if not self.settings.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

        app = (
            Application.builder()
            .token(self.settings.telegram_token)
            .post_init(self._on_startup)
            .post_shutdown(self._on_shutdown)
            .build()
        )
        app.bot_data["bot"] = self
        self._application = app

        # Throttling runs before every handler, hence the negative group.
        app.add_handler(TypeHandler(Update, self._throttle), group=-1)

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("signal", self.cmd_signal))
        app.add_handler(CommandHandler("scan", self.cmd_scan))
        app.add_handler(CommandHandler("watch", self.cmd_watch))
        app.add_handler(CommandHandler("alerts", self.cmd_alerts))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("plans", self.cmd_plans))
        app.add_handler(CommandHandler("buy", self.cmd_buy))
        app.add_handler(CommandHandler("grant", self.cmd_grant))
        app.add_handler(CallbackQueryHandler(self.on_buy_click, pattern=r"^buy:"))
        app.add_error_handler(self.on_error)

        if app.job_queue is not None:
            app.job_queue.run_repeating(
                self.job_scan,
                interval=self.settings.scan_interval_seconds,
                first=15,
                name="scan",
            )
            if self.billing is not None:
                app.job_queue.run_repeating(
                    self.job_poll_invoices,
                    interval=self.settings.invoice_poll_seconds,
                    first=60,
                    name="poll-invoices",
                )
        else:  # pragma: no cover - depends on the optional job-queue extra
            log.warning(
                "job queue unavailable; install python-telegram-bot[job-queue] "
                "for push alerts"
            )
        return app

    def run(self) -> None:
        self.build_application().run_polling(drop_pending_updates=True)

    async def _on_startup(self, app: Application) -> None:
        if self.billing is not None:
            try:
                me = await self.billing.get_me()
                log.info(
                    "Crypto Pay app %s connected (%s)",
                    me.get("name", "?"),
                    "testnet" if self.settings.cryptobot_testnet else "mainnet",
                )
            except BillingError as exc:
                log.error("Crypto Pay token rejected, billing is degraded: %s", exc)
        if self.webhook is not None:
            await self.webhook.start()

    async def _on_shutdown(self, app: Application) -> None:
        if self.webhook is not None:
            await self.webhook.stop()
        if self.billing is not None:
            await self.billing.aclose()

    # -- middleware -------------------------------------------------------

    async def _throttle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return
        if not self.settings.is_chat_allowed(chat.id):
            raise ApplicationHandlerStop
        decision = self.limiter.check(user.id)
        if not decision.allowed:
            await context.bot.send_message(
                chat.id,
                f"Easy — you are over {self.settings.rate_limit_per_minute} commands "
                f"a minute. Try again in {decision.retry_after:.0f}s.",
            )
            raise ApplicationHandlerStop

    # -- helpers ----------------------------------------------------------

    def _subscriber(self, update: Update) -> Subscriber:
        chat = update.effective_chat
        user = update.effective_user
        return self.store.upsert(chat.id, username=user.username or "" if user else "")

    def _timeframe_for(self, sub: Subscriber) -> str:
        return sub.timeframe or self.settings.timeframe

    def _watchlist_for(self, sub: Subscriber) -> tuple[str, ...]:
        return sub.watchlist or self.settings.watchlist

    def _min_confidence_for(self, sub: Subscriber) -> float:
        return (
            sub.min_confidence
            if sub.min_confidence is not None
            else self.settings.min_confidence
        )

    def _has_access(self, sub: Subscriber) -> bool:
        return not self.settings.billing_enabled or sub.has_access

    async def _require_access(self, update: Update, sub: Subscriber) -> bool:
        if self._has_access(sub):
            return True
        await update.effective_message.reply_text(
            "Your access has expired. /buy to renew, /plans to see the options.",
        )
        return False

    async def _analyse(self, symbol: str, timeframe: str) -> Signal:
        """Run one symbol off the event loop — ccxt and pandas both block."""
        return await asyncio.to_thread(
            self.engine.scan_symbol, self.provider, symbol, timeframe
        )

    async def _analyse_many(
        self, symbols: Sequence[str], timeframe: str
    ) -> tuple[list[Signal], dict[str, str]]:
        return await asyncio.to_thread(
            self.engine.scan, self.provider, list(symbols), timeframe
        )

    @staticmethod
    def _parse_timeframe(args: Sequence[str], default: str) -> tuple[str | None, str | None]:
        """Return ``(timeframe, error)`` for an optional timeframe argument."""
        for arg in args:
            if arg.lower() in TIMEFRAME_MINUTES:
                return arg.lower(), None
        for arg in args:
            # A short token with a digit and no slash was meant to be a
            # timeframe, so say so rather than silently using the default.
            if any(c.isdigit() for c in arg) and "/" not in arg and len(arg) <= 4:
                return None, (
                    f"Unknown timeframe {arg!r}. "
                    f"Pick one of: {', '.join(TIMEFRAME_MINUTES)}."
                )
        return default, None

    # -- commands ---------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Whether this chat is new has to be read before the upsert creates it.
        is_new = self.store.get(update.effective_chat.id) is None
        sub = self._subscriber(update)
        greeting = [HELP]

        if self.settings.billing_enabled and is_new and self.settings.free_trial_days > 0:
            sub = self.store.grant_access(sub.chat_id, self.settings.free_trial_days)
            greeting.append(
                f"\n🎁 {self.settings.free_trial_days} days of access to start with."
            )
        elif self.settings.billing_enabled and not sub.has_access:
            greeting.append("\nNo active access — /plans to see what is on offer.")

        greeting.append(f"\n{RISK_FOOTER}")
        await update.effective_message.reply_text(
            "\n".join(greeting), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return

        args = context.args or []
        if not args:
            await update.effective_message.reply_text(
                "Which pair? e.g. <code>/signal BTC/USDT 5m</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        symbol = self.provider.resolve(args[0])
        timeframe, error = self._parse_timeframe(args[1:], self._timeframe_for(sub))
        if error:
            await update.effective_message.reply_text(error)
            return

        message = await update.effective_message.reply_text(
            f"Analysing {escape(symbol)} on {timeframe}…"
        )
        try:
            signal = await self._analyse(symbol, timeframe)
        except MarketDataError as exc:
            await message.edit_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML)
            return
        await message.edit_text(
            format_signal(signal) + f"\n\n{RISK_FOOTER}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return

        timeframe, error = self._parse_timeframe(context.args or [], self._timeframe_for(sub))
        if error:
            await update.effective_message.reply_text(error)
            return

        watchlist = self._watchlist_for(sub)
        message = await update.effective_message.reply_text(
            f"Scanning {len(watchlist)} pairs on {timeframe}…"
        )
        signals, errors = await self._analyse_many(watchlist, timeframe)
        await message.edit_text(format_scan(signals, errors), parse_mode=ParseMode.HTML)

    async def cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        current = list(self._watchlist_for(sub))

        if not args:
            await update.effective_message.reply_text(
                "<b>Watchlist</b>\n" + "\n".join(f"• <code>{escape(s)}</code>" for s in current),
                parse_mode=ParseMode.HTML,
            )
            return

        action = args[0].lower()
        if action == "reset":
            self.store.upsert(sub.chat_id, watchlist=())
            await update.effective_message.reply_text("Watchlist reset to the default list.")
            return

        if action not in {"add", "remove", "rm", "del"} or len(args) < 2:
            await update.effective_message.reply_text(
                "Usage: /watch, /watch add BTC/USDT, /watch remove BTC/USDT, /watch reset"
            )
            return

        symbol = self.provider.resolve(args[1])
        if action == "add":
            if symbol in current:
                await update.effective_message.reply_text(f"{symbol} is already on the list.")
                return
            if len(current) >= 25:
                await update.effective_message.reply_text(
                    "25 pairs is the cap — remove one first."
                )
                return
            try:
                await asyncio.to_thread(
                    self.provider.fetch_ohlcv, symbol, self._timeframe_for(sub), 150
                )
            except MarketDataError as exc:
                await update.effective_message.reply_text(f"⚠️ {exc}")
                return
            current.append(symbol)
        else:
            if symbol not in current:
                await update.effective_message.reply_text(f"{symbol} is not on the list.")
                return
            current.remove(symbol)

        self.store.upsert(sub.chat_id, watchlist=tuple(current))
        await update.effective_message.reply_text(
            f"Watchlist ({len(current)}): " + ", ".join(current)
            if current
            else "Watchlist is now empty — /watch reset restores the defaults."
        )

    async def cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        if not args or args[0].lower() not in {"on", "off"}:
            state = "on" if sub.alerts_enabled else "off"
            await update.effective_message.reply_text(
                f"Alerts are {state}. Use /alerts on or /alerts off."
            )
            return

        wants_on = args[0].lower() == "on"
        if wants_on and not await self._require_access(update, sub):
            return
        self.store.upsert(sub.chat_id, alerts_enabled=wants_on)
        if wants_on:
            await update.effective_message.reply_text(
                f"Alerts on. I will scan every "
                f"{self.settings.scan_interval_seconds // 60} minutes and message you "
                f"when a setup clears {self._min_confidence_for(sub):.0%}."
            )
        else:
            await update.effective_message.reply_text("Alerts off.")

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        if len(args) < 2:
            await update.effective_message.reply_text(
                "<b>Your settings</b>\n"
                f"timeframe <code>{self._timeframe_for(sub)}</code>\n"
                f"min confidence <code>{self._min_confidence_for(sub):.0%}</code>\n\n"
                "Change with <code>/settings tf 15m</code> or "
                "<code>/settings minconf 0.65</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        key, raw = args[0].lower(), args[1]
        if key in {"tf", "timeframe"}:
            if raw.lower() not in TIMEFRAME_MINUTES:
                await update.effective_message.reply_text(
                    f"Pick one of: {', '.join(TIMEFRAME_MINUTES)}."
                )
                return
            self.store.upsert(sub.chat_id, timeframe=raw.lower())
            await update.effective_message.reply_text(f"Timeframe set to {raw.lower()}.")
            return

        if key in {"minconf", "confidence"}:
            try:
                value = float(raw.rstrip("%"))
            except ValueError:
                await update.effective_message.reply_text("Give a number, e.g. 0.65 or 65%.")
                return
            if value > 1:
                value /= 100.0
            if not 0.0 < value <= 1.0:
                await update.effective_message.reply_text("Confidence must be between 0 and 1.")
                return
            self.store.upsert(sub.chat_id, min_confidence=value)
            await update.effective_message.reply_text(f"Minimum confidence set to {value:.0%}.")
            return

        await update.effective_message.reply_text("Unknown setting. Try tf or minconf.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        watchlist = self._watchlist_for(sub)

        if not self.settings.billing_enabled:
            access = "open (billing disabled)"
        elif sub.has_access:
            access = f"active, {sub.access_days_left} day(s) left"
        else:
            access = "expired — /buy to renew"

        await update.effective_message.reply_text(
            "<b>Status</b>\n"
            f"access: {access}\n"
            f"alerts: {'on' if sub.alerts_enabled else 'off'}\n"
            f"timeframe: <code>{self._timeframe_for(sub)}</code>\n"
            f"min confidence: <code>{self._min_confidence_for(sub):.0%}</code>\n"
            f"watchlist ({len(watchlist)}): {escape(', '.join(watchlist))}\n"
            f"exchange: <code>{escape(self.settings.exchange)}</code>\n"
            f"strategies: {escape(', '.join(s.name for s in self.engine.strategies))}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_plans(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.billing_enabled:
            await update.effective_message.reply_text(
                "Billing is disabled on this instance — everything is already open to you."
            )
            return
        lines = ["<b>Plans</b>"] + [f"• {escape(p.describe())}" for p in self.plans.values()]
        lines.append("\nPay in crypto through @CryptoBot. /buy to get an invoice.")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.billing_enabled:
            await update.effective_message.reply_text(
                "Billing is disabled on this instance — nothing to buy."
            )
            return
        self._subscriber(update)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(plan.describe(), callback_data=f"buy:{plan.code}")]
                for plan in self.plans.values()
            ]
        )
        await update.effective_message.reply_text("Pick a plan:", reply_markup=keyboard)

    async def on_buy_click(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        plan = self.plans.get((query.data or "").split(":", 1)[-1])
        if plan is None or self.billing is None:
            await query.edit_message_text("That plan is no longer available.")
            return

        chat_id = update.effective_chat.id
        try:
            invoice = await self.billing.create_invoice(plan, chat_id)
        except BillingError as exc:
            log.error("invoice creation failed for chat %s: %s", chat_id, exc)
            await query.edit_message_text(
                "Could not create the invoice. Try again in a minute."
            )
            return

        self.store.add_invoice(
            Invoice(
                invoice_id=invoice.invoice_id,
                chat_id=chat_id,
                plan=plan.code,
                days=plan.days,
                amount=invoice.amount,
                asset=invoice.asset,
            )
        )
        await query.edit_message_text(
            f"<b>{escape(plan.title)}</b> — {invoice.amount} {invoice.asset}\n"
            "Pay through the button below; access opens the moment it clears.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Pay with Crypto Pay", url=invoice.pay_url)]]
            ),
        )

    async def cmd_grant(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin-only: hand out access without an invoice."""
        chat_id = update.effective_chat.id
        if not self.settings.is_admin(chat_id):
            return
        args = context.args or []
        if len(args) != 2:
            await update.effective_message.reply_text("Usage: /grant <chat_id> <days>")
            return
        try:
            target, days = int(args[0]), int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Both arguments must be whole numbers.")
            return
        sub = self.store.grant_access(target, days)
        await update.effective_message.reply_text(
            f"Chat {target} now has access until {sub.paid_until:%Y-%m-%d %H:%M UTC}."
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("handler failed", exc_info=context.error)

    # -- background jobs --------------------------------------------------

    async def job_scan(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Scan every subscriber's watchlist and push what is new to them."""
        subscribers = [
            s for s in self.store.alerting_subscribers() if self._has_access(s)
        ]
        if not subscribers:
            return

        # One fetch per (symbol, timeframe) serves every subscriber watching it.
        wanted: dict[str, set[str]] = {}
        for sub in subscribers:
            wanted.setdefault(self._timeframe_for(sub), set()).update(self._watchlist_for(sub))

        results: dict[tuple[str, str], Signal] = {}
        for timeframe, symbols in wanted.items():
            signals, errors = await self._analyse_many(sorted(symbols), timeframe)
            for signal in signals:
                results[(signal.symbol, timeframe)] = signal
            for symbol, message in errors.items():
                log.warning("scan job: %s %s: %s", symbol, timeframe, message)

        for sub in subscribers:
            timeframe = self._timeframe_for(sub)
            threshold = self._min_confidence_for(sub)
            for symbol in self._watchlist_for(sub):
                signal = results.get((symbol, timeframe))
                if signal is None or not signal.actionable:
                    continue
                if signal.confidence < threshold:
                    continue
                key = "|".join(signal.key())
                if not self.store.should_alert(
                    sub.chat_id, key, self.settings.alert_cooldown_seconds
                ):
                    continue
                try:
                    await context.bot.send_message(
                        sub.chat_id,
                        format_signal(signal),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    log.exception("could not deliver an alert to chat %s", sub.chat_id)
                    continue
                self.store.mark_alerted(sub.chat_id, key)

    async def job_poll_invoices(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Catch payments whose webhook never arrived (tunnel down, retry lost)."""
        if self.billing is None or self.webhook is None:
            return
        for pending in self.store.pending_invoices():
            try:
                remote = await self.billing.get_invoice(pending.invoice_id)
            except BillingError as exc:
                log.warning("could not poll invoice %s: %s", pending.invoice_id, exc)
                continue
            if remote and remote.get("status") == "paid":
                log.info("invoice %s settled by polling", pending.invoice_id)
                await self.webhook.settle(pending.invoice_id, pending.chat_id, pending.plan)

    async def _announce_payment(self, chat_id: int, plan: Plan, invoice: Invoice) -> None:
        """Called by the webhook once an invoice is credited."""
        application = self._application
        if application is None:  # pragma: no cover - only reachable before startup
            return
        sub = self.store.get(chat_id)
        days_left = sub.access_days_left if sub else plan.days
        await application.bot.send_message(
            chat_id,
            f"✅ Payment received — <b>{escape(plan.title)}</b> is active.\n"
            f"{days_left} day(s) of access. /alerts on to start receiving setups.",
            parse_mode=ParseMode.HTML,
        )


def run_bot(settings: Settings) -> None:
    """Build and run the bot until interrupted."""
    PocketTraderBot(settings).run()
