import asyncio
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dotenv import load_dotenv


load_dotenv()


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

TIMER_UPDATE_SECONDS = int(
    os.getenv("TIMER_UPDATE_SECONDS", "20")
)

WEB_PORT = int(
    os.getenv("PORT", "10000")
)


if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_IDS")


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

DB_FILE = "perebiv.db"


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            chat_id INTEGER PRIMARY KEY,
            active INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0,

            duration INTEGER DEFAULT 600,
            remaining INTEGER DEFAULT 600,

            leader_id INTEGER,
            leader_name TEXT,
            leader_username TEXT,

            prize TEXT,
            description TEXT,

            started_at REAL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            chat_id INTEGER,
            user_id INTEGER,

            name TEXT,
            username TEXT,

            duration INTEGER,
            prize TEXT,

            won_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_game(chat_id):
    conn = db()
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        """
        SELECT *
        FROM games
        WHERE chat_id = ?
        """,
        (chat_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO games
            (
                chat_id,
                active,
                paused,
                duration,
                remaining
            )
            VALUES (?, 0, 0, 600, 600)
            """,
            (chat_id,)
        )

        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM games
            WHERE chat_id = ?
            """,
            (chat_id,)
        ).fetchone()

    conn.close()

    return dict(row)


def update_game(chat_id, **kwargs):
    if not kwargs:
        return

    conn = db()

    fields = []
    values = []

    for key, value in kwargs.items():
        fields.append(f"{key} = ?")
        values.append(value)

    values.append(chat_id)

    conn.execute(
        f"""
        UPDATE games
        SET {", ".join(fields)}
        WHERE chat_id = ?
        """,
        values
    )

    conn.commit()
    conn.close()


def add_winner(
    chat_id,
    user_id,
    name,
    username,
    duration,
    prize
):
    conn = db()

    conn.execute(
        """
        INSERT INTO winners
        (
            chat_id,
            user_id,
            name,
            username,
            duration,
            prize,
            won_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            user_id,
            name,
            username,
            duration,
            prize,
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )
    )

    conn.commit()
    conn.close()


def get_winners(chat_id):
    conn = db()

    rows = conn.execute(
        """
        SELECT
            name,
            username,
            duration,
            prize,
            won_at
        FROM winners
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    return rows


# =========================================================
# HELPERS
# =========================================================

def format_time(seconds):
    seconds = max(0, int(seconds))

    minutes = seconds // 60
    seconds = seconds % 60

    return f"{minutes:02d}:{seconds:02d}"


def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def leader_name(game):
    if game["leader_username"]:
        return f"@{game['leader_username']}"

    return game["leader_name"] or "—"


def get_remaining(game):
    remaining = int(game["remaining"])

    if (
        game["active"]
        and not game["paused"]
        and game["leader_id"]
        and game["started_at"]
    ):
        elapsed = int(
            time.time() - game["started_at"]
        )

        remaining = max(
            0,
            remaining - elapsed
        )

    return remaining


# =========================================================
# ADMIN MENU
# =========================================================

def menu_keyboard():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="▶️ Новая игра",
        callback_data="new_game"
    )

    builder.button(
        text="⏸ Пауза",
        callback_data="pause_game"
    )

    builder.button(
        text="▶️ Возобновить",
        callback_data="resume_game"
    )

    builder.button(
        text="⏹ Завершить игру",
        callback_data="stop_game"
    )

    builder.button(
        text="📊 Статус",
        callback_data="status"
    )

    builder.button(
        text="🏆 Победители",
        callback_data="winners"
    )

    builder.adjust(1)

    return builder.as_markup()


# =========================================================
# TEMPORARY ADMIN SETUP
# =========================================================

pending_games = {}


# =========================================================
# /start
# =========================================================

@dp.message(Command("start"))
async def start_handler(message: Message):

    if message.chat.type != "private":
        return

    if not is_admin(message.from_user.id):
        await message.answer(
            "Этот бот предназначен для управления игрой."
        )
        return

    await message.answer(
        "<b>🎮 Управление игрой</b>\n\n"
        "Выбери действие:",
        reply_markup=menu_keyboard()
    )


# =========================================================
# /id
# =========================================================

@dp.message(Command("id"))
async def id_handler(message: Message):

    await message.answer(
        f"<code>{message.chat.id}</code>"
    )


# =========================================================
# NEW GAME
# =========================================================

