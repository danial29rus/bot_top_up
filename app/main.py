from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from html import escape
import json
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings, load_settings
from .database import Database
from .legal import payment_and_refunds, privacy_policy, service_rules, terms_of_service
from .pricing import PricingError, PricingService, Quote
from .resell import ResellClient, ResellError
from .states import PremiumOrder, StarsOrder, SteamOrder, SupportDialog

router = Router()
settings: Settings
db: Database
resell: ResellClient
pricing: PricingService


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить Steam", callback_data="new:steam")],
        [InlineKeyboardButton(text="⭐ Купить Telegram Stars", callback_data="new:stars")],
        [InlineKeyboardButton(text="✨ Telegram Premium", callback_data="new:premium")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet")],
        [InlineKeyboardButton(text="📄 Документы и правила", callback_data="documents"), InlineKeyboardButton(text="💬 Поддержка", callback_data="support")],
    ])


def cabinet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="cabinet:profile")],
        [InlineKeyboardButton(text="📦 История заказов", callback_data="cabinet:orders")],
        [InlineKeyboardButton(text="💳 История пополнений", callback_data="cabinet:topups")],
        [InlineKeyboardButton(text="💬 Мои обращения", callback_data="support:mine")],
        [InlineKeyboardButton(text="📄 Документы", callback_data="documents"), InlineKeyboardButton(text="← Главное меню", callback_data="back:main")],
    ])


def documents_menu(need_acceptance: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔒 Конфиденциальность", callback_data="doc:privacy")],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="doc:terms")],
        [InlineKeyboardButton(text="↩️ Оплата и возвраты", callback_data="doc:refunds")],
        [InlineKeyboardButton(text="🛡 Правила сервиса", callback_data="doc:rules")],
    ]
    if need_acceptance:
        rows.append([InlineKeyboardButton(text="✅ Принимаю документы", callback_data="terms:accept")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currencies() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=x, callback_data=f"currency:{x}") for x in ("RUB", "USD")],
        [InlineKeyboardButton(text="KZT", callback_data="currency:KZT"), InlineKeyboardButton(text="UAH", callback_data="currency:UAH")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="back:main")],
    ])


def quantity_buttons(kind: str, values: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=str(v), callback_data=f"{kind}:{v}") for v in values[i:i + 3]] for i in range(0, len(values), 3)]
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_reference(order: dict) -> str:
    return order.get("public_id") or f"#{order['id']}"


def order_text(order: dict) -> str:
    names = {"steam": "Steam", "stars": "Telegram Stars", "premium": "Telegram Premium"}
    statuses = {
        "awaiting_price": "ожидает цены",
        "awaiting_payment": "ожидает оплаты",
        "payment_review": "проверяем оплату",
        "creating": "создаётся у поставщика",
        "created": "принят поставщиком",
        "processing": "выполняется",
        "completed": "выполнен",
        "failed": "не выполнен",
        "refund": "возврат у поставщика",
        "supplier_error": "требует проверки оператором",
    }
    if order.get("price_rub"):
        price = f"{int(order['price_rub']):,} ₽".replace(",", " ")
    elif order["price_usd"]:
        price = f"${order['price_usd']}"
    else:
        price = "уточняется администратором"
    reference = order_reference(order)
    return f"Заявка {reference}\nУслуга: {names[order['product']]}\nК оплате: {price}\nСтатус: {statuses.get(order['status'], order['status'])}\nСоздана: {order['created_at']} UTC"


def order_details(order: dict) -> str:
    payload = order["payload"]
    if order["product"] == "steam":
        return f"Steam: {payload['amount']} {payload['currency']}"
    if order["product"] == "stars":
        return f"Stars: {payload['quantity']} для @{payload['telegram_username']}"
    return f"Premium: {payload['months']} мес. для @{payload['telegram_username']}"


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    if settings.payment_enabled:
        rows = [[InlineKeyboardButton(text="Я оплатил(а)", callback_data=f"payment_sent:{order_id}")]]
    else:
        rows = [[InlineKeyboardButton(text="⏳ Оплата подключается", callback_data="payment:unavailable")]]
    rows.append([InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_admin(user_id: int) -> bool:
    return await db.is_admin(user_id)


async def can_order(call: CallbackQuery) -> bool:
    if await db.accepted_terms(call.from_user.id):
        return True
    await replace_view(
        call.message,
        "Перед созданием первой заявки прочитайте документы и подтвердите согласие.",
        documents_menu(need_acceptance=True),
    )
    await call.answer()
    return False


async def notify_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in await db.admin_ids():
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception:  # An administrator may have blocked the bot.
            logging.exception("Could not notify admin %s", admin_id)


def admin_order_markup(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton(text="📜 Журнал заявки", callback_data=f"audit:{order_id}")],
    ])


