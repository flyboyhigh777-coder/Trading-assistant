"""Telegram-бот: доходность выпусков, портфель, напоминания о купонах, оплата."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
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

from .billing import ClickProvider, PaymeProvider, Plan, plans_by_code
from .bonds import Bond, Position, cashflows, next_coupon, portfolio_cashflows
from .config import Settings
from .formatting import (
    DISCLAIMER,
    format_bond,
    format_bond_list,
    format_calendar,
    format_date,
    format_portfolio,
    format_quotes,
    plural_days,
)
from .market import CatalogError, MarketDataError, UzseClient, find, load_catalog
from .money import format_percent, format_sum, to_decimal
from .ratelimit import RateLimiter
from .storage import Order, Store

log = logging.getLogger(__name__)

HELP = """<b>Помощник по облигациям УзРФБ</b>

<b>Выпуски</b>
/bonds — все выпуски, по эффективной ставке
/bond HMMT3B — карточка выпуска с реальной доходностью
/bond HMMT3B 5 — то же самое на 5 бумаг

<b>Портфель</b>
/add UZ6058537AA0 3 — добавить бумагу
/remove UZ6058537AA0 — убрать
/portfolio — что лежит и сколько приносит
/calendar — ближайшие выплаты

<b>Настройки сравнения</b>
/settings вклад 22 — с какой ставкой по вкладу сравнивать
/settings инфляция 10 — для реальной доходности
/alerts вкл — напоминать о купонах заранее

<b>Прочее</b>
/quotes — акции и когда по ним последний раз была сделка
/status — доступ и настройки
/plans, /buy — оплата доступа

