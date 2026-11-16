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
from config import BOT_TOKEN

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
    