async def replace_view(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """One-screen navigation: callbacks edit the active card instead of creating chat noise."""
    try:
        if message.photo:
            await message.edit_caption(caption=text[:1024], reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logging.exception("Could not replace bot view")


async def send_banner(message: Message, path: str, caption: str, markup: InlineKeyboardMarkup) -> None:
    """Sends a branded card but stays usable if an asset was not mounted in deployment."""
    if Path(path).is_file():
        await message.answer_photo(FSInputFile(path), caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)


async def show_main_menu(message: Message, replace: bool = False) -> None:
    caption = "<b>NovaTop</b>\n\n<b>Больше игр. Меньше ожидания.</b>\nПополняйте кошелёк Steam по логину — пароль не нужен. Следите за каждой заявкой в личном кабинете.\n\n<i>Оплата проверяется оператором вручную.</i>"
    if replace:
        await replace_view(message, caption, menu())
        return
    await send_banner(
        message,
        settings.topup_banner_path,
        caption,
        menu(),
    )


async def show_cabinet(message: Message, user_id: int, replace: bool = False) -> None:
    stats = await db.user_stats(user_id)
    caption = f"<b>Личный кабинет</b>\n\nИстория ваших заявок всегда под рукой.\n\nВсего: <b>{stats['total']}</b> · В работе: <b>{stats['active']}</b> · Выполнено: <b>{stats['completed']}</b>"
    if replace:
        await replace_view(message, caption, cabinet_menu())
        return
    await send_banner(
        message,
        settings.cabinet_banner_path,
        caption,
        cabinet_menu(),
    )


async def create_local_order(message: Message, product: str, payload: dict, quote: Quote, customer=None, replace: bool = False) -> None:
    user = customer or message.from_user
    order_id = await db.create_order(
        user.id,
        user.username,
        product,
        payload,
        str(quote.supplier_cost_usd),
        str(quote.usd_rub_rate),
        str(quote.markup_percent),
        quote.price_rub,
        quote.source,
        "awaiting_payment",
    )
    price_line = f"Сумма к оплате: <b>{quote.price_rub:,} ₽</b>\n".replace(",", " ")
    keyboard = payment_keyboard(order_id)
    if settings.payment_enabled:
        payment_state = "<b>Статус: ⏳ Ожидает подтверждения оплаты</b>\nПосле оплаты нажмите кнопку — оператор проверит поступление."
    else:
        payment_state = "<b>Статус: ⏳ Ожидает подключения оплаты</b>\nЗаявка сохранена в личном кабинете. Приём оплаты пока не подключён."
    created_order = await db.get_order(order_id)
    text = f"Заявка {created_order['public_id']} создана.\n{price_line}\n{payment_state}"
    if replace:
        await replace_view(message, text, keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)
    await notify_admins(message.bot, f"Новая заявка\n{order_text(created_order)}\nПользователь: @{user.username or 'без username'} ({user.id})")


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.first_name)
    if not await db.accepted_terms(user.id):
        await message.answer(
            "Добро пожаловать. Перед первой заявкой ознакомьтесь с правилами сервиса, политикой конфиденциальности и условиями оплаты.",
            reply_markup=documents_menu(need_acceptance=True),
        )
        return
    await show_main_menu(message)


@router.message(Command("id"))
async def get_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.callback_query(F.data == "back:main")
async def back_to_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(call.message, replace=True)
    await call.answer()


@router.callback_query(F.data == "cabinet")
async def cabinet(call: CallbackQuery) -> None:
    await show_cabinet(call.message, call.from_user.id, replace=True)
    await call.answer()


@router.callback_query(F.data == "cabinet:profile")
async def profile(call: CallbackQuery) -> None:
    stats = await db.user_stats(call.from_user.id)
    username = f"@{call.from_user.username}" if call.from_user.username else "не указан"
    await replace_view(
        call.message,
        f"<b>Мой профиль</b>\nTelegram ID: <code>{call.from_user.id}</code>\nUsername: {username}\n\nВсего заявок: {stats['total']}\nАктивных: {stats['active']}\nВыполнено: {stats['completed']}",
        cabinet_menu(),
    )
    await call.answer()


async def show_history(message: Message, user_id: int, only_topups: bool = False, replace: bool = False) -> None:
    items = await (db.user_orders_by_product(user_id, ("steam",)) if only_topups else db.user_orders(user_id))
    title = "<b>История пополнений Steam</b>" if only_topups else "<b>История заказов</b>"
    if not items:
        if replace:
            await replace_view(message, f"{title}\n\nЗаписей пока нет.", cabinet_menu())
        else:
            await message.answer(f"{title}\n\nЗаписей пока нет.", reply_markup=cabinet_menu())
        return
    cards = "\n\n".join(f"{order_text(item)}\n{order_details(item)}" for item in items)
    if replace:
        await replace_view(message, f"{title}\n\n{cards}", cabinet_menu())
    else:
        await message.answer(f"{title}\n\n{cards}", reply_markup=cabinet_menu())


@router.callback_query(F.data == "cabinet:orders")
@router.message(Command("orders"))
async def my_orders(event: CallbackQuery | Message) -> None:
    await show_history(event.message if isinstance(event, CallbackQuery) else event, event.from_user.id, replace=isinstance(event, CallbackQuery))
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "cabinet:topups")
async def topups_history(call: CallbackQuery) -> None:
    await show_history(call.message, call.from_user.id, only_topups=True, replace=True)
    await call.answer()


