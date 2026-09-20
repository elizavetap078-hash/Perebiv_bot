import asyncio
import html
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
TIMER_UPDATE_SECONDS = int(os.getenv("TIMER_UPDATE_SECONDS", "20"))
WEB_PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

DB_FILE = "perebiv.db"


# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher(storage=MemoryStorage())


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    saved_at INTEGER NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS games (
    chat_id INTEGER PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 600,
    remaining INTEGER NOT NULL DEFAULT 600,
    prize TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    leader_id INTEGER,
    leader_name TEXT,
    leader_username TEXT,
    started_at INTEGER,
    timer_message_id INTEGER,
    announcement_message_id INTEGER
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS winners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    username TEXT,
    duration INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)
""")

db.commit()


# =========================================================
# КЭШ АДМИНИСТРАТОРОВ
# =========================================================

admin_cache = {}
admin_cache_time = {}
ADMIN_CACHE_SECONDS = 60


async def get_group_admins(chat_id: int):
    current = time.time()

    if (
        chat_id in admin_cache
        and current - admin_cache_time.get(chat_id, 0)
        < ADMIN_CACHE_SECONDS
    ):
        return admin_cache[chat_id]

    admins = set()

    try:
        members = await bot.get_chat_administrators(chat_id)

        for member in members:
            admins.add(member.user.id)

    except Exception as e:
        print(
            f"Не удалось получить админов {chat_id}:",
            repr(e)
        )

    admin_cache[chat_id] = admins
    admin_cache_time[chat_id] = current

    return admins


# =========================================================
# HELPERS
# =========================================================

def now():
    return int(time.time())


def format_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def mention_user(user_id, name, username=None):
    if username:
        return f"@{html.escape(username)}"

    return (
        f'<a href="tg://user?id={user_id}">'
        f'{html.escape(name or "Участник")}'
        f'</a>'
    )


def get_chat(chat_id):
    return db.execute(
        "SELECT * FROM chats WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()


def save_chat(chat):
    db.execute("""
        INSERT INTO chats(
            chat_id,
            title,
            username,
            saved_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            saved_at=excluded.saved_at
    """, (
        chat.id,
        chat.title or str(chat.id),
        chat.username,
        now()
    ))

    db.commit()


def get_saved_chats():
    return db.execute("""
        SELECT *
        FROM chats
        ORDER BY title COLLATE NOCASE
    """).fetchall()


def get_game(chat_id):
    row = db.execute(
        "SELECT * FROM games WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()

    if row:
        return row

    db.execute("""
        INSERT INTO games(
            chat_id,
            active,
            paused,
            duration,
            remaining
        )
        VALUES (?, 0, 0, 600, 600)
    """, (chat_id,))

    db.commit()

    return db.execute(
        "SELECT * FROM games WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()


def update_game(chat_id, **values):
    if not values:
        return

    fields = []
    params = []

    for key, value in values.items():
        fields.append(f"{key} = ?")
        params.append(value)

    params.append(chat_id)

    db.execute(
        f"""
        UPDATE games
        SET {", ".join(fields)}
        WHERE chat_id = ?
        """,
        params
    )

    db.commit()


def get_winners(chat_id):
    return db.execute("""
        SELECT *
        FROM winners
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT 10
    """, (chat_id,)).fetchall()


async def bot_is_admin(chat_id):
    try:
        me = await bot.get_me()

        member = await bot.get_chat_member(
            chat_id,
            me.id
        )

        return member.status in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        }

    except Exception:
        return False


# =========================================================
# CUSTOM EMOJI
# =========================================================

EMOJI_FLAG = "5429274854727656965"
EMOJI_GIFT = "5199749070830197566"
EMOJI_TIMER = "5262540380301191210"
EMOJI_PENCIL = "5395444784611480792"
EMOJI_NO = "5240241223632954241"


def tg_emoji(emoji_id, fallback):
    return (
        f'<tg-emoji emoji-id="{emoji_id}">'
        f'{fallback}'
        f'</tg-emoji>'
    )


# =========================================================
# ТЕКСТ ИВЕНТА
# =========================================================

def build_event_text(prize, duration, description):
    text = (
        f'{tg_emoji(EMOJI_FLAG, "🏁")} '
        f'<b>ИВЕНТ НА ПЕРЕБИВ</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{tg_emoji(EMOJI_GIFT, "🎁")} '
        f'<b>Приз</b> → {prize}\n'
        f'{tg_emoji(EMOJI_TIMER, "⏱")} '
        f'<b>Длительность</b> → {format_time(duration)}\n\n'
        f'{tg_emoji(EMOJI_PENCIL, "✏️")} '
        f'<i>Любое сообщение в чате перебивает '
        f'лидера и обнуляет таймер.</i>\n'
        f'{tg_emoji(EMOJI_NO, "🚫")} '
        f'<i>Сообщения админов чата не засчитываются.</i>'
    )

    if description:
        text += f"\n\n{description}"

    return text


def build_timer_text(game):
    if game["paused"]:
        status = "⏸ <b>ПАУЗА</b>"
    else:
        status = "🔥 <b>ИГРА ИДЁТ</b>"

    if game["leader_id"]:
        leader = mention_user(
            game["leader_id"],
            game["leader_name"],
            game["leader_username"]
        )
    else:
        leader = "Ожидаем первого участника"

    return (
        f"{status}\n\n"
        f"👑 <b>Лидер:</b> {leader}\n"
        f"⏱ <b>До победы:</b> "
        f"<b>{format_time(game['remaining'])}</b>"
    )


# =========================================================
# МЕНЮ ЛС
# =========================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Новая игра",
                    callback_data="new_game"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏸ Пауза",
                    callback_data="pause_game"
                ),
                InlineKeyboardButton(
                    text="▶️ Возобновить",
                    callback_data="resume_game"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏹ Завершить игру",
                    callback_data="stop_game"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус",
                    callback_data="status_game"
                ),
                InlineKeyboardButton(
                    text="🏆 Победители",
                    callback_data="winners_game"
                )
            ]
        ]
    )


def chats_keyboard(action, chats):
    buttons = []

    for chat in chats:
        title = chat["title"]

        if len(title) > 35:
            title = title[:32] + "..."

        buttons.append([
            InlineKeyboardButton(
                text=f"🎮 {title}",
                callback_data=f"{action}:{chat['chat_id']}"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# =========================================================
# СОЗДАНИЕ ИГРЫ
# =========================================================

class NewGame(StatesGroup):
    duration = State()
    prize = State()
    description = State()


@dp.message(Command("start"))
async def start_private(message: Message, state: FSMContext):
    # В группе /start полностью игнорируем.
    if message.chat.type != "private":
        return

    await state.clear()

    await message.answer(
        "🎮 <b>ПЕРЕБИВ</b>\n\n"
        "Управление игровыми событиями.\n\n"
        "Выбери действие:",
        reply_markup=main_menu()
    )


# /id работает ТОЛЬКО в личке.
@dp.message(Command("id"))
async def id_private(message: Message):
    if message.chat.type != "private":
        return

    await message.answer(
        f"<code>{message.chat.id}</code>"
    )


@dp.callback_query(F.data == "new_game")
async def new_game(callback: CallbackQuery):
    await callback.answer()

    chats = []

    for chat in get_saved_chats():
        if await bot_is_admin(chat["chat_id"]):
            chats.append(chat)

    if not chats:
        await callback.message.answer(
            "❗ Пока нет доступных групп.\n\n"
            "Добавь бота администратором в группу "
            "и отправь там любое сообщение."
        )
        return

    await callback.message.answer(
        "🎮 <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "newchat",
            chats
        )
    )


@dp.callback_query(F.data.startswith("newchat:"))
async def choose_new_chat(
    callback: CallbackQuery,
    state: FSMContext
):
    await callback.answer()

    chat_id = int(callback.data.split(":")[1])

    await state.update_data(
        chat_id=chat_id
    )

    await state.set_state(
        NewGame.duration
    )

    await callback.message.answer(
        "⏱ <b>Длительность игры</b>\n\n"
        "Напиши количество минут.\n\n"
        "Например: <code>10</code>"
    )


@dp.message(NewGame.duration)
async def game_duration(
    message: Message,
    state: FSMContext
):
    if message.chat.type != "private":
        return

    try:
        minutes = int(message.text.strip())
    except Exception:
        await message.answer(
            "❗ Напиши число минут.\n"
            "Например: <code>10</code>"
        )
        return

    if minutes < 1 or minutes > 1440:
        await message.answer(
            "❗ Можно указать от 1 до 1440 минут."
        )
        return

    await state.update_data(
        duration=minutes * 60
    )

    await state.set_state(
        NewGame.prize
    )

    await message.answer(
        "🎁 <b>Приз</b>\n\n"
        "Напиши приз."
    )


@dp.message(NewGame.prize)
async def game_prize(
    message: Message,
    state: FSMContext
):
    if message.chat.type != "private":
        return

    prize = message.html_text.strip()

    if not prize:
        await message.answer(
            "❗ Приз не может быть пустым."
        )
        return

    await state.update_data(
        prize=prize
    )

    await state.set_state(
        NewGame.description
    )

    await message.answer(
        "✏️ <b>Описание</b>\n\n"
        "Напиши описание ивента.\n\n"
        "Можно использовать жирный, курсив, "
        "ссылки и custom emoji.\n\n"
        "Если описание не нужно — отправь <code>-</code>."
    )


@dp.message(NewGame.description)
async def game_description(
    message: Message,
    state: FSMContext
):
    if message.chat.type != "private":
        return

    data = await state.get_data()

    chat_id = int(data["chat_id"])
    duration = int(data["duration"])
    prize = data["prize"]

    description = message.html_text.strip()

    if description == "-":
        description = ""

    old_game = get_game(chat_id)

    if old_game["active"]:
        await finish_game(
            chat_id,
            send_winner=False
        )

    update_game(
        chat_id,
        active=1,
        paused=0,
        duration=duration,
        remaining=duration,
        prize=prize,
        description=description,
        leader_id=None,
        leader_name=None,
        leader_username=None,
        started_at=None,
        timer_message_id=None,
        announcement_message_id=None
    )

    event_message = await bot.send_message(
        chat_id,
        build_event_text(
            prize,
            duration,
            description
        )
    )

    timer_message = await bot.send_message(
        chat_id,
        build_timer_text(
            get_game(chat_id)
        )
    )

    update_game(
        chat_id,
        timer_message_id=timer_message.message_id,
        announcement_message_id=event_message.message_id
    )

    await state.clear()

    chat = get_chat(chat_id)

    await message.answer(
        "✅ <b>Ивент запущен!</b>\n\n"
        f"🎮 Группа: "
        f"<b>{html.escape(chat['title'])}</b>\n"
        f"⏱ Длительность: "
        f"<b>{format_time(duration)}</b>\n"
        f"🎁 Приз: {prize}",
        reply_markup=main_menu()
    )


# =========================================================
# ГЛАВНЫЙ ОБРАБОТЧИК ГРУППЫ
# =========================================================

@dp.message()
async def group_handler(message: Message):
    # Только группы.
    if message.chat.type not in {
        "group",
        "supergroup"
    }:
        return

    # Команды в группе полностью игнорируются.
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return

    # Запоминаем группу только при обычном сообщении.
    save_chat(message.chat)

    game = get_game(message.chat.id)

    # Вне активного ивента — полная тишина.
    if not game["active"]:
        return

    user_id = message.from_user.id

    # Получаем список администраторов из кэша.
    admins = await get_group_admins(
        message.chat.id
    )

    # Администраторы никогда не перебивают.
    if user_id in admins:
        return

    # -----------------------------------------------------
    # ОБНОВЛЯЕМ ТАЙМЕР ПЕРЕД ПРОВЕРКОЙ ЛИДЕРА
    # -----------------------------------------------------

    if not game["paused"] and game["started_at"]:
        elapsed = now() - int(game["started_at"])

        remaining = max(
            0,
            int(game["remaining"]) - elapsed
        )

        if remaining <= 0:
            await declare_winner(
                message.chat.id
            )
            return

        update_game(
            message.chat.id,
            remaining=remaining,
            started_at=now()
        )

        game = get_game(
            message.chat.id
        )

    # -----------------------------------------------------
    # СООБЩЕНИЕ ТЕКУЩЕГО ЛИДЕРА
    # -----------------------------------------------------

    if (
        game["leader_id"] is not None
        and int(game["leader_id"]) == int(user_id)
    ):
        return

    # -----------------------------------------------------
    # ПЕРВЫЙ ЛИДЕР / НОВЫЙ ЛИДЕР
    # -----------------------------------------------------

    is_first_leader = (
        game["leader_id"] is None
    )

    update_game(
        message.chat.id,
        leader_id=user_id,
        leader_name=(
            message.from_user.full_name
            or message.from_user.first_name
            or "Участник"
        ),
        leader_username=message.from_user.username,
        remaining=game["duration"],
        started_at=now(),
        paused=0
    )

    game = get_game(
        message.chat.id
    )

    person = mention_user(
        user_id,
        message.from_user.full_name
        or message.from_user.first_name
        or "Участник",
        message.from_user.username
    )

    # -----------------------------------------------------
    # ПЕРВЫЙ ЛИДЕР
    # -----------------------------------------------------

    if is_first_leader:
        text = (
            "🏁 <b>Новый лидер!</b>\n"
            f"👑 {person}\n"
            f"⏱ До победы: "
            f"<b>{format_time(game['remaining'])}</b>"
        )

    # -----------------------------------------------------
    # ПЕРЕБИВ
    # -----------------------------------------------------

    else:
        text = (
            "📢 <b>Перебито!</b>\n"
            f"👑 Новый лидер: {person}\n"
            f"⏱ До победы: "
            f"<b>{format_time(game['remaining'])}</b>"
        )

    # ВАЖНО: отвечаем именно реплаем.
    await message.reply(text)

    # Сразу обновляем единственный таймер.
    await refresh_timer_message(
        message.chat.id
    )


# =========================================================
# ТАЙМЕР
# =========================================================

async def refresh_timer_message(chat_id):
    game = get_game(chat_id)

    if not game["timer_message_id"]:
        return

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=game["timer_message_id"],
            text=build_timer_text(game)
        )
    except Exception as e:
        print(
            "Timer edit error:",
            repr(e)
        )


async def timer_loop():
    while True:
        try:
            games = db.execute("""
                SELECT *
                FROM games
                WHERE active = 1
            """).fetchall()

            for game in games:
                chat_id = game["chat_id"]

                if game["paused"]:
                    continue

                if not game["leader_id"]:
                    continue

                if not game["started_at"]:
                    continue

                elapsed = (
                    now()
                    - int(game["started_at"])
                )

                remaining = max(
                    0,
                    int(game["remaining"])
                    - elapsed
                )

                if remaining <= 0:
                    await declare_winner(
                        chat_id
                    )
                    continue

                update_game(
                    chat_id,
                    remaining=remaining,
                    started_at=now()
                )

                await refresh_timer_message(
                    chat_id
                )

        except Exception as e:
            print(
                "Timer loop error:",
                repr(e)
            )

        await asyncio.sleep(
            TIMER_UPDATE_SECONDS
        )


# =========================================================
# ПОБЕДИТЕЛЬ
# =========================================================

async def declare_winner(chat_id):
    game = get_game(chat_id)

    if not game["active"]:
        return

    if not game["leader_id"]:
        return

    user_id = game["leader_id"]
    name = game["leader_name"] or "Участник"
    username = game["leader_username"]

    db.execute("""
        INSERT INTO winners(
            chat_id,
            user_id,
            name,
            username,
            duration,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        user_id,
        name,
        username,
        game["duration"],
        now()
    ))

    db.commit()

    person = mention_user(
        user_id,
        name,
        username
    )

    await bot.send_message(
        chat_id,
        "🏆 <b>ПОБЕДИТЕЛЬ!</b>\n\n"
        f"👑 {person}\n"
        f"⏱ Продержался "
        f"<b>{format_time(game['duration'])}</b> "
        f"без перебива!"
    )

    update_game(
        chat_id,
        active=0,
        paused=0,
        remaining=game["duration"],
        leader_id=None,
        leader_name=None,
        leader_username=None,
        started_at=None
    )


