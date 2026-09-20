import asyncio
import html
import os
import time
from contextlib import suppress
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from database import Database


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

GAME_CHAT_ID = int(os.getenv("GAME_CHAT_ID", "0"))

TIMER_UPDATE_SECONDS = int(
    os.getenv("TIMER_UPDATE_SECONDS", "20")
)

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_IDS")

if not GAME_CHAT_ID:
    raise RuntimeError("Не задан GAME_CHAT_ID")


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

dp = Dispatcher(
    storage=MemoryStorage()
)

router = Router()
dp.include_router(router)

db = Database()

game_lock = asyncio.Lock()
timer_task: Optional[asyncio.Task] = None

admin_cache = {}
admin_cache_time = 0


# ============================================================
# FSM
# ============================================================

class NewGame(StatesGroup):
    duration = State()
    custom_duration = State()
    prize = State()
    description = State()
    preview = State()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))

    minutes = seconds // 60
    secs = seconds % 60

    return f"{minutes:02d}:{secs:02d}"


def progress_bar(
    remaining: int,
    total: int,
    length: int = 10,
) -> str:

    if total <= 0:
        filled = 0
    else:
        ratio = remaining / total
        filled = round(length * ratio)

    filled = max(0, min(length, filled))

    return "▰" * filled + "▱" * (length - filled)


def user_link(
    user_id: int,
    name: str,
    username: Optional[str] = None,
) -> str:

    safe_name = html.escape(name)

    if username:
        return f"@{html.escape(username)}"

    return (
        f'<a href="tg://user?id={user_id}">'
        f"{safe_name}"
        f"</a>"
    )


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None

    import re

    match = re.search(
        r'https?://[^\s<>"\']+',
        text,
    )

    if match:
        return match.group(0)

    return None


def get_message_html(message: Message) -> str:
    """
    Сохраняет Telegram-форматирование:
    жирный, курсив, ссылки, custom emoji и т.д.
    """

    try:
        value = message.html_text

        if value:
            return value

    except Exception:
        pass

    return html.escape(
        message.text
        or message.caption
        or ""
    )


def get_message_url(message: Message) -> Optional[str]:
    text = (
        message.text
        or message.caption
        or ""
    )

    return extract_url(text)


def get_remaining(game: dict) -> int:
    remaining = int(
        game.get("remaining") or 0
    )

    if game["state"] != "active":
        return max(0, remaining)

    started_at = game.get("started_at")

    if not started_at:
        return max(0, remaining)

    elapsed = time.time() - float(started_at)

    return max(
        0,
        int(remaining - elapsed)
    )


async def is_group_admin(user_id: int) -> bool:

    global admin_cache
    global admin_cache_time

    now = time.time()

    if now - admin_cache_time > 60:
        try:
            admins = await bot.get_chat_administrators(
                GAME_CHAT_ID
            )

            admin_cache = {
                admin.user.id
                for admin in admins
            }

            admin_cache_time = now

        except Exception:
            return False

    return user_id in admin_cache


async def edit_event_message(
    game: dict,
):
    message_id = game.get("timer_message_id")

    if not message_id:
        return

    try:
        await bot.edit_message_text(
            chat_id=GAME_CHAT_ID,
            message_id=message_id,
            text=build_event_message(game),
            parse_mode=ParseMode.HTML,
            link_preview_options=link_preview(
                game.get("preview_url")
            ),
        )

    except TelegramBadRequest:
        pass

    except Exception:
        pass


def link_preview(
    url: Optional[str],
) -> LinkPreviewOptions:

    if not url:
        return LinkPreviewOptions(
            is_disabled=True
        )

    return LinkPreviewOptions(
        is_disabled=False,
        url=url,
        prefer_large_media=True,
        show_above_text=True,
    )


