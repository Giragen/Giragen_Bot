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

@dp.message(Command("/start"))
async def send_welcome(message: types.Message):
    # Reply-кнопки (внизу экрана)
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

    # Inline-кнопки (под сообщением)
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📺 YouTube", callback_data="inline_youtube")],
            [InlineKeyboardButton(text="🎮 Twitch", callback_data="inline_twitch")],
            [InlineKeyboardButton(text="📹 VKLive", callback_data="inline_vklive")]
        ]
    )

    # Сначала отправляем сообщение с inline-кнопками
    await message.answer(
        "Привет! Я бот от Giragen.\nПокажу ссылки на наши каналы:",
        reply_markup=inline_kb
    )

    # Затем прикрепляем reply-клавиатуру
    await message.answer(
        "Выберите платформу:",
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

# Обработчики для inline-кнопок
@dp.callback_query(F.data == "inline_youtube")
async def inline_youtube(callback: types.CallbackQuery):
    await callback.answer()
    youtube_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти на YouTube", url="https://www.youtube.com/@giragen")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")]
        ]
    )
    await callback.message.edit_text(
        "📺 Мой YouTube канал:",
        reply_markup=youtube_kb
    )

@dp.callback_query(F.data == "inline_twitch")
async def inline_twitch(callback: types.CallbackQuery):
    await callback.answer()
    twitch_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти на Twitch", url="https://www.twitch.tv/giragen")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")]
        ]
    )
    await callback.message.edit_text(
        "🎮 Мой Twitch канал:",
        reply_markup=twitch_kb
    )

@dp.callback_query(F.data == "inline_vklive")
async def inline_vklive(callback: types.CallbackQuery):
    await callback.answer()
    vklive_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти на VKLive", url="https://live.vkvideo.ru/giragen")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")]
        ]
    )
    await callback.message.edit_text(
        "📹 Мой VKLive канал:",
        reply_markup=vklive_kb
    )

@dp.callback_query(F.data == "inline_specs")
async def inline_specs(callback: types.CallbackQuery):
    await callback.answer()
    specs_text = """
    🖥️ Мои комплектующие:
    • Процессор: Intel i5-7400
    • Видеокарта: NVIDIA GTX 1050 Ti (4GB)
    • Оперативная память: 16GB DDR4
    • Накопитель: SSD 256GB + HDD 1TB
    """
    specs_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")]
        ]
    )
    await callback.message.edit_text(
        specs_text,
        reply_markup=specs_kb
    )

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.answer()
    menu_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📺 YouTube", callback_data="inline_youtube")],
            [InlineKeyboardButton(text="🎮 Twitch", callback_data="inline_twitch")],
            [InlineKeyboardButton(text="📹 VKLive", callback_data="inline_vklive")],
            [InlineKeyboardButton(text="🖥️ Комплектующие", callback_data="inline_specs")]
        ]
    )
    await callback.message.edit_text(
        "Привет! Я бот от Giragen. Выберите опцию:",
        reply_markup=menu_kb
    )

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())