@dp.callback_query(F.data == "new_game")
async def new_game(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "chat_id"
    }

    await callback.message.answer(
        "▶️ <b>Новая игра</b>\n\n"
        "Напиши ID чата, где будет проходить игра.\n\n"
        "Например:\n"
        "<code>-1003331067394</code>"
    )

    await callback.answer()


# =========================================================
# ADMIN PRIVATE MESSAGES
# =========================================================

@dp.message(
    lambda message:
    message.chat.type == "private"
    and message.from_user.id in ADMIN_IDS
)
async def admin_private_message(message: Message):

    text = (
        message.text.strip()
        if message.text
        else ""
    )

    if not text:
        return

    user_id = message.from_user.id

    # -----------------------------------------------------
    # НЕТ АКТИВНОГО ДИАЛОГА С НАСТРОЙКОЙ
    # -----------------------------------------------------

    if user_id not in pending_games:
        return

    data = pending_games[user_id]

    step = data["step"]

    # -----------------------------------------------------
    # CHAT ID
    # -----------------------------------------------------

    if step == "chat_id":

        try:
            chat_id = int(text)
        except ValueError:

            await message.answer(
                "ID чата должен быть числом."
            )

            return

        data["chat_id"] = chat_id
        data["step"] = "duration"

        await message.answer(
            "⏱ Теперь напиши длительность игры "
            "в минутах.\n\n"
            "Например:\n"
            "<code>10</code>"
        )

        return

    # -----------------------------------------------------
    # DURATION
    # -----------------------------------------------------

    if step == "duration":

        if not text.isdigit():

            await message.answer(
                "Напиши длительность числом.\n"
                "Например: <code>10</code>"
            )

            return

        minutes = int(text)

        if minutes <= 0:

            await message.answer(
                "Длительность должна быть больше 0."
            )

            return

        data["duration"] = minutes * 60
        data["step"] = "prize"

        await message.answer(
            "🎁 Теперь напиши приз для победителя."
        )

        return

    # -----------------------------------------------------
    # PRIZE
    # -----------------------------------------------------

    if step == "prize":

        data["prize"] = text
        data["step"] = "description"

        await message.answer(
            "✏️ Теперь напиши описание игры.\n\n"
            "Если описание не нужно — напиши <code>-</code>."
        )

        return

    # -----------------------------------------------------
    # DESCRIPTION
    # -----------------------------------------------------

    if step == "description":

        data["description"] = (
            ""
            if text == "-"
            else text
        )

        chat_id = data["chat_id"]
        duration = data["duration"]
        prize = data["prize"]
        description = data["description"]

        # Проверяем, что бот может работать в этом чате.
        try:

            chat = await bot.get_chat(chat_id)

            if chat.type not in (
                "group",
                "supergroup"
            ):
                await message.answer(
                    "❌ Этот ID не принадлежит группе."
                )

                pending_games.pop(
                    user_id,
                    None
                )

                return

        except Exception as e:

            print(
                "Chat check error:",
                e
            )

            await message.answer(
                "❌ Не удалось найти этот чат.\n\n"
                "Проверь ID и убедись, что бот "
                "добавлен в группу."
            )

            pending_games.pop(
                user_id,
                None
            )

            return

        # Сохраняем игру.
        update_game(
            chat_id,

            active=1,
            paused=0,

            duration=duration,
            remaining=duration,

            leader_id=None,
            leader_name=None,
            leader_username=None,

            prize=prize,
            description=description,

            started_at=None
        )

        pending_games.pop(
            user_id,
            None
        )

        # Текст запуска.
        game_text = (
            "🏁 <b>ИВЕНТ НА ПЕРЕБИВ</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"🎁 <b>Приз</b> → {prize}\n"
            f"⏱ <b>Длительность</b> → "
            f"{format_time(duration)}\n\n"
            "✏️ <i>Любое сообщение в чате "
            "перебивает лидера и обнуляет таймер.</i>\n\n"
            "🚫 <i>Сообщения админов чата "
            "не засчитываются.</i>"
        )

        if description:
            game_text += (
                "\n\n"
                f"{description}"
            )

        try:

            await bot.send_message(
                chat_id,
                game_text
            )

            await message.answer(
                "✅ Игра запущена.\n\n"
                f"Чат: <code>{chat_id}</code>\n"
                f"Длительность: "
                f"<b>{format_time(duration)}</b>"
            )

        except Exception as e:

            print(
                "Game start error:",
                e
            )

            update_game(
                chat_id,
                active=0
            )

            await message.answer(
                "❌ Не удалось отправить сообщение "
                "в группу.\n\n"
                "Проверь, что бот добавлен в группу "
                "и является администратором."
            )

        return