@router.callback_query(F.data == "documents")
async def documents(call: CallbackQuery) -> None:
    await replace_view(call.message, "<b>Документы и правила</b>\nВыберите документ для чтения.", documents_menu(not await db.accepted_terms(call.from_user.id)))
    await call.answer()


@router.callback_query(F.data.startswith("doc:"))
async def document(call: CallbackQuery) -> None:
    documents = {
        "privacy": privacy_policy,
        "terms": terms_of_service,
        "refunds": payment_and_refunds,
        "rules": service_rules,
    }
    document_id = call.data.split(":", 1)[1]
    await replace_view(call.message, documents[document_id](settings), documents_menu(not await db.accepted_terms(call.from_user.id)))
    await call.answer()


@router.callback_query(F.data == "terms:accept")
async def accept_terms(call: CallbackQuery) -> None:
    await db.upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    await db.accept_terms(call.from_user.id)
    await show_main_menu(call.message, replace=True)
    await call.answer()


def ticket_admin_markup(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support:reply:{ticket_id}"), InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support:close:{ticket_id}")],
        [InlineKeyboardButton(text="📜 Открыть переписку", callback_data=f"ticket:view:{ticket_id}")],
    ])


def ticket_customer_markup(ticket_id: int, is_open: bool) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="📜 Открыть переписку", callback_data=f"ticket:view:{ticket_id}")]]
    if is_open:
        buttons.insert(0, [InlineKeyboardButton(text="💬 Дописать в обращение", callback_data=f"ticket:write:{ticket_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def notify_ticket_admins(bot: Bot, ticket_id: int, user_id: int, text: str) -> None:
    await notify_admins(
        bot,
        f"<b>Обращение #{ticket_id}</b>\nКлиент: <code>{user_id}</code>\n\n{escape(text)}",
        ticket_admin_markup(ticket_id),
    )


@router.callback_query(F.data == "support")
async def support_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SupportDialog.customer_message)
    await replace_view(
        call.message,
        "<b>Поддержка NovaTop</b>\nОпишите вопрос одним сообщением. Ответ придёт сюда же, от этого бота.\n\nНе отправляйте пароль Steam/Telegram, коды входа, данные карты или seed-фразы.",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]),
    )
    await call.answer()