# =========================================================
# ЗАВЕРШЕНИЕ
# =========================================================

async def finish_game(
    chat_id,
    send_winner=False
):
    game = get_game(chat_id)

    if send_winner:
        await declare_winner(chat_id)
        return

    update_game(
        chat_id,
        active=0,
        paused=0,
        remaining=game["duration"],
        leader_id=None,
        leader_name=None,
        leader_username=None,
        started_at=None
    )


# =========================================================
# УПРАВЛЕНИЕ ИЗ ЛС
# =========================================================

@dp.callback_query(F.data == "pause_game")
async def pause_menu(callback):
    await callback.answer()

    chats = get_saved_chats()

    if not chats:
        await callback.message.answer(
            "Нет сохранённых групп."
        )
        return

    await callback.message.answer(
        "⏸ <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "pause",
            chats
        )
    )


@dp.callback_query(F.data == "resume_game")
async def resume_menu(callback):
    await callback.answer()

    await callback.message.answer(
        "▶️ <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "resume",
            get_saved_chats()
        )
    )


@dp.callback_query(F.data == "stop_game")
async def stop_menu(callback):
    await callback.answer()

    await callback.message.answer(
        "⏹ <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "stop",
            get_saved_chats()
        )
    )


@dp.callback_query(F.data == "status_game")
async def status_menu(callback):
    await callback.answer()

    await callback.message.answer(
        "📊 <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "status",
            get_saved_chats()
        )
    )


