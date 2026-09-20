import asyncio
import html
import os
import sqlite3
import time
from contextlib import suppress
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
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


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

GAME_CHAT_ID = int(os.getenv("GAME_CHAT_ID", "0") or "0")
TIMER_UPDATE_SECONDS = int(os.getenv("TIMER_UPDATE_SECONDS", "20") or "20")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

# GAME_CHAT_ID специально НЕ проверяем.
# Пока он 0, команда /id будет работать в группе.


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

DB_FILE = "game.sqlite3"


def db_connect():
    return sqlite3.connect(DB_FILE)


def db_init():
    with db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS game (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id INTEGER DEFAULT 0,
                event_message_id INTEGER DEFAULT 0,
                state TEXT DEFAULT 'waiting',
                duration INTEGER DEFAULT 600,
                remaining REAL DEFAULT 600,
                started_at REAL,
                leader_id INTEGER,
                leader_name TEXT,
                prize TEXT DEFAULT '',
                description TEXT DEFAULT ''
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                duration INTEGER,
                won_at REAL
            )
            """
        )

        cur = con.execute("SELECT id FROM game WHERE id = 1")
        if cur.fetchone() is None:
            con.execute(
                """
                INSERT INTO game (
                    id, state, duration, remaining
                )
                VALUES (1, 'waiting', 600, 600)
                """
            )


def get_game():
    with db_connect() as con:
        cur = con.execute(
            """
            SELECT
                id,
                chat_id,
                event_message_id,
                state,
                duration,
                remaining,
                started_at,
                leader_id,
                leader_name,
                prize,
                description
            FROM game
            WHERE id = 1
            """
        )
        row = cur.fetchone()

    if not row:
        return None

    keys = [
        "id",
        "chat_id",
        "event_message_id",
        "state",
        "duration",
        "remaining",
        "started_at",
        "leader_id",
        "leader_name",
        "prize",
        "description",
    ]

    return dict(zip(keys, row))


def create_game(chat_id, duration, prize, description):
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                chat_id = ?,
                event_message_id = 0,
                state = 'waiting',
                duration = ?,
                remaining = ?,
                started_at = NULL,
                leader_id = NULL,
                leader_name = NULL,
                prize = ?,
                description = ?
            WHERE id = 1
            """,
            (
                chat_id,
                duration,
                duration,
                prize,
                description,
            ),
        )


def set_event_message(message_id):
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET event_message_id = ?
            WHERE id = 1
            """,
            (message_id,),
        )


def set_state(state):
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET state = ?
            WHERE id = 1
            """,
            (state,),
        )


def set_leader(user_id, user_name, duration):
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                state = 'active',
                remaining = ?,
                started_at = ?,
                leader_id = ?,
                leader_name = ?
            WHERE id = 1
            """,
            (
                duration,
                time.time(),
                user_id,
                user_name,
            ),
        )


def save_remaining(remaining):
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                remaining = ?,
                started_at = ?
            WHERE id = 1
            """,
            (
                remaining,
                time.time(),
            ),
        )


def pause_game():
    game = get_game()

    if not game:
        return

    remaining = get_remaining(game)

    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                state = 'paused',
                remaining = ?,
                started_at = NULL
            WHERE id = 1
            """,
            (remaining,),
        )


def resume_game():
    game = get_game()

    if not game:
        return

    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                state = 'active',
                started_at = ?
            WHERE id = 1
            """,
            (time.time(),),
        )


def clear_game():
    with db_connect() as con:
        con.execute(
            """
            UPDATE game
            SET
                chat_id = 0,
                event_message_id = 0,
                state = 'waiting',
                duration = 600,
                remaining = 600,
                started_at = NULL,
                leader_id = NULL,
                leader_name = NULL,
                prize = '',
                description = ''
            WHERE id = 1
            """
        )