@router.message(SupportDialog.customer_message)
async def customer_support_message(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer("Поддержка пока принимает только текстовые сообщения.")
        return
    if len(body) > 3500:
        await message.answer("Сократите сообщение до 3500 символов.")
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if ticket_id:
        ticket = await db.get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != message.from_user.id or not await db.add_ticket_message(ticket_id, message.from_user.id, "customer", body):
            await message.answer("Это обращение уже закрыто. Создайте новое через «Поддержка».")
            await state.clear()
            return
        await message.answer(f"Сообщение добавлено в обращение #{ticket_id}.")
    else:
        ticket_id = await db.create_support_ticket(message.from_user.id, body)
        await message.answer(f"Обращение #{ticket_id} создано. Ответ придёт в этот чат.", reply_markup=ticket_customer_markup(ticket_id, True))
    await notify_ticket_admins(message.bot, ticket_id, message.from_user.id, body)
    await state.clear()


@router.callback_query(F.data == "support:mine")
async def my_tickets(call: CallbackQuery) -> None:
    tickets = await db.user_tickets(call.from_user.id)
    if not tickets:
        await replace_view(call.message, "У вас нет обращений. Нажмите «Поддержка» в главном меню, чтобы создать новое.", cabinet_menu())
        await call.answer()
        return
    text = "<b>Мои обращения</b>\n\n" + "\n".join(
        f"#{ticket['id']} · {'открыто' if ticket['status'] == 'open' else 'закрыто'} · {ticket['created_at']} UTC" for ticket in tickets
    )
    buttons = [[InlineKeyboardButton(text=f"Обращение #{ticket['id']}", callback_data=f"ticket:view:{ticket['id']}")] for ticket in tickets]
    buttons.append([InlineKeyboardButton(text="← Личный кабинет", callback_data="cabinet")])
    await replace_view(call.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


async def render_ticket(message: Message, ticket_id: int, replace: bool = False) -> None:
    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        if replace:
            await replace_view(message, "Обращение не найдено.", cabinet_menu())
        else:
            await message.answer("Обращение не найдено.")
        return
    rows = [f"<b>Обращение #{ticket_id}</b> · {ticket['status']}"]
    for item in await db.ticket_messages(ticket_id):
        author = "Клиент" if item["sender_role"] == "customer" else "Поддержка NovaTop"
        rows.append(f"<b>{author}</b> · {item['created_at']} UTC\n{escape(item['body'][:700])}")
    text = "\n\n".join(rows)
    markup = ticket_customer_markup(ticket_id, ticket["status"] == "open")
    if replace:
        await replace_view(message, text, markup)
    else:
        await message.answer(text[:4000], reply_markup=markup)


@router.callback_query(F.data.startswith("ticket:view:"))
async def view_ticket(call: CallbackQuery) -> None:
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if not ticket or (ticket["user_id"] != call.from_user.id and not await is_admin(call.from_user.id)):
        await call.answer("Нет доступа", show_alert=True)
        return
    await render_ticket(call.message, ticket_id, replace=True)
    await call.answer()


@router.callback_query(F.data.startswith("ticket:write:"))
async def write_ticket(call: CallbackQuery, state: FSMContext) -> None:
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id or ticket["status"] != "open":
        await call.answer("Обращение закрыто или недоступно.", show_alert=True)
        return
    await state.set_state(SupportDialog.customer_message)
    await state.update_data(ticket_id=ticket_id)
    await replace_view(call.message, f"Напишите сообщение для обращения #{ticket_id}.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


@router.message(Command("support"))
async def admin_support(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        await message.answer("Откройте «Поддержка» в главном меню, чтобы написать оператору.")
        return
    tickets = await db.open_tickets()
    if not tickets:
        await message.answer("Открытых обращений нет.")
        return
    for ticket in tickets:
        await message.answer(
            f"<b>Обращение #{ticket['id']}</b>\nКлиент: <code>{ticket['user_id']}</code>\nСоздано: {ticket['created_at']} UTC",
            reply_markup=ticket_admin_markup(ticket["id"]),
        )


@router.callback_query(F.data == "support:admin")
async def admin_support_view(call: CallbackQuery) -> None:
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    tickets = await db.open_tickets()
    if not tickets:
        await replace_view(call.message, "Открытых обращений нет.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
        await call.answer()
        return
    text = "<b>Открытые обращения</b>\n\n" + "\n".join(f"#{ticket['id']} · клиент {ticket['user_id']}" for ticket in tickets)
    buttons = [[InlineKeyboardButton(text=f"Обращение #{ticket['id']}", callback_data=f"ticket:view:{ticket['id']}")] for ticket in tickets]
    buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="back:main")])
    await replace_view(call.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["status"] != "open":
        await call.answer("Обращение закрыто или не найдено.", show_alert=True)
        return
    await state.set_state(SupportDialog.admin_reply)
    await state.update_data(ticket_id=ticket_id)
    await replace_view(call.message, f"Введите ответ для обращения #{ticket_id}.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← К обращениям", callback_data="support:admin")]]))
    await call.answer()