@dp.callback_query(F.data == "winners_game")
async def winners_menu(callback):
    await callback.answer()

    await callback.message.answer(
        "🏆 <b>Выбери группу:</b>",
        reply_markup=chats_keyboard(
            "winners",
            get_saved_chats()
        )
    )


@dp.callback_query(F.data.startswith("pause:"))
async def pause_selected(callback):
    await callback.answer()

    chat_id = int(
        callback.data.split(":")[1]
    )

    game = get_game(chat_id)

    if not game["active"]:
        await callback.message.answer(
            "ℹ️ Игра не запущена."
        )
        return

    if game["paused"]:
        await callback.message.answer(
            "ℹ️ Игра уже на паузе."
        )
        return

    if game["started_at"]:
        elapsed = (
            now()
            - int(game["started_at"])
        )

        remaining = max(
            0,
            int(game["remaining"]) - elapsed
        )

        update_game(
            chat_id,
            remaining=remaining,
            started_at=None,
            paused=1
        )

    else:
        update_game(
            chat_id,
            paused=1
        )

    await refresh_timer_message(
        chat_id
    )

    await callback.message.answer(
        "⏸ <b>Игра поставлена на паузу.</b>"
    )


@dp.callback_query(F.data.startswith("resume:"))
async def resume_selected(callback):
    await callback.answer()

    chat_id = int(
        callback.data.split(":")[1]
    )

    game = get_game(chat_id)

    if not game["active"]:
        await callback.message.answer(
            "ℹ️ Игра не запущена."
        )
        return

    if not game["paused"]:
        await callback.message.answer(
            "ℹ️ Игра уже идёт."
        )
        return

    update_game(
        chat_id,
        paused=0,
        started_at=now()
    )

    await refresh_timer_message(
        chat_id
    )

    await callback.message.answer(
        "▶️ <b>Игра продолжена.</b>"
    )