def build_event_message(game: dict) -> str:

    duration = int(game["duration"])

    remaining = get_remaining(game)

    state = game["state"]

    if game.get("leader_id"):
        leader = user_link(
            game["leader_id"],
            game.get("leader_name") or "Игрок",
            game.get("leader_username"),
        )
    else:
        leader = "—"

    if state == "waiting":
        timer_text = (
            "⏳ Ожидаем первого перебива..."
        )

    elif state == "paused":
        timer_text = (
            f"⏸ ПАУЗА\n"
            f"Осталось: <b>{format_time(remaining)}</b>"
        )

    elif state == "active":
        timer_text = (
            f"⏳ <b>{format_time(remaining)}</b> "
            f"{progress_bar(remaining, duration)}"
        )

    else:
        timer_text = "Игра завершена"

    prize = game.get("prize_html") or "—"

    description = (
        game.get("description_html") or ""
    )

    text = (
        "🏁 <b>ИВЕНТ НА ПЕРЕБИВ</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"🎁 <b>Приз</b> → {prize}\n\n"
        f"⏱ <b>Длительность</b> → "
        f"{format_time(duration)}\n\n"
        "✏️ <i>Любое сообщение в чате "
        "перебивает лидера и обнуляет таймер.</i>\n\n"
        "🚫 <i>Сообщения админов чата "
        "не засчитываются.</i>\n"
    )

    if description:
        text += (
            "\n━━━━━━━━━━━━━━\n\n"
            f"{description}\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━\n\n"
        f"👑 <b>Текущий лидер:</b> {leader}\n"
        f"{timer_text}"
    )

    return text


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_menu() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Новая игра",
                    callback_data="new_game",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏸ Пауза",
                    callback_data="pause_game",
                ),
                InlineKeyboardButton(
                    text="▶️ Возобновить",
                    callback_data="resume_game",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏹ Завершить игру",
                    callback_data="stop_game",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус",
                    callback_data="status",
                ),
                InlineKeyboardButton(
                    text="🏆 Победители",
                    callback_data="winners",
                ),
            ],
        ]
    )


def duration_menu() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5 минут",
                    callback_data="duration:300",
                ),
                InlineKeyboardButton(
                    text="10 минут",
                    callback_data="duration:600",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="15 минут",
                    callback_data="duration:900",
                ),
                InlineKeyboardButton(
                    text="30 минут",
                    callback_data="duration:1800",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Своя длительность",
                    callback_data="custom_duration",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                )
            ],
        ]
    )


def skip_menu() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data="skip",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                )
            ],
        ]
    )


def confirm_menu() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Запустить",
                    callback_data="confirm_start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить приз",
                    callback_data="edit_prize",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                )
            ],
        ]
    )


# ============================================================
# /START
# ============================================================

@router.message(
    Command("start"),
    F.chat.type == ChatType.PRIVATE,
)
async def cmd_start(
    message: Message,
    state: FSMContext,
):

    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            "⛔ У тебя нет доступа к управлению этим ботом."
        )
        return

    await state.clear()

    await message.answer(
        "👑 <b>Панель управления ивентами</b>\n\n"
        "Выбери действие:",
        reply_markup=main_menu(),
    )


@router.message(
    Command("menu"),
    F.chat.type == ChatType.PRIVATE,
)
async def cmd_menu(
    message: Message,
    state: FSMContext,
):

    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await message.answer(
        "👑 <b>Панель управления</b>",
        reply_markup=main_menu(),
    )


# ============================================================
# /ID — ТОЛЬКО ДЛЯ ГРУППЫ
# ============================================================

@router.message(
    Command("id"),
)
async def cmd_id(message: Message):

    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            f"Твой Telegram ID:\n"
            f"<code>{message.from_user.id}</code>"
        )
        return

    await message.answer(
        "🆔 <b>ID этого чата:</b>\n"
        f"<code>{message.chat.id}</code>"
    )


# ============================================================
# НОВАЯ ИГРА
# ============================================================

