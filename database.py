import sqlite3
import threading
import time


class Database:
    def __init__(self, path: str = "bot.db"):
        self.path = path
        self.lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS game (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        chat_id INTEGER NOT NULL,
                        duration INTEGER NOT NULL,
                        remaining INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        started_at REAL,
                        leader_id INTEGER,
                        leader_name TEXT,
                        leader_username TEXT,
                        prize_html TEXT,
                        description_html TEXT,
                        timer_message_id INTEGER,
                        preview_url TEXT,
                        created_at REAL NOT NULL
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS winners (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        username TEXT,
                        prize_html TEXT,
                        duration INTEGER NOT NULL,
                        won_at REAL NOT NULL
                    )
                """)

                conn.commit()
            finally:
                conn.close()

    def get_game(self):
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM game WHERE id = 1"
                ).fetchone()

                return dict(row) if row else None
            finally:
                conn.close()

    def create_game(
        self,
        chat_id: int,
        duration: int,
        prize_html: str,
        description_html: str,
        preview_url: str | None,
    ):
        with self.lock:
            conn = self._connect()

            try:
                conn.execute("DELETE FROM game")

                conn.execute("""
                    INSERT INTO game (
                        id,
                        chat_id,
                        duration,
                        remaining,
                        state,
                        started_at,
                        leader_id,
                        leader_name,
                        leader_username,
                        prize_html,
                        description_html,
                        timer_message_id,
                        preview_url,
                        created_at
                    )
                    VALUES (
                        1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, (
                    chat_id,
                    duration,
                    duration,
                    "waiting",
                    None,
                    None,
                    None,
                    None,
                    prize_html,
                    description_html,
                    None,
                    preview_url,
                    time.time(),
                ))

                conn.commit()
            finally:
                conn.close()

    def set_timer_message(self, message_id: int):
        self.update(
            timer_message_id=message_id
        )

    def set_leader(
        self,
        user_id: int,
        name: str,
        username: str | None,
        duration: int,
    ):
        now = time.time()

        self.update(
            remaining=duration,
            state="active",
            started_at=now,
            leader_id=user_id,
            leader_name=name,
            leader_username=username,
        )

    def pause(self, remaining: int):
        self.update(
            remaining=max(0, remaining),
            state="paused",
            started_at=None,
        )

    def resume(self, remaining: int):
        self.update(
            remaining=max(0, remaining),
            state="active",
            started_at=time.time(),
        )

    def set_state(self, state: str):
        self.update(state=state)

    def set_remaining(self, remaining: int):
        self.update(
            remaining=max(0, remaining),
            started_at=time.time(),
        )

    def update(self, **fields):
        if not fields:
            return

        allowed = {
            "chat_id",
            "duration",
            "remaining",
            "state",
            "started_at",
            "leader_id",
            "leader_name",
            "leader_username",
            "prize_html",
            "description_html",
            "timer_message_id",
            "preview_url",
        }

        fields = {
            key: value
            for key, value in fields.items()
            if key in allowed
        }

        if not fields:
            return

        with self.lock:
            conn = self._connect()

            try:
                columns = ", ".join(
                    f"{key} = ?"
                    for key in fields
                )

                values = list(fields.values())

                conn.execute(
                    f"UPDATE game SET {columns} WHERE id = 1",
                    values,
                )

                conn.commit()
            finally:
                conn.close()

    def clear_game(self):
        with self.lock:
            conn = self._connect()

            try:
                conn.execute("DELETE FROM game WHERE id = 1")
                conn.commit()
            finally:
                conn.close()

    def add_winner(
        self,
        chat_id: int,
        user_id: int,
        name: str,
        username: str | None,
        prize_html: str,
        duration: int,
    ):
        with self.lock:
            conn = self._connect()

            try:
                conn.execute("""
                    INSERT INTO winners (
                        chat_id,
                        user_id,
                        name,
                        username,
                        prize_html,
                        duration,
                        won_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    chat_id,
                    user_id,
                    name,
                    username,
                    prize_html,
                    duration,
                    time.time(),
                ))

                conn.commit()
            finally:
                conn.close()

    def get_winners(self, chat_id: int, limit: int = 10):
        with self.lock:
            conn = self._connect()

            try:
                rows = conn.execute("""
                    SELECT *
                    FROM winners
                    WHERE chat_id = ?
                    ORDER BY won_at DESC
                    LIMIT ?
                """, (
                    chat_id,
                    limit,
                )).fetchall()

                return [dict(row) for row in rows]
            finally:
                conn.close()