@dp.callback_query(F.data.startswith("stop:"))
async def stop_selected(callback):
    await callback.answer()

    chat_id = int(
        callback.data.split(":")[1]
    )

    game = get_game(chat_id)

    if not game["active"]:
        await callback.message.answer(
            "ℹ️ Игра уже завершена."
        )
        return

    await finish_game(
        chat_id,
        send_winner=False
    )

    await callback.message.answer(
        "⏹ <b>Игра завершена.</b>"
    )


@dp.callback_query(F.data.startswith("status:"))
async def status_selected(callback):
    await callback.answer()

    chat_id = int(
        callback.data.split(":")[1]
    )

    game = get_game(chat_id)
    chat = get_chat(chat_id)

    title = (
        chat["title"]
        if chat
        else str(chat_id)
    )

    if not game["active"]:
        status = "не запущена"
    elif game["paused"]:
        status = "на паузе"
    else:
        status = "идёт"

    if game["leader_id"]:
        leader = mention_user(
            game["leader_id"],
            game["leader_name"],
            game["leader_username"]
        )
    else:
        leader = "нет"

    await callback.message.answer(
        "📊 <b>Статус</b>\n\n"
        f"🎮 Группа: "
        f"<b>{html.escape(title)}</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"👑 Лидер: {leader}\n"
        f"⏱ Осталось: "
        f"<b>{format_time(game['remaining'])}</b>"
    )


