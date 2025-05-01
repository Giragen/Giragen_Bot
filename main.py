from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import Command
import asyncio
import os
from dotenv import load_dotenv
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Загружаем переменные из .env файла
load_dotenv()

# Получаем токен из переменных среды
API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    kb = [
        [types.KeyboardButton(text="YouTube")],
        [types.KeyboardButton(text="Twitch")],
        [types.KeyboardButton(text="VKLive")],
        [types.KeyboardButton(text="Комплектующие")]
    ]
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Выберите платформу"
    )
    await message.reply(
        "Привет! Я бот от Giragen.\nПокажу ссылки на наши каналы:",
        reply_markup=keyboard
    )

@dp.message(F.text.lower() == "youtube")
async def youtube_link(message: types.Message):
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="YouTube Канал", url="https://www.youtube.com/@giragen")]
        ]
    )
    await message.answer(
        "Мой YouTube канал:",
        reply_markup=inline_kb
    )

@dp.message(F.text.lower() == "twitch")
async def twitch_link(message: types.Message):
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Twitch Канал", url="https://www.twitch.tv/giragen")]
        ]
    )
    await message.answer(
        "Мой Twitch канал:",
        reply_markup=inline_kb
    )

@dp.message(F.text.lower() == "vklive")
async def vklive_link(message: types.Message):
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="VKLive Канал", url="https://live.vkvideo.ru/giragen")]  
        ]
    )
    await message.answer(
        "Мой VKLive канал:",
        reply_markup=inline_kb
    )
    
@dp.message(F.text.lower() == "комплектующие")
async def send_specs(message: types.Message):
    specs = """
    📌 Комплектующие:
    • Процессор: Intel i5-7400
    • Видеокарта: NVIDIA GTX 1050 Ti (4GB)
    • Оперативная память: 16GB DDR4
    • Накопитель: SSD 256GB + HDD 1TB
    """
    await message.answer(specs)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())