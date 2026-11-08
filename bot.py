import asyncio
import hashlib
import html
import json
import os
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
)

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
DATA_FILE = "offers_data.json"
TTL = 24 * 3600  # срок жизни оффера, сек

CURRENCIES = {"stars": "⭐", "ton": "TON"}
# ссылка + цена + (необязательно) валюта, например: https://t.me/nft/CupidCharm-22645 1200 stars
QUERY_RE = re.compile(
    r"(?:https?://)?t\.me/nft/([\w\-]+)\s+(\d+(?:[.,]\d+)?)\s*(stars?|ton)?\s*$",
    re.I,
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

offers: dict[str, dict] = {}


def load_data():
    global offers
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                offers = json.load(f)
        except Exception:
            offers = {}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)


load_data()


def gift_label(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    name, _, num = slug.rpartition("-")
    return f"{name} #{num}" if name else slug


def user_link(user_id: int, username: str | None) -> str:
    return f"https://t.me/{username}" if username else f"tg://user?id={user_id}"


def time_left(offer: dict) -> str:
    sec = max(0, int(offer["expires_at"] - time.time()))
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def card_text(offer: dict) -> str:
    sym = CURRENCIES[offer["currency"]]
    return (
        f"<b>{html.escape(offer['owner_name'])} готов(а) дать "
        f"{html.escape(offer['price'])} {sym} за "
        f"<a href=\"{offer['gift']}\">{html.escape(gift_label(offer['gift']))}</a>.</b>\n\n"
        f"Действует ещё {time_left(offer)}."
    )


def offer_kb(offer_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ПОКАЗАТЬ ПОДАРОК", url=url)],
        [
            InlineKeyboardButton(text="Принять", callback_data=f"acc:{offer_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"decl:{offer_id}"),
        ],
    ])


@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(m: Message):
    me = await bot.get_me()
    await m.answer(
        "🤖 <b>Бот-генератор офферов</b>\n\n"
        "В любом чате напишите:\n"
        f"<code>@{me.username} https://t.me/nft/CupidCharm-22645 1200 stars</code>\n\n"
        "Появится карточка оффера — нажмите на неё, и она отправится в чат "
        "с кнопками «Принять» и «Отклонить».",
        parse_mode=ParseMode.HTML,
    )


@dp.inline_query()
async def on_inline(q: InlineQuery):
    m = QUERY_RE.search(q.query.strip())
    if not m:
        hint = InlineQueryResultArticle(
            id="help",
            title="Формат: ссылка цена валюта",
            description="https://t.me/nft/CupidCharm-22645 1200 stars",
            input_message_content=InputTextMessageContent(
                message_text="Формат: ссылка_на_подарок цена stars/ton"
            ),
        )
        await q.answer([hint], cache_time=1, is_personal=True)
        return

    slug, price, cur = m.group(1), m.group(2).replace(",", "."), (m.group(3) or "stars").lower()
    currency = "ton" if cur == "ton" else "stars"
    url = f"https://t.me/nft/{slug}"

    # один и тот же запрос -> один и тот же оффер (не плодим записи на каждый символ)
    offer_id = hashlib.sha1(f"{q.from_user.id}:{slug}:{price}:{currency}".encode()).hexdigest()[:12]
    offer = offers.get(offer_id)
    if not offer or offer["status"] != "open" or offer["expires_at"] < time.time():
        offer = {
            "owner_id": q.from_user.id,
            "owner_name": q.from_user.full_name,
            "owner_username": q.from_user.username,
            "gift": url,
            "price": price,
            "currency": currency,
            "status": "open",
            "expires_at": time.time() + TTL,
        }
        offers[offer_id] = offer
        save_data()

    result = InlineQueryResultArticle(
        id=offer_id,
        title=f"Оффер: {gift_label(url)}",
        description=f"{price} {CURRENCIES[currency]}",
        input_message_content=InputTextMessageContent(
            message_text=card_text(offer),
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(url=url, is_disabled=False),
        ),
        reply_markup=offer_kb(offer_id, url),
    )
    await q.answer([result], cache_time=1, is_personal=True)


@dp.callback_query(F.data.startswith("acc:") | F.data.startswith("decl:"))
async def on_offer_action(c: CallbackQuery):
    action, offer_id = c.data.split(":", 1)
    offer = offers.get(offer_id)

    if not offer:
        await c.answer("Оффер не найден.", show_alert=True)
        return
    if offer["status"] != "open":
        await c.answer("Оффер уже обработан.", show_alert=True)
        return
    if time.time() > offer["expires_at"]:
        offer["status"] = "expired"
        save_data()
        await c.answer("Срок оффера истёк.", show_alert=True)
        return
    if c.from_user.id == offer["owner_id"]:
        await c.answer("Это твой оффер, ответить может только покупатель.", show_alert=True)
        return
    if not c.inline_message_id:
        await c.answer("Отправьте оффер через inline-режим бота.", show_alert=True)
        return

    buyer = c.from_user
    buyer_name = (
        f'<a href="{user_link(buyer.id, buyer.username)}">{html.escape(buyer.full_name)}</a>'
    )

    if action == "acc":
        offer["status"] = "accepted"
        text = f"{buyer_name} принял(а) данный оффер!"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🎁 Перейти к отправке подарка",
                url=user_link(offer["owner_id"], offer.get("owner_username")),
            )
        ]])
    else:
        offer["status"] = "declined"
        text = f"{buyer_name} отклонил(а) данный оффер."
        kb = None
    save_data()

    try:
        await bot.edit_message_text(
            text=text,
            inline_message_id=c.inline_message_id,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=kb,
        )
    except TelegramBadRequest as e:
        print("edit error:", e)

    await c.answer()


async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