@router.callback_query(
    F.data == "new_game"
)
async def new_game(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    game = db.get_game()

    if game:
        await callback.answer(
            "Сначала заверши текущую игру.",
            show_alert=True,
        )
        return

    await state.set_state(
        NewGame.duration
    )

    await callback.message.answer(
        "⏱ <b>Выбери длительность игры:</b>",
        reply_markup=duration_menu(),
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("duration:")
)
async def select_duration(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    seconds = int(
        callback.data.split(":")[1]
    )

    await state.update_data(
        duration=seconds
    )

    await state.set_state(
        NewGame.prize
    )

    await callback.message.answer(
        "🎁 <b>Отправь приз.</b>\n\n"
        "Можно отправить текст, ссылку или "
        "текст со ссылкой.\n\n"
        "Форматирование Telegram "
        "сохранится.",
        reply_markup=skip_menu(),
    )

    await callback.answer()


@router.callback_query(
    F.data == "custom_duration"
)
async def custom_duration(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(
        NewGame.custom_duration
    )

    await callback.message.answer(
        "⏱ Напиши длительность в минутах.\n\n"
        "Например:\n"
        "<code>7</code>"
    )

    await callback.answer()


@router.message(
    NewGame.custom_duration,
    F.chat.type == ChatType.PRIVATE,
)
async def receive_custom_duration(
    message: Message,
    state: FSMContext,
):

    try:
        minutes = int(
            message.text.strip()
        )

        if minutes <= 0 or minutes > 1440:
            raise ValueError

    except Exception:
        await message.answer(
            "❌ Введи количество минут от 1 до 1440."
        )
        return

    await state.update_data(
        duration=minutes * 60
    )

    await state.set_state(
        NewGame.prize
    )

    await message.answer(
        "🎁 <b>Теперь отправь приз.</b>\n\n"
        "Можно отправить ссылку, текст или "
        "текст со ссылкой.",
        reply_markup=skip_menu(),
    )


# ============================================================
# ПРИЗ
# ============================================================

@router.message(
    NewGame.prize,
    F.chat.type == ChatType.PRIVATE,
)
async def receive_prize(
    message: Message,
    state: FSMContext,
):

    prize_html = get_message_html(message)

    preview_url = get_message_url(message)

    await state.update_data(
        prize_html=prize_html,
        preview_url=preview_url,
    )

    await state.set_state(
        NewGame.description
    )

    await message.answer(
        "✏️ <b>Теперь можешь добавить описание "
        "к ивенту.</b>\n\n"
        "Можно использовать жирный, курсив, "
        "ссылки и Telegram custom emoji.",
        reply_markup=skip_menu(),
    )


@router.callback_query(
    NewGame.prize,
    F.data == "skip"
)
async def skip_prize(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.update_data(
        prize_html="—",
        preview_url=None,
    )

    await state.set_state(
        NewGame.description
    )

    await callback.message.answer(
        "✏️ <b>Добавь описание к ивенту.</b>\n\n"
        "Или нажми «Пропустить».",
        reply_markup=skip_menu(),
    )

    await callback.answer()


# ============================================================
# ОПИСАНИЕ
# ============================================================

@router.message(
    NewGame.description,
    F.chat.type == ChatType.PRIVATE,
)
async def receive_description(
    message: Message,
    state: FSMContext,
):

    description_html = get_message_html(message)

    await state.update_data(
        description_html=description_html
    )

    await show_preview(
        message,
        state,
    )


@router.callback_query(
    NewGame.description,
    F.data == "skip"
)
async def skip_description(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.update_data(
        description_html=""
    )

    await show_preview(
        callback.message,
        state,
    )

    await callback.answer()


async def show_preview(
    message: Message,
    state: FSMContext,
):

    data = await state.get_data()

    duration = data["duration"]

    preview_game = {
        "duration": duration,
        "remaining": duration,
        "state": "waiting",
        "leader_id": None,
        "leader_name": None,
        "leader_username": None,
        "prize_html": data.get(
            "prize_html",
            "—",
        ),
        "description_html": data.get(
            "description_html",
            "",
        ),
        "preview_url": data.get(
            "preview_url"
        ),
    }

    await state.set_state(
        NewGame.preview
    )

    await message.answer(
        "👀 <b>Предпросмотр ивента:</b>\n\n"
        + build_event_message(preview_game),
        reply_markup=confirm_menu(),
        link_preview_options=link_preview(
            data.get("preview_url")
        ),
    )


# ============================================================
# ПОДТВЕРЖДЕНИЕ
# ============================================================

@router.callback_query(
    F.data == "confirm_start"
)
async def confirm_start(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()

    duration = int(
        data["duration"]
    )

    prize_html = data.get(
        "prize_html",
        "—",
    )

    description_html = data.get(
        "description_html",
        "",
    )

    preview_url = data.get(
        "preview_url"
    )

    async with game_lock:

        existing = db.get_game()

        if existing:
            await callback.answer(
                "Игра уже запущена.",
                show_alert=True,
            )
            return

        db.create_game(
            chat_id=GAME_CHAT_ID,
            duration=duration,
            prize_html=prize_html,
            description_html=description_html,
            preview_url=preview_url,
        )

        game = db.get_game()

        try:
            sent = await bot.send_message(
                chat_id=GAME_CHAT_ID,
                text=build_event_message(game),
                parse_mode=ParseMode.HTML,
                link_preview_options=link_preview(
                    preview_url
                ),
            )

        except Exception:
            db.clear_game()

            await callback.answer(
                "Не удалось отправить игру в группу.",
                show_alert=True,
            )
            return

        db.set_timer_message(
            sent.message_id
        )

    await state.clear()

    await callback.message.answer(
        "🚀 <b>Игра запущена!</b>\n\n"
        "Теперь первый обычный участник, "
        "который отправит сообщение в группе, "
        "станет лидером.",
        reply_markup=main_menu(),
    )

    await callback.answer()

    await restart_timer()


# ============================================================
# ИЗМЕНЕНИЕ ПРИЗА
# ============================================================

@router.callback_query(
    F.data == "edit_prize"
)
async def edit_prize(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(
        NewGame.prize
    )

    await callback.message.answer(
        "🎁 Отправь новый приз.",
        reply_markup=skip_menu(),
    )

    await callback.answer()


# ============================================================
# ПАУЗА
# ============================================================

@router.callback_query(
    F.data == "pause_game"
)
async def pause_game(
    callback: CallbackQuery,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    async with game_lock:

        game = db.get_game()

        if not game:
            await callback.answer(
                "Сейчас нет активной игры.",
                show_alert=True,
            )
            return

        if game["state"] != "active":
            await callback.answer(
                "Игра сейчас не запущена.",
                show_alert=True,
            )
            return

        remaining = get_remaining(game)

        if remaining <= 0:
            await callback.answer(
                "Игра уже закончилась.",
                show_alert=True,
            )
            return

        db.pause(remaining)

        game = db.get_game()

        await edit_event_message(game)

    await cancel_timer()

    await callback.answer(
        "Игра поставлена на паузу."
    )


# ============================================================
# ВОЗОБНОВЛЕНИЕ
# ============================================================

@router.callback_query(
    F.data == "resume_game"
)
async def resume_game(
    callback: CallbackQuery,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    async with game_lock:

        game = db.get_game()

        if not game:
            await callback.answer(
                "Нет игры.",
                show_alert=True,
            )
            return

        if game["state"] != "paused":
            await callback.answer(
                "Игра не находится на паузе.",
                show_alert=True,
            )
            return

        remaining = int(
            game["remaining"]
        )

        if remaining <= 0:
            await callback.answer(
                "Время уже закончилось.",
                show_alert=True,
            )
            return

        db.resume(remaining)

        game = db.get_game()

        await edit_event_message(game)

    await callback.answer(
        "▶️ Игра продолжена."
    )

    await restart_timer()


# ============================================================
# ОСТАНОВКА
# ============================================================

@router.callback_query(
    F.data == "stop_game"
)
async def stop_game(
    callback: CallbackQuery,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    async with game_lock:

        game = db.get_game()

        if not game:
            await callback.answer(
                "Нет активной игры.",
                show_alert=True,
            )
            return

        message_id = game.get(
            "timer_message_id"
        )

        await cancel_timer()

        db.clear_game()

        if message_id:
            try:
                await bot.edit_message_text(
                    chat_id=GAME_CHAT_ID,
                    message_id=message_id,
                    text=(
                        "⏹ <b>ИГРА ЗАВЕРШЕНА</b>\n\n"
                        "Ивент был остановлен "
                        "администратором."
                    ),
                )

            except Exception:
                pass

    await callback.answer(
        "Игра завершена."
    )


# ============================================================
# СТАТУС
# ============================================================

@router.callback_query(
    F.data == "status"
)
async def status(
    callback: CallbackQuery,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    game = db.get_game()

    if not game:
        await callback.message.answer(
            "📊 <b>Статус</b>\n\n"
            "Сейчас игра не запущена."
        )

        await callback.answer()
        return

    remaining = get_remaining(game)

    if game.get("leader_id"):
        leader = user_link(
            game["leader_id"],
            game["leader_name"],
            game.get("leader_username"),
        )
    else:
        leader = "Пока нет"

    await callback.message.answer(
        "📊 <b>Статус игры</b>\n\n"
        f"Состояние: <b>{html.escape(game['state'])}</b>\n"
        f"Лидер: {leader}\n"
        f"Осталось: <b>{format_time(remaining)}</b>\n"
        f"Полная длительность: "
        f"<b>{format_time(game['duration'])}</b>"
    )

    await callback.answer()


# ============================================================
# ПОБЕДИТЕЛИ
# ============================================================

@router.callback_query(
    F.data == "winners"
)
async def winners(
    callback: CallbackQuery,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    rows = db.get_winners(
        GAME_CHAT_ID,
        10,
    )

    if not rows:
        await callback.message.answer(
            "🏆 Победителей пока нет."
        )

        await callback.answer()
        return

    lines = [
        "🏆 <b>Последние победители</b>\n"
    ]

    for index, row in enumerate(
        rows,
        start=1,
    ):

        name = html.escape(
            row["name"]
        )

        if row.get("username"):
            name = (
                f"@"
                f"{html.escape(row['username'])}"
            )

        duration = format_time(
            row["duration"]
        )

        lines.append(
            f"{index}. {name} — "
            f"{duration}"
        )

    await callback.message.answer(
        "\n".join(lines)
    )

    await callback.answer()


# ============================================================
# ОТМЕНА FSM
# ============================================================

@router.callback_query(
    F.data == "cancel"
)
async def cancel(
    callback: CallbackQuery,
    state: FSMContext,
):

    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await callback.message.answer(
        "❌ Создание игры отменено.",
        reply_markup=main_menu(),
    )

    await callback.answer()


# ============================================================
# ПЕРЕБИВ В ГРУППЕ
# ============================================================

@router.message(
    F.chat.id == GAME_CHAT_ID,
)
async def group_message(
    message: Message,
):

    if not message.from_user:
        return

    # Сообщения ботов игнорируем.
    if message.from_user.is_bot:
        return

    game = db.get_game()

    if not game:
        return

    if game["state"] != "active":
        return

    # Администраторы группы не перебивают лидера.
    if await is_group_admin(
        message.from_user.id
    ):
        return

    async with game_lock:

        # Повторно читаем состояние после
        # получения блокировки.
        game = db.get_game()

        if not game:
            return

        if game["state"] != "active":
            return

        remaining = get_remaining(game)

        if remaining <= 0:
            return

        duration = int(
            game["duration"]
        )

        user = message.from_user

        db.set_leader(
            user_id=user.id,
            name=user.full_name,
            username=user.username,
            duration=duration,
        )

        game = db.get_game()

        # Ответ реплаем на сообщение перебившего.
        await message.reply(
            "📢 <b>Перебито!</b>\n"
            f"👑 Новый лидер: "
            f"{user_link(user.id, user.full_name, user.username)}\n"
            f"⏱ До победы: "
            f"<b>{format_time(duration)}</b>"
        )

        await edit_event_message(game)

    await restart_timer()


# ============================================================
# ТАЙМЕР
# ============================================================

async def cancel_timer():

    global timer_task

    if timer_task and not timer_task.done():

        timer_task.cancel()

        with suppress(
            asyncio.CancelledError
        ):
            await timer_task

    timer_task = None


async def restart_timer():

    global timer_task

    await cancel_timer()

    game = db.get_game()

    if not game:
        return

    if game["state"] != "active":
        return

    timer_task = asyncio.create_task(
        timer_loop()
    )


async def timer_loop():

    while True:

        await asyncio.sleep(
            TIMER_UPDATE_SECONDS
        )

        async with game_lock:

            game = db.get_game()

            if not game:
                return

            if game["state"] != "active":
                return

            remaining = get_remaining(game)

            if remaining <= 0:

                await finish_winner(
                    game
                )

                return

            # ВАЖНО:
            # сохраняем актуальное оставшееся время
            # и начинаем новый отсчёт от текущего момента.
            db.set_remaining(
                remaining
            )

            game = db.get_game()

            await edit_event_message(
                game
            )


async def finish_winner(
    game: dict,
):

    leader_id = game.get(
        "leader_id"
    )

    if not leader_id:
        db.clear_game()
        return

    name = game.get(
        "leader_name"
    ) or "Игрок"

    username = game.get(
        "leader_username"
    )

    duration = int(
        game["duration"]
    )

    prize_html = game.get(
        "prize_html"
    ) or "—"

    winner = user_link(
        leader_id,
        name,
        username,
    )

    await bot.send_message(
        chat_id=GAME_CHAT_ID,
        text=(
            "🏆 <b>ПОБЕДИТЕЛЬ!</b>\n\n"
            f"👑 {winner}\n"
            f"⏱ Продержался "
            f"<b>{format_time(duration)}</b> "
            f"без перебива!"
        ),
    )

    db.add_winner(
        chat_id=GAME_CHAT_ID,
        user_id=leader_id,
        name=name,
        username=username,
        prize_html=prize_html,
        duration=duration,
    )

    message_id = game.get(
        "timer_message_id"
    )

    if message_id:

        try:
            await bot.edit_message_text(
                chat_id=GAME_CHAT_ID,
                message_id=message_id,
                text=(
                    "🏆 <b>ИГРА ЗАВЕРШЕНА</b>\n\n"
                    f"👑 Победитель: {winner}\n"
                    f"⏱ Время: "
                    f"<b>{format_time(duration)}</b>"
                ),
            )

        except Exception:
            pass

    db.clear_game()


# ============================================================
# ВОССТАНОВЛЕНИЕ ПОСЛЕ ПЕРЕЗАПУСКА
# ============================================================

async def restore_game():

    async with game_lock:

        game = db.get_game()

        if not game:
            return

        if game["state"] == "active":

            remaining = get_remaining(
                game
            )

            if remaining <= 0:

                await finish_winner(
                    game
                )

                return

            # Сохраняем оставшееся время
            # и начинаем новый отсчёт сейчас.
            db.set_remaining(
                remaining
            )

            game = db.get_game()

            await edit_event_message(
                game
            )

        elif game["state"] in (
            "waiting",
            "paused",
        ):

            await edit_event_message(
                game
            )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    await restore_game()

    print(
        "Бот запущен."
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