def add_winner(user_id, user_name, duration):
    with db_connect() as con:
        con.execute(
            """
            INSERT INTO winners (
                user_id,
                user_name,
                duration,
                won_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                user_name,
                duration,
                time.time(),
            ),
        )


def get_winners(limit=10):
    with db_connect() as con:
        cur = con.execute(
            """
            SELECT user_name, duration, won_at
            FROM winners
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def get_remaining(game):
    if game["state"] != "active":
        return max(0, float(game["remaining"]))

    if not game["started_at"]:
        return max(0, float(game["remaining"]))

    elapsed = time.time() - float(game["started_at"])
    return max(0, float(game["remaining"]) - elapsed)


# =========================================================
# BOT
# =========================================================

router = Router()
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

game_lock = asyncio.Lock()
timer_task: Optional[asyncio.Task] = None

db_init()


# =========================================================
# FSM
# =========================================================

class NewGame(StatesGroup):
    duration = State()
    custom_duration = State()
    prize = State()
    description = State()
    preview = State()


# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def progress_bar(remaining: float, duration: int) -> str:
    if duration <= 0:
        return ""

    ratio = max(0, min(1, remaining / duration))
    filled = int(round(ratio * 10))

    return "▰" * filled + "▱" * (10 - filled)


def display_name(message: Message) -> str:
    user = message.from_user

    if not user:
        return "Участник"

    if user.username:
        return f"@{user.username}"

    return user.full_name


def get_preview_url(text: str) -> Optional[str]:
    words = text.replace("\n", " ").split()

    for word in words:
        word = word.strip("()[]<>.,!?")

        if word.startswith("https://") or word.startswith("http://"):
            return word

    return None


def event_text(game, remaining=None):
    if remaining is None:
        remaining = game["remaining"]

    duration = game["duration"]

    prize = game["prize"] or "Не указан"

    description = game["description"] or ""

    leader = game["leader_name"]

    if game["state"] == "waiting":
        timer_line = "⏳ Ожидаем первого сообщения…"
    elif game["state"] == "paused":
        timer_line = (
            f"⏸ Пауза\n"
            f"⏳ Осталось: {format_time(remaining)}"
        )
    elif game["state"] == "active":
        timer_line = (
            f"⏳ {format_time(remaining)} "
            f"{progress_bar(remaining, duration)}"
        )
    else:
        timer_line = ""

    leader_line = ""

    if leader:
        leader_line = f"\n👑 Текущий лидер: {leader}\n"

    text = (
        "🏁 <b>ИВЕНТ НА ПЕРЕБИВ</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🎁 <b>Приз</b> → {prize}\n"
        f"⏱ <b>Длительность</b> → {format_time(duration)}\n"
        "✏️ <i>Любое сообщение в чате перебивает лидера "
        "и обнуляет таймер.</i>\n"
        "🚫 <i>Сообщения админов чата не засчитываются.</i>\n"
        f"{leader_line}\n"
        f"{timer_line}"
    )

    if description:
        text += f"\n\n{description}"

    return text


def menu_keyboard():
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
                    callback_data="pause",
                ),
                InlineKeyboardButton(
                    text="▶️ Возобновить",
                    callback_data="resume",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏹ Завершить игру",
                    callback_data="stop",
                )
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


def duration_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5 минут",
                    callback_data="duration_300",
                ),
                InlineKeyboardButton(
                    text="10 минут",
                    callback_data="duration_600",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="15 минут",
                    callback_data="duration_900",
                ),
                InlineKeyboardButton(
                    text="30 минут",
                    callback_data="duration_1800",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Своя длительность",
                    callback_data="duration_custom",
                )
            ],
        ]
    )


def preview_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Запустить игру",
                    callback_data="confirm_start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎁 Изменить приз",
                    callback_data="edit_prize",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить описание",
                    callback_data="edit_description",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel_new",
                )
            ],
        ]
    )


async def edit_event(bot: Bot):
    game = get_game()

    if not game:
        return

    if not game["chat_id"] or not game["event_message_id"]:
        return

    remaining = get_remaining(game)

    try:
        await bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["event_message_id"],
            text=event_text(game, remaining),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def cancel_timer():
    global timer_task

    if timer_task and not timer_task.done():
        timer_task.cancel()

        with suppress(asyncio.CancelledError):
            await timer_task

    timer_task = None


