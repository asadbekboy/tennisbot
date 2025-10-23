import sqlite3
import time
import json
from typing import List, Dict, Any, Tuple

DEFAULT_RATING = 1000

class Database:
    """Класс для управления базой данных SQLite."""
    def __init__(self, db_name='bot.db'):
        self.db_name = db_name
        self.init_db()

    def get_conn(self):
        """Возвращает соединение и курсор."""
        conn = sqlite3.connect(self.db_name)
        # Устанавливаем row_factory для доступа к столбцам по имени
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Инициализирует таблицы базы данных."""
        conn = self.get_conn()
        cursor = conn.cursor()

        # Таблица игроков
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                rating REAL NOT NULL DEFAULT ?,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                full_name TEXT
            )
        """, (DEFAULT_RATING,))

        # Таблица для сопоставления Telegram ID с username
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_mapping (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL
            )
        """)
        
        # Таблица завершенных матчей (история)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL, 
                winner_ids TEXT NOT NULL, 
                loser_ids TEXT NOT NULL, 
                score TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

        # Таблица ожидающих подтверждения матчей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_matches (
                id INTEGER PRIMARY KEY,
                match_type TEXT NOT NULL,
                participants TEXT NOT NULL,
                winner_ids TEXT NOT NULL,
                loser_ids TEXT NOT NULL,
                score TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def update_user_mapping(self, user_id: int, username: str):
        """Обновляет или добавляет сопоставление username -> ID."""
        conn = self.get_conn()
        cursor = conn.cursor()
        # ON CONFLICT используется для обновления user_id, если username уже существует
        cursor.execute(
            "INSERT INTO user_mapping (username, user_id) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET user_id=?",
            (username, user_id, user_id)
        )
        conn.commit()
        conn.close()

    def get_user_id_by_tag(self, username: str) -> int | None:
        """Получает ID по username (тегу)."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM user_mapping WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        return row['user_id'] if row else None

    def get_or_create_player(self, user_id: int, username: str, full_name: str) -> None:
        """Находит игрока по ID или создает нового."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM players WHERE id = ?", (user_id,))
        player = cursor.fetchone()

        if player:
            # Обновляем имя и username игрока, если он уже есть
            cursor.execute(
                "UPDATE players SET username = ?, full_name = ? WHERE id = ?",
                (username, full_name, user_id)
            )
        else:
            # Создаем нового игрока
            cursor.execute(
                "INSERT INTO players (id, username, full_name) VALUES (?, ?, ?)",
                (user_id, username, full_name)
            )
        
        # Обновляем сопоставление (username -> ID)
        self.update_user_mapping(user_id, username)

        conn.commit()
        conn.close()

    def get_player_rating(self, user_id: int) -> int:
        """Получает рейтинг игрока."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT rating FROM players WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        # Возвращаем округленный рейтинг
        return int(round(row['rating'])) if row and row['rating'] is not None else DEFAULT_RATING

    def update_player_rating(self, user_id: int, delta_rating: int):
        """Обновляет рейтинг и статистику игрока."""
        conn = self.get_conn()
        cursor = conn.cursor()
        
        is_winner = delta_rating > 0
        
        # Если победа или поражение
        stat_field = "wins" if is_winner else "losses"
        
        cursor.execute(
            f"UPDATE players SET rating = rating + ?, {stat_field} = {stat_field} + 1 WHERE id = ?",
            (delta_rating, user_id)
        )
        conn.commit()
        conn.close()

    def add_pending_match(self, match_type: str, participants: List[int], winner_ids: List[int], loser_ids: List[int], score: str) -> int:
        """Добавляет заявку на подтверждение матча."""
        conn = self.get_conn()
        cursor = conn.cursor()

        # Преобразуем списки ID в строки для хранения
        participants_str = ",".join(map(str, participants))
        winner_ids_str = ",".join(map(str, winner_ids))
        loser_ids_str = ",".join(map(str, loser_ids))
        
        cursor.execute(
            "INSERT INTO pending_matches (match_type, participants, winner_ids, loser_ids, score, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (match_type, participants_str, winner_ids_str, loser_ids_str, score, time.time())
        )
        match_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return match_id
        
    def get_pending_match(self, match_id: int) -> Tuple[str, str, str, str] | None:
        """Получает заявку по ID."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT match_type, winner_ids, loser_ids, score FROM pending_matches WHERE id = ?", (match_id,))
        row = cursor.fetchone()
        conn.close()
        return tuple(row) if row else None
        
    def delete_pending_match(self, match_id: int):
        """Удаляет заявку."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_matches WHERE id = ?", (match_id,))
        conn.commit()
        conn.close()

    def finalize_match(self, match_id: int, winner_ids: List[int], loser_ids: List[int], score: str, match_type: str):
        """Перемещает матч из временной таблицы в историю."""
        conn = self.get_conn()
        cursor = conn.cursor()
        
        winner_ids_str = ",".join(map(str, winner_ids))
        loser_ids_str = ",".join(map(str, loser_ids))
        
        cursor.execute(
            "INSERT INTO matches (type, winner_ids, loser_ids, score, timestamp) VALUES (?, ?, ?, ?, ?)",
            (match_type, winner_ids_str, loser_ids_str, score, time.time())
        )
        
        # Удаляем из временной таблицы
        self.delete_pending_match(match_id)
        
        conn.commit()
        conn.close()

    def get_leaderboard(self) -> List[Tuple[str, int, int, int]]:
        """Получает топ игроков."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT username, rating, wins, losses FROM players ORDER BY rating DESC")
        rows = cursor.fetchall()
        conn.close()
        # Преобразуем рейтинг в int
        return [(r['username'], int(round(r['rating'])), r['wins'], r['losses']) for r in rows]

    def get_player_stats(self, user_id: int) -> Tuple[str, int, int, int] | None:
        """Получает статистику игрока по ID."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT username, rating, wins, losses FROM players WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            # Округляем рейтинг
            return row['username'], int(round(row['rating'])), row['wins'], row['losses']
        return None

    def get_match_history(self, limit: int = 10) -> List[Tuple]:
        """Получает последние N матчей."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, type, winner_ids, loser_ids, score, timestamp FROM matches ORDER BY timestamp DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [(r['id'], r['type'], r['winner_ids'], r['loser_ids'], r['score'], r['timestamp']) for r in rows]

    def delete_match_by_id(self, match_id: int) -> bool:
        """Удаляет матч из истории."""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        deleted_rows = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted_rows > 0