<i>Это калькулятор, а не брокер: бот ничего не покупает и не продаёт.</i>"""


class UzInvestBot:
    """Связывает каталог, хранилище, биржу и платежи."""

    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        catalog: dict[str, Bond] | None = None,
        uzse: UzseClient | None = None,
        plans: dict[str, Plan] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or Store(settings.subscribers_path)
        self.catalog = catalog if catalog is not None else self._load_catalog()
        self.uzse = uzse or UzseClient(
            stocks_url=settings.stocks_url, bonds_url=settings.bonds_url
        )
        self.plans = plans or plans_by_code()
        self.limiter = RateLimiter(limit=settings.rate_limit_per_minute, window=60.0)
        self._application: Application | None = None

        self.payme = (
            PaymeProvider(
                merchant_id=settings.payme_merchant_id,
                merchant_key=settings.payme_merchant_key,
                account_field=settings.payme_account_field,
                test_mode=settings.payme_test_mode,
            )
            if settings.payme_enabled
            else None
        )
        self.click = (
            ClickProvider(
                service_id=settings.click_service_id,
                merchant_id=settings.click_merchant_id,
                secret_key=settings.click_secret_key,
                merchant_user_id=settings.click_merchant_user_id,
            )
            if settings.click_enabled
            else None
        )

        from .webhook import BillingRoutes

        self.routes = (
            BillingRoutes(
                settings, self.store, self.plans, self.payme, self.click,
                on_paid=self._announce_payment,
            )
            if settings.billing_enabled
            else None
        )

    def _load_catalog(self) -> dict[str, Bond]:
        try:
            return load_catalog(self.settings.catalog_path)
        except CatalogError as exc:
            log.error("каталог не загрузился (%s), работаю с пустым", exc)
            return {}

    # -- сборка -----------------------------------------------------------

    def build_application(self) -> Application:
        if not self.settings.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

        app = (
            Application.builder()
            .token(self.settings.telegram_token)
            .post_init(self._on_startup)
            .post_shutdown(self._on_shutdown)
            .build()
        )
        self._application = app
        app.add_handler(TypeHandler(Update, self._throttle), group=-1)

        for command, handler in (
            ("start", self.cmd_start),
            ("help", self.cmd_help),
            ("bonds", self.cmd_bonds),
            ("bond", self.cmd_bond),
            ("add", self.cmd_add),
            ("remove", self.cmd_remove),
            ("clear", self.cmd_clear),
            ("portfolio", self.cmd_portfolio),
            ("calendar", self.cmd_calendar),
            ("quotes", self.cmd_quotes),
            ("settings", self.cmd_settings),
            ("alerts", self.cmd_alerts),
            ("status", self.cmd_status),
            ("plans", self.cmd_plans),
            ("buy", self.cmd_buy),
            ("grant", self.cmd_grant),
        ):
            app.add_handler(CommandHandler(command, handler))
        app.add_handler(CallbackQueryHandler(self.on_buy_click, pattern=r"^buy:"))
        app.add_error_handler(self.on_error)

        if app.job_queue is not None:
            app.job_queue.run_daily(
                self.job_coupon_reminders,
                time=dt.time(hour=self.settings.reminder_hour_utc, tzinfo=dt.timezone.utc),
                name="coupon-reminders",
            )
        else:  # pragma: no cover
            log.warning("нет job queue: напоминания о купонах не будут работать")
        return app

    def run(self) -> None:
        self.build_application().run_polling(drop_pending_updates=True)

    async def _on_startup(self, app: Application) -> None:
        log.info("каталог: %s выпусков", len(self.catalog))
        if self.routes is not None:
            await self.routes.start()
            for name, url in self.settings.callback_urls().items():
                log.info("впишите в кабинет %s: %s", name, url)
        elif self.settings.billing_enabled:  # pragma: no cover
            log.warning("ключи касс заданы, но маршруты не поднялись")
        else:
            log.warning("оплата выключена — доступ открыт всем")

    async def _on_shutdown(self, app: Application) -> None:
        if self.routes is not None:
            await self.routes.stop()

    # -- middleware -------------------------------------------------------

    async def _throttle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None:
            return
        if not self.settings.is_chat_allowed(chat.id):
            raise ApplicationHandlerStop
        decision = self.limiter.check(user.id)
        if not decision.allowed:
            await context.bot.send_message(
                chat.id,
                f"Слишком часто — не больше {self.settings.rate_limit_per_minute} "
                f"команд в минуту. Попробуйте через {decision.retry_after:.0f} с.",
            )
            raise ApplicationHandlerStop

    # -- помощники --------------------------------------------------------

    def _subscriber(self, update: Update):
        chat, user = update.effective_chat, update.effective_user
        return self.store.upsert(chat.id, username=(user.username or "") if user else "")

    def _has_access(self, sub) -> bool:
        return not self.settings.billing_enabled or sub.has_access

    async def _require_access(self, update: Update, sub) -> bool:
        if self._has_access(sub):
            return True
        await update.effective_message.reply_text(
            "Доступ закончился. /plans — тарифы, /buy — оплатить."
        )
        return False

    def _deposit_rate(self, sub) -> Decimal:
        if sub.deposit_rate is not None:
            return sub.deposit_rate
        return self.settings.default_deposit_rate

    def _inflation(self, sub) -> Decimal:
        return sub.inflation if sub.inflation is not None else self.settings.default_inflation

    @staticmethod
    def _parse_rate(raw: str) -> Decimal | None:
        """Принять «22», «22%» и «0.22» одинаково."""
        try:
            value = to_decimal(raw.rstrip("%").replace(",", "."))
        except Exception:  # noqa: BLE001
            return None
        if value > 1:
            value /= 100
        return value if 0 <= value < 10 else None

    # -- команды ----------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        is_new = self.store.get(update.effective_chat.id) is None
        sub = self._subscriber(update)
        parts = [HELP]
        if self.settings.billing_enabled and is_new and self.settings.free_trial_days > 0:
            self.store.grant_access(sub.chat_id, self.settings.free_trial_days)
            parts.append(
                f"\n🎁 {plural_days(self.settings.free_trial_days)} доступа для начала."
            )
        elif self.settings.billing_enabled and not sub.has_access:
            parts.append("\nДоступа нет — /plans покажет тарифы.")
        await update.effective_message.reply_text(
            "\n".join(parts), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def cmd_bonds(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        await update.effective_message.reply_text(
            format_bond_list(self.catalog), parse_mode=ParseMode.HTML
        )

    async def cmd_bond(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        args = context.args or []
        if not args:
            await update.effective_message.reply_text(
                "Какой выпуск? Например: <code>/bond HMMT3B</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        bond = find(self.catalog, args[0])
        if bond is None:
            await update.effective_message.reply_text(
                f"Выпуск «{escape(args[0])}» не нашёлся. /bonds — список."
            )
            return
        quantity = 1
        if len(args) > 1 and args[1].isdigit():
            quantity = max(1, min(int(args[1]), 1_000_000))
        await update.effective_message.reply_text(
            format_bond(
                bond,
                deposit_rate=self._deposit_rate(sub),
                inflation=self._inflation(sub),
                quantity=quantity,
            ),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        args = context.args or []
        if len(args) < 2 or not args[1].isdigit():
            await update.effective_message.reply_text(
                "Формат: <code>/add UZ6058537AA0 3</code>", parse_mode=ParseMode.HTML
            )
            return
        bond = find(self.catalog, args[0])
        if bond is None:
            await update.effective_message.reply_text(
                f"Выпуск «{escape(args[0])}» не нашёлся. /bonds — список."
            )
            return
        quantity = int(args[1])
        if not 1 <= quantity <= 1_000_000:
            await update.effective_message.reply_text("Количество — от 1 до 1 000 000.")
            return
        self.store.set_position(sub.chat_id, Position(bond.isin, quantity))
        coupon = next_coupon(bond, dt.date.today())
        tail = (
            f" Ближайшая выплата {format_date(coupon[0])}: {format_sum(coupon[1] * quantity)}."
            if coupon
            else " Выпуск уже погашен."
        )
        await update.effective_message.reply_text(
            f"{bond.ticker} × {quantity} — записал.{tail}"
        )

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        if not args:
            await update.effective_message.reply_text(
                "Формат: <code>/remove UZ6058537AA0</code>", parse_mode=ParseMode.HTML
            )
            return
        bond = find(self.catalog, args[0])
        isin = bond.isin if bond else args[0]
        if self.store.remove_position(sub.chat_id, isin):
            await update.effective_message.reply_text("Убрал.")
        else:
            await update.effective_message.reply_text("Такой бумаги в портфеле нет.")

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        self.store.clear_positions(sub.chat_id)
        await update.effective_message.reply_text("Портфель очищен.")

    async def cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        today = dt.date.today()
        flows = portfolio_cashflows(
            list(sub.positions), self.catalog, since=today,
            until=today + dt.timedelta(days=366),
        )
        await update.effective_message.reply_text(
            format_portfolio(list(sub.positions), self.catalog, flows, today),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_calendar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        today = dt.date.today()
        flows = portfolio_cashflows(list(sub.positions), self.catalog, since=today)
        await update.effective_message.reply_text(
            format_calendar(flows), parse_mode=ParseMode.HTML
        )

    async def cmd_quotes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not await self._require_access(update, sub):
            return
        message = await update.effective_message.reply_text("Загружаю котировки…")
        try:
            quotes = await asyncio.to_thread(self.uzse.quotes, "stocks")
        except MarketDataError as exc:
            await message.edit_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML)
            return
        await message.edit_text(format_quotes(quotes), parse_mode=ParseMode.HTML)

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        if len(args) < 2:
            await update.effective_message.reply_text(
                "<b>Настройки сравнения</b>\n"
                f"ставка по вкладу: <code>{format_percent(self._deposit_rate(sub), 0)}</code>\n"
                f"инфляция: <code>{format_percent(self._inflation(sub), 0)}</code>\n\n"
                "Изменить: <code>/settings вклад 24</code> или "
                "<code>/settings инфляция 11</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        key, raw = args[0].lower(), args[1]
        value = self._parse_rate(raw)
        if value is None:
            await update.effective_message.reply_text("Нужно число, например 22 или 0.22.")
            return
        if key in {"вклад", "депозит", "deposit"}:
            self.store.upsert(sub.chat_id, deposit_rate=value)
            await update.effective_message.reply_text(
                f"Сравниваю с вкладом под {format_percent(value, 0)}."
            )
        elif key in {"инфляция", "inflation"}:
            self.store.upsert(sub.chat_id, inflation=value)
            await update.effective_message.reply_text(
                f"Инфляция для расчёта: {format_percent(value, 0)}."
            )
        else:
            await update.effective_message.reply_text("Доступно: вклад, инфляция.")

    async def cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        args = context.args or []
        wanted = args[0].lower() if args else ""
        if wanted not in {"вкл", "выкл", "on", "off"}:
            state = "включены" if sub.alerts_enabled else "выключены"
            await update.effective_message.reply_text(
                f"Напоминания {state}. /alerts вкл или /alerts выкл."
            )
            return
        enable = wanted in {"вкл", "on"}
        if enable and not await self._require_access(update, sub):
            return
        self.store.upsert(sub.chat_id, alerts_enabled=enable)
        await update.effective_message.reply_text(
            f"Напоминаю за {plural_days(self.settings.coupon_reminder_days)} до выплаты."
            if enable
            else "Напоминания выключены."
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sub = self._subscriber(update)
        if not self.settings.billing_enabled:
            access = "открыт (оплата выключена)"
        elif sub.has_access:
            access = f"активен, осталось {plural_days(sub.access_days_left)}"
        else:
            access = "закончился — /buy"
        await update.effective_message.reply_text(
            "<b>Статус</b>\n"
            f"доступ: {access}\n"
            f"напоминания: {'вкл' if sub.alerts_enabled else 'выкл'}\n"
            f"бумаг в портфеле: {len(sub.positions)}\n"
            f"выпусков в каталоге: {len(self.catalog)}\n"
            f"вклад для сравнения: {format_percent(self._deposit_rate(sub), 0)}\n"
            f"инфляция: {format_percent(self._inflation(sub), 0)}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_plans(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.billing_enabled:
            await update.effective_message.reply_text(
                "Оплата на этом сервере выключена — всё и так открыто."
            )
            return
        ways = [p.label for p in (self.payme, self.click) if p and p.is_configured()]
        lines = ["<b>Тарифы</b>"]
        lines += [f"• {escape(p.describe())}" for p in self.plans.values()]
        lines.append(f"\nОплата: {', '.join(ways)}. /buy — выставить счёт.")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.billing_enabled:
            await update.effective_message.reply_text("Оплата выключена, покупать нечего.")
            return
        self._subscriber(update)
        buttons = []
        for plan in self.plans.values():
            row = []
            for provider in (self.payme, self.click):
                if provider and provider.is_configured():
                    row.append(
                        InlineKeyboardButton(
                            f"{plan.title} · {provider.label}",
                            callback_data=f"buy:{plan.code}:{provider.name}",
                        )
                    )
            if row:
                buttons.append(row)
        if not buttons:
            await update.effective_message.reply_text("Ни одна касса не настроена.")
            return
        await update.effective_message.reply_text(
            "Выберите тариф и способ оплаты:", reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def on_buy_click(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, _, rest = (query.data or "").partition(":")
        plan_code, _, provider_name = rest.partition(":")
        plan = self.plans.get(plan_code)
        provider = {"payme": self.payme, "click": self.click}.get(provider_name)
        if plan is None or provider is None or not provider.is_configured():
            await query.edit_message_text("Этот способ оплаты сейчас недоступен.")
            return

        chat_id = update.effective_chat.id
        order = self.store.create_order(
            chat_id, plan.code, plan.days, plan.price, provider.name
        )
        link = provider.payment_link(order.order_id, plan)
        await query.edit_message_text(
            f"<b>{escape(plan.title)}</b> — {format_sum(plan.price)}\n"
            f"Счёт <code>{escape(order.order_id)}</code>, оплата через "
            f"{escape(provider.label)}.\n"
            "Доступ откроется сразу после подтверждения платежа.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"Оплатить через {provider.label}", url=link.url)]]
            ),
        )

    async def cmd_grant(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.settings.is_admin(chat_id):
            return
        args = context.args or []
        if len(args) != 2:
            await update.effective_message.reply_text("Формат: /grant <chat_id> <дней>")
            return
        try:
            target, days = int(args[0]), int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Оба аргумента — целые числа.")
            return
        sub = self.store.grant_access(target, days)
        await update.effective_message.reply_text(
            f"Чат {target}: доступ до {sub.paid_until:%d.%m.%Y %H:%M UTC}."
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("обработчик упал", exc_info=context.error)

    # -- фоновые задачи ---------------------------------------------------

    async def job_coupon_reminders(
        self, context: ContextTypes.DEFAULT_TYPE, today: dt.date | None = None
    ) -> None:
        """Напомнить о выплатах, до которых осталось N дней.

        ``today`` передаётся явно в тестах: подменять ``date.today``
        глобально нельзя — на этом ломается стандартный ``calendar``.
        """
        today = today or dt.date.today()
        horizon = today + dt.timedelta(days=self.settings.coupon_reminder_days)

        for sub in self.store.alerting_subscribers():
            if not self._has_access(sub) or not sub.positions:
                continue
            due = []
            for position in sub.positions:
                bond = self.catalog.get(position.isin)
                if bond is None:
                    continue
                for flow in cashflows(bond, position.quantity, since=today, until=horizon):
                    due.append((bond, flow))
            if not due:
                continue

            key = f"coupons|{horizon.isoformat()}"
            if not self.store.should_alert(
                sub.chat_id, key, self.settings.alert_cooldown_seconds
            ):
                continue

            lines = ["<b>Скоро выплаты</b>", ""]
            total = Decimal(0)
            for bond, flow in sorted(due, key=lambda pair: pair[1].date):
                label = "погашение" if flow.is_redemption else "купон"
                lines.append(
                    f"💵 {format_date(flow.date)} — <b>{format_sum(flow.amount)}</b> "
                    f"<i>{label}, {escape(bond.ticker)}</i>"
                )
                total += flow.amount
            lines += ["", f"итого: <b>{format_sum(total)}</b>", "", DISCLAIMER]

            try:
                await context.bot.send_message(
                    sub.chat_id, "\n".join(lines), parse_mode=ParseMode.HTML
                )
            except Exception:
                log.exception("не доставил напоминание чату %s", sub.chat_id)
                continue
            self.store.mark_alerted(sub.chat_id, key)

    async def _announce_payment(self, chat_id: int, plan: Plan, order: Order) -> None:
        """Вызывается маршрутом оплаты, когда деньги подтверждены."""
        if self._application is None:  # pragma: no cover
            return
        sub = self.store.get(chat_id)
        left = sub.access_days_left if sub else plan.days
        await self._application.bot.send_message(
            chat_id,
            f"✅ Оплата получена — <b>{escape(plan.title)}</b> активен.\n"
            f"Осталось {plural_days(left)}. /bonds — к выпускам.",
            parse_mode=ParseMode.HTML,
        )


def run_bot(settings: Settings) -> None:
    UzInvestBot(settings).run()