@dp.callback_query(F.data.startswith("winners:"))
async def winners_selected(callback):
    await callback.answer()

    chat_id = int(
        callback.data.split(":")[1]
    )

    winners = get_winners(chat_id)

    if not winners:
        await callback.message.answer(
            "🏆 Победителей пока нет."
        )
        return

    text = "🏆 <b>Последние победители</b>\n\n"

    for i, winner in enumerate(
        winners,
        start=1
    ):
        person = mention_user(
            winner["user_id"],
            winner["name"],
            winner["username"]
        )

        date = datetime.fromtimestamp(
            winner["created_at"]
        ).strftime("%d.%m.%Y")

        text += (
            f"{i}. {person} — "
            f"{format_time(winner['duration'])} "
            f"({date})\n"
        )

    await callback.message.answer(text)


# =========================================================
# RENDER WEB SERVER
# =========================================================

async def health(request):
    return web.Response(
        text="Perebiv_bot is live!"
    )


async def start_web():
    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        WEB_PORT
    )

    await site.start()

    print(
        f"Web server started on port {WEB_PORT}"
    )


# =========================================================
# START
# =========================================================

async def main():
    print("Perebiv_bot starting...")

    await start_web()

    asyncio.create_task(
        timer_loop()
    )

    print("Polling started.")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())
