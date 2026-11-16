import asyncio
import html
import logging
import random
import re
import aiosqlite
import httpx

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"
DB_PATH = "prices.db"
CHECK_INTERVAL_MINUTES = 15

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class TrackState(StatesGroup):
    waiting_for_price = State()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                nm_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                target_price INTEGER NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.commit()


def extract_wb_id(text: str) -> int | None:
    match = re.search(r"wildberries\.ru/catalog/(\d+)", text)
    return int(match.group(1)) if match else None


async def get_wb_product(nm_id: int) -> tuple[str, int] | None:
    url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={nm_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        product = products[0]
        name = product.get("name", f"Артикул {nm_id}")
        for size in product.get("sizes", []):
            price_data = size.get("price")
            if price_data and "product" in price_data:
                return name, price_data["product"] // 100
    return None


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Отправьте ссылку на товар с Wildberries (можно нажать «Поделиться» прямо из приложения WB), "
        "и я сообщу, когда цена упадёт до нужной отметки.\n\n"
        "Список отслеживаемых товаров: /list"
    )


@dp.message(Command("list"))
async def cmd_list(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, target_price, url FROM items WHERE user_id = ? AND is_active = 1",
            (message.from_user.id,),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("Активных отслеживаний пока нет. Просто пришлите ссылку на товар.")
        return

    lines = [
        f"• <a href='{url}'>{html.escape(name)}</a> — ждём <b>{price} ₽</b>"
        for name, price, url in rows
    ]
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(F.text.contains("wildberries.ru/catalog/"))
async def handle_url(message: Message, state: FSMContext):
    nm_id = extract_wb_id(message.text or "")
    if not nm_id:
        await message.answer("Не удалось распознать артикул в ссылке. Проверьте формат URL.")
        return

    clean_url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    wait_msg = await message.answer("Проверяю карточку товара...")
    product = await get_wb_product(nm_id)
    if not product:
        await wait_msg.edit_text("Товар не найден или сейчас нет в наличии.")
        return

    name, current_price = product
    await state.update_data(nm_id=nm_id, name=name, url=clean_url, current_price=current_price)
    await state.set_state(TrackState.waiting_for_price)

    await wait_msg.edit_text(
        f"<b>{html.escape(name)}</b>\n"
        f"Текущая цена: <b>{current_price} ₽</b>\n\n"
        f"Напишите желаемую цену в рублях (только число):",
        parse_mode="HTML",
    )


@dp.message(TrackState.waiting_for_price)
async def handle_target_price(message: Message, state: FSMContext):
    digits = re.sub(r"\D", "", message.text or "")
    if not digits or int(digits) <= 0:
        await message.answer("Введите корректную цену числом, например: 2500")
        return

    target_price = int(digits)
    data = await state.get_data()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO items (user_id, nm_id, name, url, target_price) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, data["nm_id"], data["name"], data["url"], target_price),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Готово! Отслеживаю <b>{html.escape(data['name'])}</b>.\n"
        f"Сообщу, как только цена опустится до <b>{target_price} ₽</b> или ниже.",
        parse_mode="HTML",
    )


async def check_prices():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, nm_id, name, url, target_price FROM items WHERE is_active = 1"
        ) as cursor:
            items = await cursor.fetchall()

        for item_id, user_id, nm_id, name, url, target_price in items:
            await asyncio.sleep(random.uniform(1.5, 4.0))
            product = await get_wb_product(nm_id)
            if not product:
                continue

            _, current_price = product
            if current_price <= target_price:
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🔥 <b>Цена снизилась!</b>\n\n"
                            f"<a href='{url}'>{html.escape(name)}</a>\n"
                            f"Текущая цена: <b>{current_price} ₽</b> (ваша цель: {target_price} ₽)"
                        ),
                        parse_mode="HTML",
                    )
                    await db.execute("UPDATE items SET is_active = 0 WHERE id = ?", (item_id,))
                    await db.commit()
                except Exception as e:
                    logging.error(f"Ошибка отправки пользователю {user_id}: {e}")


async def main():
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