# =========================================================
# PAUSE
# =========================================================

@dp.callback_query(F.data == "pause_game")
async def pause_game(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "pause_chat_id"
    }

    await callback.message.answer(
        "⏸ Напиши ID чата, где нужно "
        "поставить игру на паузу."
    )

    await callback.answer()


# =========================================================
# RESUME
# =========================================================

@dp.callback_query(F.data == "resume_game")
async def resume_game(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "resume_chat_id"
    }

    await callback.message.answer(
        "▶️ Напиши ID чата, где нужно "
        "продолжить игру."
    )

    await callback.answer()


# =========================================================
# STOP
# =========================================================

@dp.callback_query(F.data == "stop_game")
async def stop_game(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "stop_chat_id"
    }

    await callback.message.answer(
        "⏹ Напиши ID чата, где нужно "
        "завершить игру."
    )

    await callback.answer()


# =========================================================
# STATUS
# =========================================================

@dp.callback_query(F.data == "status")
async def status(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "status_chat_id"
    }

    await callback.message.answer(
        "📊 Напиши ID чата, по которому "
        "показать статус."
    )

    await callback.answer()


# =========================================================
# WINNERS
# =========================================================

@dp.callback_query(F.data == "winners")
async def winners(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    pending_games[callback.from_user.id] = {
        "step": "winners_chat_id"
    }

    await callback.message.answer(
        "🏆 Напиши ID чата, по которому "
        "показать победителей."
    )

    await callback.answer()


# =========================================================
# ADMIN COMMANDS BY CHAT ID
# =========================================================

async def handle_admin_action(
    message: Message,
    data: dict
):
    text = (
        message.text.strip()
        if message.text
        else ""
    )

    step = data["step"]

    try:
        chat_id = int(text)
    except ValueError:

        await message.answer(
            "ID чата должен быть числом."
        )

        return True

    game = get_game(chat_id)

    # -----------------------------------------------------
    # PAUSE
    # -----------------------------------------------------

    if step == "pause_chat_id":

        if not game["active"]:
            await message.answer(
                "⏸ В этом чате игра не запущена."
            )
            return True

        if game["paused"]:
            await message.answer(
                "Игра уже стоит на паузе."
            )
            return True

        remaining = get_remaining(game)

        update_game(
            chat_id,
            paused=1,
            remaining=remaining,
            started_at=None
        )

        await message.answer(
            "⏸ Игра поставлена на паузу.\n"
            f"Осталось: <b>{format_time(remaining)}</b>"
        )

        return True

    # -----------------------------------------------------
    # RESUME
    # -----------------------------------------------------

    if step == "resume_chat_id":

        if not game["active"]:
            await message.answer(
                "▶️ В этом чате игра не запущена."
            )
            return True

        if not game["paused"]:
            await message.answer(
                "Игра уже запущена."
            )
            return True

        update_game(
            chat_id,
            paused=0,
            started_at=time.time()
        )

        await message.answer(
            "▶️ Игра продолжена."
        )

        return True

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

    if step == "stop_chat_id":

        update_game(
            chat_id,

            active=0,
            paused=0,

            leader_id=None,
            leader_name=None,
            leader_username=None,

            started_at=None
        )

        await message.answer(
            "⏹ Игра завершена."
        )

        return True

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if step == "status_chat_id":

        if not game["active"]:

            await message.answer(
                "📊 Игра в этом чате не запущена."
            )

            return True

        remaining = get_remaining(game)

        await message.answer(
            "📊 <b>Статус игры</b>\n\n"
            f"👑 Лидер: "
            f"<b>{leader_name(game)}</b>\n"
            f"⏳ Осталось: "
            f"<b>{format_time(remaining)}</b>\n"
            f"🎁 Приз: "
            f"<b>{game['prize'] or '—'}</b>"
        )

        return True

    # -----------------------------------------------------
    # WINNERS
    # -----------------------------------------------------

    if step == "winners_chat_id":

        rows = get_winners(chat_id)

        if not rows:

            await message.answer(
                "🏆 В этом чате победителей пока нет."
            )

            return True

        text = (
            "🏆 <b>Последние победители</b>\n\n"
        )

        for index, row in enumerate(
            rows,
            start=1
        ):

            name = (
                f"@{row[1]}"
                if row[1]
                else row[0]
            )

            text += (
                f"{index}. <b>{name}</b> — "
                f"{format_time(row[2])}\n"
            )

            if row[3]:
                text += (
                    f"🎁 {row[3]}\n"
                )

            text += "\n"

        await message.answer(text)

        return True

    return False


# =========================================================
# GROUP MESSAGES — ПЕРЕБИВ
# =========================================================

@dp.message(
    F.chat.type.in_(
        {"group", "supergroup"}
    )
)
async def game_message(message: Message):

    chat_id = message.chat.id

    game = get_game(chat_id)

    # Игра в этом чате не запущена.
    if not game["active"]:
        return

    # Игра на паузе.
    if game["paused"]:
        return

    user_id = message.from_user.id

    # -----------------------------------------------------
    # АДМИНИСТРАТОРЫ НЕ ПЕРЕБИВАЮТ
    # -----------------------------------------------------

    try:

        member = await bot.get_chat_member(
            chat_id,
            user_id
        )

        if member.status in (
            "administrator",
            "creator"
        ):
            return

    except Exception as e:

        print(
            "get_chat_member error:",
            e
        )

        return

    # -----------------------------------------------------
    # ТЕКУЩИЙ ЛИДЕР НЕ МОЖЕТ ПЕРЕБИТЬ САМ СЕБЯ
    # -----------------------------------------------------

    current_leader_id = game["leader_id"]

    if current_leader_id is not None:

        if int(current_leader_id) == int(user_id):
            return

    # -----------------------------------------------------
    # НОВЫЙ ЛИДЕР
    # -----------------------------------------------------

    leader_name_value = (
        message.from_user.full_name
    )

    leader_username_value = (
        message.from_user.username
    )

    duration = game["duration"]

    update_game(
        chat_id,

        active=1,
        paused=0,

        remaining=duration,

        leader_id=user_id,
        leader_name=leader_name_value,
        leader_username=leader_username_value,

        started_at=time.time()
    )

    if leader_username_value:

        username = (
            f"@{leader_username_value}"
        )

    else:

        username = leader_name_value

    await message.reply(
        "📢 <b>Перебито!</b>\n"
        f"👑 Новый лидер: "
        f"<b>{username}</b>\n"
        f"⏱ До победы: "
        f"<b>{format_time(duration)}</b>"
    )


# =========================================================
# TIMER
# =========================================================

async def timer_loop():

    while True:

        try:

            conn = db()
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                """
                SELECT *
                FROM games
                WHERE active = 1
                AND paused = 0
                AND leader_id IS NOT NULL
                AND started_at IS NOT NULL
                """
            ).fetchall()

            conn.close()

            for row in rows:

                game = dict(row)

                chat_id = game["chat_id"]

                remaining = get_remaining(game)

                # -------------------------------------------------
                # ПОБЕДА
                # -------------------------------------------------

                if remaining <= 0:

                    username = leader_name(game)

                    add_winner(
                        chat_id,
                        game["leader_id"],
                        game["leader_name"],
                        game["leader_username"],
                        game["duration"],
                        game["prize"]
                    )

                    update_game(
                        chat_id,

                        active=0,
                        paused=0,

                        leader_id=None,
                        leader_name=None,
                        leader_username=None,

                        remaining=game["duration"],

                        started_at=None
                    )

                    await bot.send_message(
                        chat_id,

                        "🏆 <b>ПОБЕДИТЕЛЬ!</b>\n\n"
                        f"👑 <b>{username}</b>\n"
                        f"⏱ Продержался "
                        f"<b>{format_time(game['duration'])}</b> "
                        "без перебива!"
                    )

        except Exception as e:

            print(
                "Timer error:",
                e
            )

        await asyncio.sleep(
            TIMER_UPDATE_SECONDS
        )


# =========================================================
# WEB SERVER FOR RENDER
# =========================================================

async def health(request):

    return web.Response(
        text="Perebiv bot is running!"
    )


async def start_web_server():

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
# MAIN
# =========================================================

async def main():

    init_db()

    await start_web_server()

    asyncio.create_task(
        timer_loop()
    )

    print("Bot started.")

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