@router.message(SupportDialog.admin_reply)
async def support_reply(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 3500:
        await message.answer("Нужен текст до 3500 символов.")
        return
    ticket_id = (await state.get_data()).get("ticket_id")
    ticket = await db.get_ticket(ticket_id) if ticket_id else None
    if not ticket or not await db.add_ticket_message(ticket_id, message.from_user.id, "admin", body):
        await message.answer("Обращение закрыто или не найдено.")
        await state.clear()
        return
    await message.bot.send_message(ticket["user_id"], f"<b>Ответ поддержки по обращению #{ticket_id}</b>\n\n{escape(body)}", reply_markup=ticket_customer_markup(ticket_id, True))
    await message.answer("Ответ отправлен клиенту.")
    await state.clear()


@router.callback_query(F.data.startswith("support:close:"))
async def support_close(call: CallbackQuery) -> None:
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if not ticket or not await db.close_ticket(ticket_id):
        await call.answer("Обращение уже закрыто или не найдено.", show_alert=True)
        return
    await call.bot.send_message(ticket["user_id"], f"Обращение #{ticket_id} закрыто. Если вопрос остался, создайте новое через «Поддержка».")
    await replace_view(call.message, f"Обращение #{ticket_id} закрыто.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


@router.callback_query(F.data == "new:steam")
async def steam_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await can_order(call):
        return
    await state.set_state(SteamOrder.login)
    await replace_view(call.message, "Введите логин Steam (не пароль).", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


@router.message(SteamOrder.login)
async def steam_login(message: Message, state: FSMContext) -> None:
    login = message.text.strip()
    if not login or len(login) > 100 or " " in login:
        await message.answer("Нужен корректный логин Steam без пробелов.")
        return
    try:
        valid = await resell.check_steam_login(login)
    except ResellError:
        await message.answer("Не удалось проверить логин. Попробуйте позднее.")
        return
    if not valid:
        await message.answer("Этот логин нельзя пополнить. Проверьте его и начните заново.", reply_markup=menu())
        await state.clear()
        return
    await state.update_data(login=login)
    await state.set_state(SteamOrder.currency)
    await message.answer("Выберите валюту кошелька Steam:", reply_markup=currencies())


@router.callback_query(SteamOrder.currency, F.data.startswith("currency:"))
async def steam_currency(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(currency=call.data.split(":", 1)[1])
    await state.set_state(SteamOrder.amount)
    await replace_view(call.message, "Введите сумму пополнения. Минимум — эквивалент $0.15.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


@router.message(SteamOrder.amount)
async def steam_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        await message.answer("Введите сумму числом, например: 500")
        return
    data = await state.get_data()
    try:
        quote = await pricing.steam_quote(amount, data["currency"])
    except (ResellError, PricingError):
        await message.answer("Не удалось получить курс. Попробуйте позднее.")
        return
    if amount <= 0 or quote.supplier_cost_usd < Decimal("0.15"):
        await message.answer("Сумма меньше минимальной.")
        return
    await create_local_order(message, "steam", {"steam_login": data["login"], "currency": data["currency"], "amount": str(amount)}, quote)
    await state.clear()


@router.callback_query(F.data == "new:stars")
async def stars_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await can_order(call):
        return
    await state.set_state(StarsOrder.username)
    await replace_view(call.message, "Введите username получателя Stars, например @username.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


async def receive_telegram_username(
    message: Message,
    state: FSMContext,
    next_state,
    prompt: str,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    username = message.text.strip().lstrip("@")
    if not username or len(username) > 32 or not username.replace("_", "").isalnum():
        await message.answer("Введите публичный Telegram username без ссылки.")
        return False
    await state.update_data(username=username)
    await state.set_state(next_state)
    await message.answer(prompt, reply_markup=keyboard)
    return True


@router.message(StarsOrder.username)
async def stars_username(message: Message, state: FSMContext) -> None:
    await receive_telegram_username(
        message,
        state,
        StarsOrder.quantity,
        "Выберите количество Stars:",
        quantity_buttons("stars", [50, 100, 250, 500, 1000, 2500]),
    )


@router.callback_query(StarsOrder.quantity, F.data.startswith("stars:"))
async def stars_quantity(call: CallbackQuery, state: FSMContext) -> None:
    quantity = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    try:
        quote = await pricing.stars_quote(quantity)
    except (ResellError, PricingError):
        await replace_view(call.message, "Не удалось получить актуальную цену Stars. Попробуйте позднее.", menu())
        await call.answer()
        return
    await create_local_order(call.message, "stars", {"telegram_username": data["username"], "quantity": quantity}, quote, call.from_user, replace=True)
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "new:premium")
async def premium_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await can_order(call):
        return
    await state.set_state(PremiumOrder.username)
    await replace_view(call.message, "Введите username получателя Premium, например @username.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.answer()


@router.message(PremiumOrder.username)
async def premium_username(message: Message, state: FSMContext) -> None:
    await receive_telegram_username(
        message,
        state,
        PremiumOrder.months,
        "Выберите срок Premium:",
        quantity_buttons("premium", [3, 6, 12]),
    )


@router.message(StarsOrder.quantity)
async def stars_quantity_text(message: Message) -> None:
    await message.answer("Выберите количество кнопкой ниже.", reply_markup=quantity_buttons("stars", [50, 100, 250, 500, 1000, 2500]))


@router.message(PremiumOrder.months)
async def premium_months_text(message: Message) -> None:
    await message.answer("Premium доступен на 3, 6 или 12 месяцев. Выберите срок кнопкой.", reply_markup=quantity_buttons("premium", [3, 6, 12]))


@router.callback_query(PremiumOrder.months, F.data.startswith("premium:"))
async def premium_months(call: CallbackQuery, state: FSMContext) -> None:
    months = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    try:
        quote = await pricing.premium_quote(months)
    except (ResellError, PricingError):
        await replace_view(call.message, "Не удалось получить актуальную цену Premium. Попробуйте позднее.", menu())
        await call.answer()
        return
    await create_local_order(call.message, "premium", {"telegram_username": data["username"], "months": months}, quote, call.from_user, replace=True)
    await state.clear()
    await call.answer()


@router.callback_query(F.data.startswith("payment_sent:"))
async def payment_sent(call: CallbackQuery) -> None:
    if not settings.payment_enabled:
        await call.answer("Приём оплаты ещё не подключён.", show_alert=True)
        return
    order_id = int(call.data.split(":", 1)[1])
    if not await db.mark_payment_review(order_id, call.from_user.id):
        await call.answer("Заявка уже передана в работу или недоступна.", show_alert=True)
        return
    order = await db.get_order(order_id)
    await replace_view(call.message, "Спасибо. Оператор проверит оплату и начнёт выполнение заявки.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Личный кабинет", callback_data="cabinet")]]))
    await notify_admins(call.bot, f"Проверить оплату\n{order_text(order)}\nПолучатель: {order['payload']}", admin_order_markup(order_id))
    await call.answer()


@router.callback_query(F.data == "payment:unavailable")
async def payment_unavailable(call: CallbackQuery) -> None:
    await call.answer("Приём оплаты ещё не подключён. Заявка сохранена в личном кабинете.", show_alert=True)


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    items = await db.orders_for_admin(("awaiting_price", "payment_review", "awaiting_payment"))
    if not items:
        await message.answer("Нет заявок, ожидающих оплаты.")
        return
    for item in items:
        markup = admin_order_markup(item["id"]) if item["status"] == "payment_review" else None
        await message.answer(f"{order_text(item)}\nДанные: {item['payload']}\nКлиент: {item['user_id']}", reply_markup=markup)


@router.message(Command("price"))
async def set_price(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /price НОМЕР СУММА_В_RUB")
        return
    try:
        order_id = int(parts[1])
        price = int(parts[2])
        if price <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.answer("Номер и сумма должны быть положительными числами.")
        return
    if not await db.set_price(order_id, price, message.from_user.id):
        await message.answer("Нельзя изменить цену этой заявки.")
        return
    order = await db.get_order(order_id)
    await message.bot.send_message(
        order["user_id"],
        f"Для заявки {order_reference(order)} сумма к оплате: <b>{price:,} ₽</b>.\nСтатус: ожидает подключения оплаты.".replace(",", " "),
        reply_markup=payment_keyboard(order_id),
    )
    await message.answer("Цена установлена, клиент уведомлён.")


async def show_audit(message: Message, order_id: int, replace: bool = False) -> None:
    order = await db.get_order(order_id)
    if not order:
        if replace:
            await replace_view(message, "Заявка не найдена.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
        else:
            await message.answer("Заявка не найдена.")
        return
    events = await db.audit_events(order_id)
    if not events:
        if replace:
            await replace_view(message, f"По заявке #{order_id} пока нет записей журнала.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
        else:
            await message.answer(f"По заявке #{order_id} пока нет записей журнала.")
        return
    lines = [f"<b>Журнал заявки #{order_id}</b>"]
    for event in events:
        details = json.dumps(event["details"], ensure_ascii=False, separators=(",", ":"))
        actor = f" · оператор {event['actor_user_id']}" if event["actor_user_id"] and event["actor_user_id"] != order["user_id"] else ""
        lines.append(f"{event['created_at']} UTC — <b>{event['action']}</b>{actor}\n<code>{escape(details[:400])}</code>")
    text = "\n\n".join(lines)
    if replace:
        await replace_view(message, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    else:
        await message.answer(text[:4000])


@router.message(Command("audit"))
async def audit_command(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /audit НОМЕР_ЗАЯВКИ")
        return
    await show_audit(message, int(parts[1]))


@router.callback_query(F.data.startswith("audit:"))
async def audit_callback(call: CallbackQuery) -> None:
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await show_audit(call.message, int(call.data.split(":", 1)[1]), replace=True)
    await call.answer()


@router.callback_query(F.data.startswith("paid:"))
async def confirm_payment(call: CallbackQuery) -> None:
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    order_id = int(call.data.split(":", 1)[1])
    order = await db.claim_for_creation(order_id, call.from_user.id)
    if not order:
        await call.answer("Эта заявка уже обработана.", show_alert=True)
        return
    try:
        supplier = await resell.create_order(order["product"], order["payload"])
        await db.set_supplier_result(order_id, int(supplier["number"]), supplier["status"], supplier.get("charged_usd"))
    except (ResellError, KeyError, ValueError) as exc:
        await db.set_error(order_id, str(exc))
        await replace_view(call.message, f"Заявка #{order_id}: ошибка Resell. Повторно не отправляйте автоматически: {escape(str(exc))}", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
        await call.answer()
        return
    current = await db.get_order(order_id)
    await replace_view(call.message, f"Заявка {order_reference(order)} отправлена в Resell, номер поставщика #{supplier['number']}.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="back:main")]]))
    await call.bot.send_message(order["user_id"], f"Оплата по заявке {order_reference(order)} подтверждена. Выполняем заказ; статус: {current['status']}.")
    await call.answer("Заказ отправлен")


async def poll_orders(bot: Bot) -> None:
    while True:
        try:
            for order in await db.pollable_orders():
                supplier = await resell.get_order(order["supplier_order_id"])
                new_status = supplier["status"]
                if new_status.lower() == order["status"]:
                    continue
                error = supplier.get("status_reason") or supplier.get("fail_code")
                await db.update_supplier_status(order["id"], new_status, error)
                text = f"Заявка {order_reference(order)}: статус изменён на {new_status.lower()}."
                if error:
                    text += f" Причина: {error}"
                await bot.send_message(order["user_id"], text)
                await notify_admins(bot, text)
        except Exception:
            logging.exception("Order polling failed")
        await asyncio.sleep(settings.poll_seconds)


async def main() -> None:
    global settings, db, resell, pricing
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db = Database(settings.database_path)
    await db.init()
    resell = ResellClient(settings.resell_api_key, proxy=settings.outbound_proxy)
    await resell.start()
    pricing = PricingService(resell, settings.markup_percent, settings.rub_price_rounding, proxy=settings.outbound_proxy)
    await pricing.start()
    bot = Bot(
        settings.bot_token,
        session=AiohttpSession(proxy=settings.outbound_proxy),
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    polling_task = asyncio.create_task(poll_orders(bot))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        polling_task.cancel()
        await pricing.close()
        await resell.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
