import aiosqlite
import glob
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from config import DB_PATH, DEFAULT_MODEL, DEFAULT_WORKING_DIR, AUTO_APPROVE, normalize_model_name

logger = logging.getLogger(__name__)

def generate_topic(prompt: str) -> str:
    """Extract a clean, readable topic from the prompt."""
    lines = [l.strip() for l in prompt.split("\n") if l.strip()]
    if not lines:
        return "Новый диалог"
    first_line = lines[0]
    # Remove leading command characters
    first_line = re.sub(r'^[/#!\s]+', '', first_line)
    # Remove markdown formatting chars
    first_line = re.sub(r'[*_`#~\[\]]', '', first_line).strip()
    if len(first_line) > 40:
        first_line = first_line[:37] + "..."
    return first_line or "Диалог"

def normalize_session_key(key_or_id: Any, chat_id: Optional[int] = None, thread_id: Optional[int] = None) -> str:
    """Returns normalized string key 'chat_id:thread_id'."""
    if chat_id is not None:
        eff_thread = thread_id if (thread_id and thread_id > 0) else 0
        return f"{chat_id}:{eff_thread}"
    s = str(key_or_id)
    if ":" in s:
        return s
    return f"{s}:0"

@dataclass
class UserSession:
    session_key: str
    user_id: int
    chat_id: int
    thread_id: Optional[int]
    conversation_id: Optional[str]
    topic_name: str
    model: str
    working_dir: str
    auto_approve: bool
    effort: str = "high"
    execution_mode: str = "accept-edits"

    @property
    def is_forum_topic(self) -> bool:
        return bool(self.thread_id and self.thread_id > 0)

class SessionManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Main sessions table keyed by session_key (chat_id:thread_id)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_key TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    thread_id INTEGER DEFAULT 0,
                    user_id INTEGER,
                    conversation_id TEXT,
                    topic_name TEXT,
                    model TEXT,
                    working_dir TEXT,
                    auto_approve INTEGER DEFAULT 1,
                    effort TEXT DEFAULT 'high',
                    execution_mode TEXT DEFAULT 'accept-edits',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Conversation history with topic, thread_id, and session_key
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    chat_id INTEGER,
                    thread_id INTEGER DEFAULT 0,
                    session_key TEXT,
                    conversation_id TEXT UNIQUE,
                    topic TEXT,
                    last_prompt TEXT,
                    last_response TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Active session pointer for private chat projects
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_active_sessions (
                    user_id INTEGER PRIMARY KEY,
                    active_session_key TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: copy legacy rows from user_sessions if table exists
            try:
                await db.execute("""
                    INSERT OR IGNORE INTO sessions (
                        session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                        model, working_dir, auto_approve, effort, execution_mode, updated_at
                    )
                    SELECT 
                        CAST(user_id AS TEXT) || ':0',
                        user_id,
                        0,
                        user_id,
                        conversation_id,
                        'AntigravityTG',
                        model,
                        working_dir,
                        auto_approve,
                        effort,
                        execution_mode,
                        updated_at
                    FROM user_sessions
                """)
            except Exception:
                pass

            # Migration: Ensure columns in conversation_history
            for col, col_type in [
                ("session_key", "TEXT"),
                ("chat_id", "INTEGER"),
                ("thread_id", "INTEGER DEFAULT 0"),
                ("topic", "TEXT"),
                ("last_prompt", "TEXT"),
                ("last_response", "TEXT")
            ]:
                try:
                    await db.execute(f"ALTER TABLE conversation_history ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            # Migration: Ensure columns in sessions
            for col, col_type in [
                ("topic_name", "TEXT"),
                ("effort", "TEXT DEFAULT 'high'"),
                ("execution_mode", "TEXT DEFAULT 'accept-edits'")
            ]:
                try:
                    await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            # Migration: Update any legacy 'Основной' to 'AntigravityTG'
            try:
                await db.execute("""
                    UPDATE sessions
                    SET topic_name = 'AntigravityTG'
                    WHERE topic_name IN ('Основной', 'Основной диалог')
                       OR (session_key LIKE '%:0' AND (topic_name IS NULL OR topic_name = '' OR topic_name IN ('Основной', 'Основной диалог')))
                """)
            except Exception:
                pass

            # Migration: Normalize model names (remove legacy effort suffixes)
            try:
                for suffix in ("-high", "-medium", "-low"):
                    await db.execute(f"""
                        UPDATE sessions
                        SET model = SUBSTR(model, 1, LENGTH(model) - {len(suffix)})
                        WHERE model LIKE 'gemini-%{suffix}' OR model LIKE 'gpt-oss-%{suffix}'
                    """)
            except Exception:
                pass

            await db.commit()

    async def get_session(
        self,
        user_id: int,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
        topic_title: Optional[str] = None,
        session_key: Optional[str] = None
    ) -> UserSession:
        """Retrieves or creates a session for the specified chat/thread/session_key."""
        c_id = chat_id if chat_id is not None else user_id
        eff_thread = thread_id if (thread_id and thread_id > 0) else 0
        if session_key:
            s_key = session_key
        else:
            s_key = await self.get_active_session_key(user_id, chat_id, thread_id)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                       model, working_dir, auto_approve, effort, execution_mode
                FROM sessions WHERE session_key = ?
                """,
                (s_key,)
            )
            row = await cursor.fetchone()
            if row:
                return UserSession(
                    session_key=s_key,
                    user_id=row["user_id"] or user_id,
                    chat_id=row["chat_id"] or c_id,
                    thread_id=thread_id if eff_thread > 0 else None,
                    conversation_id=row["conversation_id"],
                    topic_name=row["topic_name"] or ("AntigravityTG" if eff_thread == 0 else f"Проект #{eff_thread}"),
                    model=normalize_model_name(row["model"]) if row["model"] else DEFAULT_MODEL,
                    working_dir=row["working_dir"] or DEFAULT_WORKING_DIR,
                    auto_approve=bool(row["auto_approve"]),
                    effort=row["effort"] or "high",
                    execution_mode=row["execution_mode"] or "accept-edits"
                )

            # Not found -> create new session record
            target_working_dir = DEFAULT_WORKING_DIR
            target_topic = topic_title
            try:
                p_dir = Path(r"C:\Users\Chatty\Projects")
                if p_dir.exists() and p_dir.is_dir():
                    search_slugs = []
                    if topic_title:
                        search_slugs.append(topic_title.lower())
                    if ":" in s_key:
                        search_slugs.append(s_key.split(":")[-1].lower())
                    for sub in p_dir.iterdir():
                        if sub.is_dir() and sub.name.lower() in search_slugs:
                            target_working_dir = str(sub.resolve())
                            if not target_topic:
                                target_topic = sub.name
                            break
            except Exception:
                pass

            default_topic = target_topic or ("AntigravityTG" if eff_thread == 0 else f"Проект #{eff_thread}")
            new_session = UserSession(
                session_key=s_key,
                user_id=user_id,
                chat_id=c_id,
                thread_id=thread_id if eff_thread > 0 else None,
                conversation_id=None,
                topic_name=default_topic,
                model=DEFAULT_MODEL,
                working_dir=target_working_dir,
                auto_approve=AUTO_APPROVE,
                effort="high",
                execution_mode="accept-edits"
            )
            await db.execute(
                """
                INSERT INTO sessions (
                    session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                    model, working_dir, auto_approve, effort, execution_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s_key,
                    c_id,
                    eff_thread,
                    user_id,
                    None,
                    default_topic,
                    new_session.model,
                    new_session.working_dir,
                    1 if new_session.auto_approve else 0,
                    new_session.effort,
                    new_session.execution_mode
                )
            )
            await db.commit()
            return new_session

    async def update_conversation_id(self, key_or_user_id: Any, conversation_id: Optional[str]):
        s_key = normalize_session_key(key_or_id=key_or_user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE sessions SET conversation_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_key = ?
                """,
                (conversation_id, s_key)
            )
            await db.commit()

    async def save_turn(
        self,
        session_key: str,
        user_id: int,
        conversation_id: str,
        prompt: str,
        response: str,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
        topic_title: Optional[str] = None
    ):
        """Saves a conversation turn, records the topic, last prompt, and last response."""
        s_key = normalize_session_key(session_key, chat_id, thread_id)
        c_id = chat_id if chat_id is not None else user_id
        eff_thread = thread_id if (thread_id and thread_id > 0) else 0
        topic = topic_title or generate_topic(prompt)

        async with aiosqlite.connect(self.db_path) as db:
            # Update session record
            await db.execute(
                """
                UPDATE sessions SET
                    conversation_id = ?,
                    topic_name = COALESCE(topic_name, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_key = ?
                """,
                (conversation_id, topic, s_key)
            )

            # Insert or update conversation history
            await db.execute(
                """
                INSERT INTO conversation_history (
                    user_id, chat_id, thread_id, session_key, conversation_id,
                    topic, preview, last_prompt, last_response, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    topic = COALESCE(conversation_history.topic, excluded.topic),
                    last_prompt = excluded.last_prompt,
                    last_response = excluded.last_response,
                    session_key = excluded.session_key,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, c_id, eff_thread, s_key, conversation_id, topic, topic, prompt, response)
            )
            await db.commit()

    async def get_conversation_details(self, conversation_id: str) -> Optional[Dict[str, str]]:
        """Returns details for a conversation including topic, last prompt, and last response."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT topic, preview, last_prompt, last_response, strftime('%d.%m %H:%M', updated_at) as fmt_date
                FROM conversation_history WHERE conversation_id = ?
                """,
                (conversation_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "topic": row["topic"] or row["preview"] or "Диалог",
                    "last_prompt": row["last_prompt"] or "",
                    "last_response": row["last_response"] or "",
                    "date": row["fmt_date"]
                }
        return None

    async def get_active_topic(
        self,
        session_key: Any,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None
    ) -> str:
        """Returns the topic of the currently active session for the user/topic."""
        s_key = normalize_session_key(session_key, chat_id, thread_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT topic_name, conversation_id FROM sessions WHERE session_key = ?",
                (s_key,)
            )
            row = await cursor.fetchone()
            if row:
                if row["topic_name"]:
                    return row["topic_name"]
                if row["conversation_id"]:
                    details = await self.get_conversation_details(row["conversation_id"])
                    if details and details.get("topic"):
                        return details["topic"]
                    return f"Сессия {row['conversation_id'][:8]}"

        return "Новый диалог (не начат)"

    async def set_topic(self, session_key: Any, new_topic: str):
        """Allows renaming a topic/project name."""
        s_key = normalize_session_key(session_key)
        clean_topic = new_topic.strip()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET topic_name = ?, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (clean_topic, s_key)
            )
            cursor = await db.execute("SELECT conversation_id FROM sessions WHERE session_key = ?", (s_key,))
            row = await cursor.fetchone()
            if row and row[0]:
                await db.execute(
                    "UPDATE conversation_history SET topic = ?, updated_at = CURRENT_TIMESTAMP WHERE conversation_id = ?",
                    (clean_topic, row[0])
                )
            await db.commit()

    async def reset_conversation(self, session_key: Any):
        """Resets the conversation context for this specific session/topic."""
        s_key = normalize_session_key(session_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET conversation_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (s_key,)
            )
            await db.commit()

    async def get_user_conversations(
        self,
        user_id: int,
        session_key: Optional[str] = None,
        limit: int = 6
    ) -> List[Dict[str, str]]:
        """Returns recent conversations for the session or user."""
        conversations = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if session_key:
                s_key = normalize_session_key(session_key)
                cursor = await db.execute(
                    """
                    SELECT conversation_id, topic, preview, strftime('%d.%m %H:%M', updated_at) as fmt_date
                    FROM conversation_history
                    WHERE session_key = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (s_key, limit)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT conversation_id, topic, preview, strftime('%d.%m %H:%M', updated_at) as fmt_date
                    FROM conversation_history
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit)
                )
            rows = await cursor.fetchall()
            for r in rows:
                conversations.append({
                    "id": r["conversation_id"],
                    "topic": r["topic"] or r["preview"] or "Диалог",
                    "date": r["fmt_date"]
                })

        # Also discover on-disk conversations if fewer than limit
        if len(conversations) < limit:
            existing_ids = {c["id"] for c in conversations}
            sys_convs = self.get_system_conversations(limit=limit)
            for cid in sys_convs:
                if cid not in existing_ids:
                    conversations.append({
                        "id": cid,
                        "topic": f"Сессия {cid[:8]}...",
                        "date": "Архив"
                    })
                if len(conversations) >= limit:
                    break

        return conversations

    def get_system_conversations(self, limit: int = 6) -> List[str]:
        """Discovers recent conversations from ~/.gemini/antigravity-cli/conversations/."""
        try:
            conv_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity-cli\conversations")
            if not os.path.exists(conv_dir):
                return []
            db_files = glob.glob(os.path.join(conv_dir, "*.db"))
            db_files.sort(key=os.path.getmtime, reverse=True)
            result = []
            for f in db_files[:limit]:
                cid = os.path.splitext(os.path.basename(f))[0]
                result.append(cid)
            return result
        except Exception:
            return []

    async def set_model(self, session_key: Any, model: str):
        s_key = normalize_session_key(session_key)
        norm_model = normalize_model_name(model)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET model = ?, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (norm_model, s_key)
            )
            await db.commit()

    async def set_working_dir(self, session_key: Any, working_dir: str):
        s_key = normalize_session_key(session_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET working_dir = ?, conversation_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (working_dir, s_key)
            )
            await db.commit()

    async def set_auto_approve(self, session_key: Any, auto_approve: bool):
        s_key = normalize_session_key(session_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET auto_approve = ?, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (1 if auto_approve else 0, s_key)
            )
            await db.commit()

    async def set_effort(self, session_key: Any, effort: str):
        s_key = normalize_session_key(session_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET effort = ?, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (effort, s_key)
            )
            await db.commit()

    async def set_execution_mode(self, session_key: Any, mode: str):
        s_key = normalize_session_key(session_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET execution_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE session_key = ?",
                (mode, s_key)
            )
            await db.commit()

    async def get_all_sessions_for_chat(self, chat_id: int) -> List[UserSession]:
        """Returns all topic sessions created within a forum chat."""
        sessions = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                       model, working_dir, auto_approve, effort, execution_mode
                FROM sessions WHERE chat_id = ?
                ORDER BY updated_at DESC
                """,
                (chat_id,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                eff_thread = row["thread_id"]
                sessions.append(UserSession(
                    session_key=row["session_key"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    thread_id=eff_thread if eff_thread > 0 else None,
                    conversation_id=row["conversation_id"],
                    topic_name=row["topic_name"] or ("AntigravityTG" if eff_thread == 0 else f"Проект #{eff_thread}"),
                    model=normalize_model_name(row["model"]) if row["model"] else DEFAULT_MODEL,
                    working_dir=row["working_dir"] or DEFAULT_WORKING_DIR,
                    auto_approve=bool(row["auto_approve"]),
                    effort=row["effort"] or "high",
                    execution_mode=row["execution_mode"] or "accept-edits"
                ))
        return sessions

    async def get_active_session_key(self, user_id: int, chat_id: Optional[int] = None, thread_id: Optional[int] = None) -> str:
        """Returns the active session key for this user/chat."""
        if thread_id and thread_id > 0:
            return f"{chat_id}:{thread_id}"
        if chat_id is not None and chat_id != user_id:
            return f"{chat_id}:0"

        # Direct 1-on-1 private chat: check user_active_sessions
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT active_session_key FROM user_active_sessions WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row and row["active_session_key"]:
                return row["active_session_key"]

        return f"{user_id}:0"

    async def set_active_session_key(self, user_id: int, session_key: str):
        """Sets the active session key for a user in private chat."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_active_sessions (user_id, active_session_key, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    active_session_key = excluded.active_session_key,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, session_key)
            )
            await db.commit()

    async def get_session_by_key(self, session_key: str) -> Optional[UserSession]:
        """Retrieves session directly by its session_key."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                       model, working_dir, auto_approve, effort, execution_mode
                FROM sessions WHERE session_key = ?
                """,
                (session_key,)
            )
            row = await cursor.fetchone()
            if row:
                eff_thread = row["thread_id"]
                return UserSession(
                    session_key=row["session_key"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    thread_id=eff_thread if eff_thread > 0 else None,
                    conversation_id=row["conversation_id"],
                    topic_name=row["topic_name"] or "Диалог",
                    model=normalize_model_name(row["model"]) if row["model"] else DEFAULT_MODEL,
                    working_dir=row["working_dir"] or DEFAULT_WORKING_DIR,
                    auto_approve=bool(row["auto_approve"]),
                    effort=row["effort"] or "high",
                    execution_mode=row["execution_mode"] or "accept-edits"
                )
        return None

    async def switch_or_create_project(
        self,
        user_id: int,
        project_name: str,
        base_projects_dir: str = r"C:\Users\Chatty\Projects"
    ) -> Tuple[UserSession, bool, str]:
        """
        Switches or creates a project session for the user in 1-on-1 private chat.
        Automatically scans base_projects_dir for matching folder.
        Returns (session, was_created, working_dir).
        """
        clean_name = project_name.strip()
        if clean_name.lower() in ["main", "default", "основной", "0", "antigravitytg", "antigravity"]:
            session_key = f"{user_id}:0"
            existing = await self.get_session_by_key(session_key)
            if not existing:
                existing = await self.get_session(user_id=user_id, chat_id=user_id, thread_id=0, session_key=session_key, topic_title="AntigravityTG")
            await self.set_active_session_key(user_id, session_key)
            return existing, False, existing.working_dir

        slug = re.sub(r'[^a-zA-Z0-9_\-а-яА-ЯёЁ]', '_', clean_name).lower()
        if not slug:
            slug = "default"
        session_key = f"{user_id}:{slug}"

        # 1. Check if already exists in DB
        existing = await self.get_session_by_key(session_key)
        if existing:
            await self.set_active_session_key(user_id, session_key)
            return existing, False, existing.working_dir

        # 2. Check if folder exists on disk in Projects dir or auto-create it
        matched_dir = None
        try:
            p_dir = Path(base_projects_dir)
            if p_dir.exists() and p_dir.is_dir():
                for sub in p_dir.iterdir():
                    if sub.is_dir() and sub.name.lower() == clean_name.lower():
                        matched_dir = str(sub.resolve())
                        break
                if not matched_dir:
                    new_proj_dir = p_dir / clean_name
                    new_proj_dir.mkdir(parents=True, exist_ok=True)
                    matched_dir = str(new_proj_dir.resolve())
        except Exception:
            pass

        target_working_dir = matched_dir or str(DEFAULT_WORKING_DIR)

        # 3. Create new session record
        new_session = UserSession(
            session_key=session_key,
            user_id=user_id,
            chat_id=user_id,
            thread_id=None,
            conversation_id=None,
            topic_name=clean_name,
            model=DEFAULT_MODEL,
            working_dir=target_working_dir,
            auto_approve=AUTO_APPROVE,
            effort="high",
            execution_mode="accept-edits"
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sessions (
                    session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                    model, working_dir, auto_approve, effort, execution_mode
                ) VALUES (?, ?, 0, ?, NULL, ?, ?, ?, 1, 'high', 'accept-edits')
                """,
                (
                    session_key,
                    user_id,
                    user_id,
                    clean_name,
                    DEFAULT_MODEL,
                    target_working_dir
                )
            )
            await db.commit()

        await self.set_active_session_key(user_id, session_key)
        return new_session, True, target_working_dir

    async def get_all_projects_for_user(self, user_id: int) -> List[UserSession]:
        """Returns all virtual project sessions for a user in private chat."""
        # Ensure default session exists
        await self.get_session(user_id=user_id, chat_id=user_id, thread_id=0, session_key=f"{user_id}:0", topic_title="AntigravityTG")
        sessions = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_key, chat_id, thread_id, user_id, conversation_id, topic_name,
                       model, working_dir, auto_approve, effort, execution_mode
                FROM sessions WHERE user_id = ?
                ORDER BY (CASE WHEN thread_id > 0 THEN 0 ELSE 1 END), updated_at DESC
                """,
                (user_id,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                eff_thread = row["thread_id"]
                sessions.append(UserSession(
                    session_key=row["session_key"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    thread_id=eff_thread if eff_thread > 0 else None,
                    conversation_id=row["conversation_id"],
                    topic_name=row["topic_name"] or "AntigravityTG",
                    model=normalize_model_name(row["model"]) if row["model"] else DEFAULT_MODEL,
                    working_dir=row["working_dir"] or DEFAULT_WORKING_DIR,
                    auto_approve=bool(row["auto_approve"]),
                    effort=row["effort"] or "high",
                    execution_mode=row["execution_mode"] or "accept-edits"
                ))
        unique_sessions = []
        seen_dirs = set()
        seen_names = set()

        # First prioritize native forum topics
        for s in sessions:
            if s.thread_id and s.thread_id > 0:
                norm_dir = os.path.normpath(s.working_dir).lower()
                norm_name = s.topic_name.lower()
                unique_sessions.append(s)
                seen_dirs.add(norm_dir)
                seen_names.add(norm_name)

        # Then add base sessions if not already represented
        for s in sessions:
            if not s.thread_id or s.thread_id == 0:
                norm_dir = os.path.normpath(s.working_dir).lower()
                norm_name = s.topic_name.lower()
                if norm_dir not in seen_dirs and norm_name not in seen_names:
                    unique_sessions.append(s)
                    seen_dirs.add(norm_dir)
                    seen_names.add(norm_name)

        # Also discover folders in Projects directory if not already covered
        try:
            p_dir = Path(r"C:\Users\Chatty\Projects")
            if p_dir.exists() and p_dir.is_dir():
                for sub in p_dir.iterdir():
                    if sub.is_dir() and not sub.name.startswith("."):
                        norm_sub = os.path.normpath(str(sub.resolve())).lower()
                        if norm_sub not in seen_dirs and sub.name.lower() not in seen_names:
                            slug = re.sub(r'[^a-zA-Z0-9_\-а-яА-ЯёЁ]', '_', sub.name).lower()
                            s_key = f"{user_id}:{slug}"
                            unique_sessions.append(UserSession(
                                session_key=s_key,
                                user_id=user_id,
                                chat_id=user_id,
                                thread_id=None,
                                conversation_id=None,
                                topic_name=sub.name,
                                model=DEFAULT_MODEL,
                                working_dir=str(sub.resolve()),
                                auto_approve=AUTO_APPROVE,
                                effort="high",
                                execution_mode="accept-edits"
                            ))
                            seen_dirs.add(norm_sub)
                            seen_names.add(sub.name.lower())
        except Exception:
            pass

        return unique_sessions

session_manager = SessionManager()