async def restart_timer(bot: Bot):
    global timer_task

    await cancel_timer()

    game = get_game()

    if not game:
        return

    if game["state"] != "active":
        return

    timer_task = asyncio.create_task(timer_loop(bot))


# =========================================================
# TIMER
# =========================================================

async def timer_loop(bot: Bot):
    try:
        while True:
            await asyncio.sleep(TIMER_UPDATE_SECONDS)

            async with game_lock:
                game = get_game()

                if not game:
                    return

                if game["state"] != "active":
                    return

                remaining = get_remaining(game)

                if remaining <= 0:
                    await finish_winner(bot)
                    return

                save_remaining(remaining)

                await edit_event(bot)

    except asyncio.CancelledError:
        raise


async def finish_winner(bot: Bot):
    game = get_game()

    if not game:
        return

    leader_id = game["leader_id"]
    leader_name = game["leader_name"] or "Участник"
    duration = game["duration"]

    if not leader_id:
        return

    add_winner(
        leader_id,
        leader_name,
        duration,
    )

    try:
        await bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🏆 <b>ПОБЕДИТЕЛЬ!</b>\n\n"
                f"👑 {leader_name}\n"
                f"⏱ Продержался {format_time(duration)} "
                "без перебива!"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        await bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["event_message_id"],
            text=(
                "🏆 <b>ИГРА ЗАВЕРШЕНА</b>\n\n"
                f"👑 Победитель: {leader_name}\n"
                f"⏱ Продержался: {format_time(duration)}\n\n"
                "🎉 Поздравляем!"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    clear_game()


# =========================================================
# КОМАНДЫ
# =========================================================

@router.message(Command("start"))
async def start_command(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    await message.answer(
        "🎮 <b>Управление игрой «Перебив»</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(),
    )


@router.message(Command("menu"))
async def menu_command(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return

    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "🎮 Меню управления:",
        reply_markup=menu_keyboard(),
    )


@router.message(Command("id"))
async def id_command(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            f"Твой Telegram ID: <code>{message.from_user.id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await message.answer(
        f"ID этого чата: <code>{message.chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# НОВАЯ ИГРА
# =========================================================

@router.callback_query(F.data == "new_game")
async def new_game(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(NewGame.duration)

    await callback.message.edit_text(
        "⏱ <b>Выбери длительность игры:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=duration_keyboard(),
    )

    await callback.answer()


@router.callback_query(F.data.startswith("duration_"))
async def choose_duration(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    value = callback.data.split("_", 1)[1]

    if value == "custom":
        await state.set_state(NewGame.custom_duration)

        await callback.message.edit_text(
            "⏱ Напиши длительность в минутах.\n\n"
            "Например: <b>20</b>",
            parse_mode=ParseMode.HTML,
        )

        await callback.answer()
        return

    duration = int(value)

    await state.update_data(duration=duration)
    await state.set_state(NewGame.prize)

    await callback.message.edit_text(
        "🎁 Теперь напиши <b>приз</b>.\n\n"
        "Можно написать текст, добавить ссылку или "
        "сделать красивое форматирование Telegram.",
        parse_mode=ParseMode.HTML,
    )

    await callback.answer()


@router.message(NewGame.custom_duration)
async def custom_duration(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()

    if not text.isdigit():
        await message.answer(
            "Напиши только число минут.\nНапример: <b>20</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    minutes = int(text)

    if minutes < 1 or minutes > 1440:
        await message.answer(
            "Можно указать от 1 до 1440 минут."
        )
        return

    await state.update_data(duration=minutes * 60)
    await state.set_state(NewGame.prize)

    await message.answer(
        "🎁 Теперь напиши <b>приз</b>.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewGame.prize)
async def enter_prize(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    prize = message.html_text or ""

    await state.update_data(prize=prize)
    await state.set_state(NewGame.description)

    await message.answer(
        "✏️ Теперь напиши дополнительное описание.\n\n"
        "Если описание не нужно — напиши <b>нет</b>.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewGame.description)
async def enter_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    description = message.html_text or ""

    if description.strip().lower() in {
        "нет",
        "-",
        "не нужно",
        "пропустить",
    }:
        description = ""

    await state.update_data(description=description)
    await state.set_state(NewGame.preview)

    data = await state.get_data()

    duration = data["duration"]

    preview_game = {
        "state": "waiting",
        "duration": duration,
        "remaining": duration,
        "leader_name": None,
        "prize": data.get("prize", ""),
        "description": data.get("description", ""),
    }

    await message.answer(
        "👀 <b>Предпросмотр:</b>\n\n"
        + event_text(preview_game),
        parse_mode=ParseMode.HTML,
        reply_markup=preview_keyboard(),
        link_preview_options=LinkPreviewOptions(
            is_disabled=False,
            prefer_large_media=True,
            show_above_text=True,
        ),
    )


# =========================================================
# ЗАПУСК
# =========================================================

@router.callback_query(F.data == "confirm_start")
async def confirm_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    data = await state.get_data()

    duration = int(data["duration"])
    prize = data.get("prize", "")
    description = data.get("description", "")

    if GAME_CHAT_ID == 0:
        await callback.message.answer(
            "⚠️ Сначала нужно узнать ID группы.\n\n"
            "Открой группу и отправь там команду:\n"
            "<code>/id</code>",
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    create_game(
        GAME_CHAT_ID,
        duration,
        prize,
        description,
    )

    game = get_game()

    preview_url = get_preview_url(prize)

    sent = await bot.send_message(
        chat_id=GAME_CHAT_ID,
        text=event_text(game),
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(
            is_disabled=preview_url is None,
            url=preview_url,
            prefer_large_media=True,
            show_above_text=True,
        ),
    )

    set_event_message(sent.message_id)

    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Игра запущена!</b>\n\n"
        "Первое обычное сообщение в группе станет первым лидером.",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(),
    )

    await callback.answer()

    await restart_timer(bot)


# =========================================================
# ПАУЗА
# =========================================================

@router.callback_query(F.data == "pause")
async def pause_callback(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    async with game_lock:
        game = get_game()

        if not game or game["state"] != "active":
            await callback.answer(
                "Активной игры нет.",
                show_alert=True,
            )
            return

        pause_game()
        await cancel_timer()
        await edit_event(bot)

    await callback.answer("⏸ Игра поставлена на паузу.")


# =========================================================
# ВОЗОБНОВЛЕНИЕ
# =========================================================

@router.callback_query(F.data == "resume")
async def resume_callback(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    async with game_lock:
        game = get_game()

        if not game or game["state"] != "paused":
            await callback.answer(
                "Игра не находится на паузе.",
                show_alert=True,
            )
            return

        resume_game()
        await edit_event(bot)

        await restart_timer(bot)

    await callback.answer("▶️ Игра продолжена.")


# =========================================================
# ОСТАНОВКА
# =========================================================

@router.callback_query(F.data == "stop")
async def stop_callback(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    async with game_lock:
        game = get_game()

        if not game or game["state"] == "waiting":
            await callback.answer(
                "Активной игры нет.",
                show_alert=True,
            )
            return

        await cancel_timer()

        try:
            await bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["event_message_id"],
                text=(
                    "⏹ <b>ИГРА ЗАВЕРШЕНА</b>\n\n"
                    "Администратор завершил игру."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        clear_game()

    await callback.answer("Игра завершена.")


# =========================================================
# СТАТУС
# =========================================================

@router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    game = get_game()

    if not game or game["state"] == "waiting":
        await callback.message.answer(
            "📊 Сейчас активной игры нет."
        )
        await callback.answer()
        return

    remaining = get_remaining(game)

    text = (
        "📊 <b>Статус игры</b>\n\n"
        f"Состояние: <b>{game['state']}</b>\n"
        f"Лидер: <b>{game['leader_name'] or 'ещё нет'}</b>\n"
        f"Осталось: <b>{format_time(remaining)}</b>\n"
        f"Длительность: <b>{format_time(game['duration'])}</b>"
    )

    await callback.message.answer(
        text,
        parse_mode=ParseMode.HTML,
    )

    await callback.answer()


# =========================================================
# ПОБЕДИТЕЛИ
# =========================================================

@router.callback_query(F.data == "winners")
async def winners_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    winners = get_winners()

    if not winners:
        await callback.message.answer(
            "🏆 Победителей пока нет."
        )
        await callback.answer()
        return

    lines = ["🏆 <b>Последние победители</b>\n"]

    for index, (name, duration, won_at) in enumerate(
        winners,
        start=1,
    ):
        date = time.strftime(
            "%d.%m.%Y %H:%M",
            time.localtime(won_at),
        )

        lines.append(
            f"{index}. {html.escape(name)} — "
            f"{format_time(duration)} — {date}"
        )

    await callback.message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

    await callback.answer()


# =========================================================
# ИЗМЕНЕНИЕ ПРИЗА
# =========================================================

@router.callback_query(F.data == "edit_prize")
async def edit_prize_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(NewGame.prize)

    await callback.message.edit_text(
        "🎁 Напиши новый приз.",
        parse_mode=ParseMode.HTML,
    )

    await callback.answer()


# =========================================================
# ИЗМЕНЕНИЕ ОПИСАНИЯ
# =========================================================

@router.callback_query(F.data == "edit_description")
async def edit_description_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(NewGame.description)

    await callback.message.edit_text(
        "✏️ Напиши новое описание.\n\n"
        "Если описание не нужно — напиши <b>нет</b>.",
        parse_mode=ParseMode.HTML,
    )

    await callback.answer()


# =========================================================
# ОТМЕНА СОЗДАНИЯ
# =========================================================

@router.callback_query(F.data == "cancel_new")
async def cancel_new_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.clear()

    await callback.message.edit_text(
        "❌ Создание игры отменено.",
        reply_markup=menu_keyboard(),
    )

    await callback.answer()


# =========================================================
# СООБЩЕНИЯ В ГРУППЕ — ПЕРЕБИВ
# =========================================================

async def check_group_admin(bot: Bot, user_id: int) -> bool:
    if GAME_CHAT_ID == 0:
        return False

    try:
        member = await bot.get_chat_member(
            GAME_CHAT_ID,
            user_id,
        )

        return member.status in {
            "administrator",
            "creator",
        }

    except Exception:
        return False


@router.message()
async def group_messages(message: Message, bot: Bot):
    if message.chat.type not in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        return

    if GAME_CHAT_ID == 0:
        return

    if message.chat.id != GAME_CHAT_ID:
        return

    if not message.from_user:
        return

    if message.from_user.is_bot:
        return

    # Команды не считаем перебивом.
    if message.text and message.text.startswith("/"):
        return

    async with game_lock:
        game = get_game()

        if not game:
            return

        if game["state"] != "active" and game["state"] != "waiting":
            return

        # Администраторы группы не перебивают лидера.
        if await check_group_admin(
            bot,
            message.from_user.id,
        ):
            return

        name = display_name(message)

        # Первый обычный пользователь становится лидером.
        # Любое следующее сообщение делает его новым лидером.
        set_leader(
            message.from_user.id,
            name,
            game["duration"],
        )

        game = get_game()

        await edit_event(bot)

        try:
            await message.reply(
                "📢 <b>Перебито!</b>\n"
                f"👑 Новый лидер: {name}\n"
                f"⏱ До победы: {format_time(game['duration'])}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        await restart_timer(bot)


# =========================================================
# ВОССТАНОВЛЕНИЕ ПОСЛЕ ПЕРЕЗАПУСКА
# =========================================================

async def restore_game(bot: Bot):
    game = get_game()

    if not game:
        return

    if game["state"] == "active":
        remaining = get_remaining(game)

        if remaining <= 0:
            await finish_winner(bot)
            return

        save_remaining(remaining)

        await edit_event(bot)

        await restart_timer(bot)

    elif game["state"] == "paused":
        await edit_event(bot)


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    db_init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    dp.include_router(router)

    await restore_game(bot)

    print("Bot started.")

    try:
        await dp.start_polling(bot)
    finally:
        await cancel_timer()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
