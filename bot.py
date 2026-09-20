import asyncio
import os
import sqlite3
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}
TIMER_UPDATE_SECONDS = int(os.getenv("TIMER_UPDATE_SECONDS", "20"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_IDS")


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

DB_FILE = "perebiv.db"


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
            timer_message_id INTEGER,
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
        "SELECT * FROM games WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()

    if row is None:
        conn.execute("""
            INSERT INTO games
            (chat_id, active, paused, duration, remaining)
            VALUES (?, 0, 0, 600, 600)
        """, (chat_id,))
        conn.commit()

        row = conn.execute(
            "SELECT * FROM games WHERE chat_id = ?",
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
        SET {', '.join(fields)}
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

    conn.execute("""
        INSERT INTO winners
        (chat_id, user_id, name, username, duration, prize, won_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        user_id,
        name,
        username,
        duration,
        prize,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


def get_winners(chat_id):
    conn = db()

    rows = conn.execute("""
        SELECT name, username, duration, prize, won_at
        FROM winners
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (chat_id,)).fetchall()

    conn.close()

    return rows


def format_time(seconds):
    seconds = max(0, int(seconds))

    minutes = seconds // 60
    seconds = seconds % 60

    return f"{minutes:02d}:{seconds:02d}"


def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


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


@dp.message(Command("id"))
async def id_handler(message: Message):
    await message.answer(
        f"<code>{message.chat.id}</code>"
    )


@dp.callback_query(F.data == "new_game")
async def new_game(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "▶️ <b>Новая игра</b>\n\n"
        "Сначала напиши ID чата, где будет проходить игра.\n\n"
        "Например:\n"
        "<code>-1003331067394</code>"
    )

    await callback.answer()


# Временное хранилище настроек новой игры.
pending_games = {}


@dp.message(
    lambda message:
    message.chat.type == "private"
    and message.from_user.id in ADMIN_IDS
)
async def admin_private_message(message: Message):
    text = message.text.strip() if message.text else ""

    if not text:
        return

    user_id = message.from_user.id

    # Шаг выбора чата.
    if user_id not in pending_games:
        try:
            chat_id = int(text)
        except ValueError:
            return

        pending_games[user_id] = {
            "chat_id": chat_id
        }

        await message.answer(
            "⏱ Теперь напиши длительность игры в минутах.\n\n"
            "Например:\n"
            "<code>10</code>"
        )

        return

    data = pending_games[user_id]

    # Шаг выбора длительности.
    if "duration" not in data:
        if not text.isdigit():
            await message.answer(
                "Напиши длительность числом, например <code>10</code>."
            )
            return

        minutes = int(text)

        if minutes <= 0:
            await message.answer(
                "Длительность должна быть больше 0 минут."
            )
            return

        data["duration"] = minutes * 60

        await message.answer(
            "🎁 Теперь напиши приз для победителя."
        )

        return

    # Шаг выбора приза.
    if "prize" not in data:
        data["prize"] = text

        await message.answer(
            "✏️ Теперь напиши описание игры.\n\n"
            "Если описание не нужно — напиши <code>-</code>."
        )

        return

    # Шаг описания.
    data["description"] = "" if text == "-" else text

    chat_id = data["chat_id"]
    duration = data["duration"]
    prize = data["prize"]
    description = data["description"]

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
        timer_message_id=None,
        started_at=None
    )

    pending_games.pop(user_id, None)

    try:
        await bot.send_message(
            chat_id,
            "🏁 <b>ИВЕНТ НА ПЕРЕБИВ</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            f"🎁 <b>Приз</b> → {prize}\n"
            f"⏱ <b>Длительность</b> → "
            f"{format_time(duration)}\n\n"
            "✏️ <i>Любое сообщение в чате "
            "перебивает лидера и обнуляет таймер.</i>\n\n"
            "🚫 <i>Сообщения админов чата "
            "не засчитываются.</i>\n\n"
            f"{description}"
        )

        await message.answer(
            "✅ Игра запущена в чате:\n"
            f"<code>{chat_id}</code>"
        )

    except Exception as e:
        update_game(chat_id, active=0)

        await message.answer(
            "❌ Не удалось отправить сообщение в этот чат.\n\n"
            "Проверь, что бот добавлен туда и является администратором."
        )

        print("Start game error:", e)


@dp.callback_query(F.data == "pause_game")
async def pause_game(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "⏸ Напиши ID чата, где нужно поставить игру на паузу."
    )

    await callback.answer()


@dp.callback_query(F.data == "resume_game")
async def resume_game(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "▶️ Напиши ID чата, где нужно продолжить игру."
    )

    await callback.answer()


@dp.callback_query(F.data == "stop_game")
async def stop_game(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "⏹ Напиши ID чата, где нужно завершить игру."
    )

    await callback.answer()


@dp.callback_query(F.data == "status")
async def status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "📊 Напиши ID чата, по которому показать статус."
    )

    await callback.answer()


@dp.callback_query(F.data == "winners")
async def winners(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "🏆 Напиши ID чата, по которому показать победителей."
    )

    await callback.answer()


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def game_message(message: Message):
    chat_id = message.chat.id

    game = get_game(chat_id)

    if not game["active"]:
        return

    if game["paused"]:
        return

    user_id = message.from_user.id

    # Администраторы группы не участвуют в перебиве.
    try:
        member = await bot.get_chat_member(
            chat_id,
            user_id
        )

        if member.status in ("administrator", "creator"):
            return

    except Exception as e:
        print("get_chat_member error:", e)
        return

    current_leader_id = game["leader_id"]

    # ВАЖНО:
    # сообщение текущего лидера НЕ считается перебивом.
    if current_leader_id is not None:
        if int(current_leader_id) == int(user_id):
            return

    # Первый участник или другой участник
    # становится новым лидером.
    leader_name = message.from_user.full_name
    leader_username = message.from_user.username

    duration = game["duration"]

    update_game(
        chat_id,
        remaining=duration,
        leader_id=user_id,
        leader_name=leader_name,
        leader_username=leader_username,
        started_at=time.time()
    )

    username = (
        f"@{leader_username}"
        if leader_username
        else leader_name
    )

    await message.reply(
        "📢 <b>Перебито!</b>\n"
        f"👑 Новый лидер: <b>{username}</b>\n"
        f"⏱ До победы: <b>{format_time(duration)}</b>"
    )


async def timer_loop():
    while True:
        try:
            conn = db()
            conn.row_factory = sqlite3.Row

            games = conn.execute("""
                SELECT * FROM games
                WHERE active = 1
                AND paused = 0
                AND leader_id IS NOT NULL
                AND started_at IS NOT NULL
            """).fetchall()

            conn.close()

            for row in games:
                game = dict(row)

                chat_id = game["chat_id"]

                elapsed = int(
                    time.time() - game["started_at"]
                )

                remaining = max(
                    0,
                    game["remaining"] - elapsed
                )

                if remaining <= 0:
                    username = (
                        f"@{game['leader_username']}"
                        if game["leader_username"]
                        else game["leader_name"]
                    )

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
            print("Timer error:", e)

        await asyncio.sleep(TIMER_UPDATE_SECONDS)


async def main():
    init_db()

    asyncio.create_task(timer_loop())

    print("Bot started.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
