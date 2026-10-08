import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional, List, Tuple

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

import config
import time
import uuid
from session_manager import session_manager
from agy_bridge import agy_bridge
from queue_manager import queue_manager, QueueItem
from telegram_formatter import send_smart_message
from config import normalize_model_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AntigravityTGBot")

DOWNLOADS_DIR = config.BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

AVAILABLE_MODELS = [
    ("⚡ Gemini 3.8 Flash", "gemini-3.8-flash"),
    ("✨ Gemini 3.7 Flash", "gemini-3.7-flash"),
    ("🧠 Gemini 3.1 Pro", "gemini-3.1-pro"),
    ("🎭 Claude Sonnet 4.6", "claude-sonnet-4-6"),
    ("🔮 Claude Opus 4.6 (Thinking)", "claude-opus-4-6-thinking"),
    ("🤖 GPT-OSS 120B", "gpt-oss-120b-medium"),
]

AVAILABLE_EFFORTS = [
    ("🟢 Low", "low"),
    ("🟡 Medium", "medium"),
    ("🟠 High", "high"),
    ("🔴 X-High", "xhigh"),
    ("🟣 Max", "max"),
]

bot = Bot(token=config.BOT_TOKEN) if config.BOT_TOKEN else None
dp = Dispatcher()

def check_permission(user_id: int) -> bool:
    """Check if the user is authorized to use the bot."""
    if not config.ALLOWED_USERS:
        return False
    return user_id in config.ALLOWED_USERS

async def get_session_info(event) -> Tuple[int, int, Optional[int], str]:
    """Extracts (user_id, chat_id, thread_id, session_key)."""
    if isinstance(event, CallbackQuery):
        user_id = event.from_user.id
        chat_id = event.message.chat.id
        thread_id = event.message.message_thread_id
    else:
        user_id = event.from_user.id
        chat_id = event.chat.id
        thread_id = event.message_thread_id

    session_key = await session_manager.get_active_session_key(user_id, chat_id, thread_id)
    return user_id, chat_id, thread_id, session_key

async def send_unauthorized_msg(message: types.Message):
    text = (
        f"# 🔒 Доступ ограничен\n\n"
        f"Ваш Telegram User ID: `{message.from_user.id}`\n\n"
        f"> [!IMPORTANT]\n"
        f"> Чтобы разрешить доступ, добавьте этот ID в файл `.env` на компьютере:\n"
        f"> `ALLOWED_USERS={message.from_user.id}`\n\n"
        f"После изменения перезапустите бота."
    )
    await send_smart_message(
        bot,
        message.chat.id,
        text,
        reply_to_message_id=message.message_id,
        message_thread_id=message.message_thread_id
    )

async def keep_typing(bot: Bot, chat_id: int, stop_event: asyncio.Event, message_thread_id: Optional[int] = None):
    """Continuously send typing action while background process is running."""
    valid_thread_id = message_thread_id if (message_thread_id and message_thread_id > 0) else None
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, message_thread_id=valid_thread_id)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    topic = await session_manager.get_active_topic(session_key)

    is_topic = bool(thread_id and thread_id > 0)
    topic_desc = f"Форумная тема #{thread_id}" if is_topic else f"Проект «{session.topic_name}»"

    text = (
        f"# 🚀 Antigravity Agent Bridge\n\n"
        f"Добро пожаловать! Я ваш персональный агент **Google Antigravity**, работающий прямо на вашем ПК.\n\n"
        f"### ⚙️ Текущая конфигурация («{topic}»)\n"
        f"- **Пространство:** {topic_desc}\n"
        f"- **Модель:** `{session.model}`\n"
        f"- **Глубина рассуждений (Effort):** `{session.effort}`\n"
        f"- **Режим:** `{session.execution_mode}`\n"
        f"- **Авто-выполнение команд:** `{'Включено ⚡' if session.auto_approve else 'Выключено 🔒'}`\n"
        f"- **Рабочая директория:** `{session.working_dir}`\n\n"
        f"### 🏗 Управление проектами и темами\n"
        f"• `/project <имя>` — переключиться или создать проект (например, `/project Funpay`)\n"
        f"• `/projects` — меню проектов с кнопками быстрого переключения\n"
        f"• `/closetopic` — закрыть текущую тему проекта (в группе)\n"
        f"• `/topic <название>` — переименовать текущий проект\n\n"
        f"### 🎮 Панель управления и команды\n"
        f"• `/new` или `/reset` — начать чистый диалог в этом проекте\n"
        f"• `/resume` — выбор сохраненного диалога по теме и просмотр последнего ответа\n"
        f"• `/status` — подробная карточка состояния системы\n"
        f"• `/model` — меню смены нейросети\n"
        f"• `/effort` — настройка интенсивности размышлений\n"
        f"• `/mode` — переключение режимов (планирование / авто-правки)\n"
        f"• `/dir` — сменить или просмотреть рабочую папку на ПК\n"
        f"• `/sh <команда>` — прямое выполнение команды в консоли Windows\n"
        f"• `/stop` — немедленно прервать текущий расчет в этой теме\n"
        f"• `/help` — расширенная справка по всем функциям\n\n"
        f"> [!TIP]\n"
        f"> Каждый проект — это полностью изолированное пространство со своей историей и папкой на диске. Можно вести несколько проектов параллельно!"
    )
    await send_smart_message(bot, chat_id, text, reply_to_message_id=message.message_id, message_thread_id=thread_id)

@dp.message(Command("new", "reset"))
async def handle_new(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    # Cancel active process and clear queue if any
    agy_bridge.cancel_active_process(session_key)
    cancelled_items = await queue_manager.clear_queue(session_key)
    for it in cancelled_items:
        if it.notify_msg:
            try:
                await it.notify_msg.edit_text("🚫 <i>Запрос отменен из-за сброса диалога (/new)</i>", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    await session_manager.reset_conversation(session_key)
    text = (
        f"# 🔄 Сессия сброшена\n\n"
        f"Контекст диалога в этой теме очищен. Следующий ваш запрос откроет новый независимый сеанс с чистой памятью."
    )
    if cancelled_items:
        text += f"\n\n> [!NOTE]\n> Из очереди также удалено ожидающих запросов: **{len(cancelled_items)}**."
    await send_smart_message(bot, chat_id, text, reply_to_message_id=message.message_id, message_thread_id=thread_id)

async def resume_conversation_and_send_last_response(
    chat_id: int,
    user_id: int,
    target_id: str,
    reply_to_message_id: Optional[int] = None,
    message_thread_id: Optional[int] = None
):
    """Activates the conversation and sends back the topic with the last agent answer."""
    session_key = await session_manager.get_active_session_key(user_id, chat_id, message_thread_id)

    await session_manager.update_conversation_id(session_key, target_id)
    details = await session_manager.get_conversation_details(target_id)

    topic = details.get("topic") if details else f"Сессия {target_id[:8]}"
    last_prompt = details.get("last_prompt") if details else ""
    last_resp = details.get("last_response") if details else ""

    text = f"# 💬 Диалог: «{topic}»\n\n"
    text += f"> [!NOTE]\n> Диалог успешно возобновлен. Контекст восстановлен.\n\n"

    if last_prompt:
        clean_prompt = last_prompt.strip()
        if len(clean_prompt) > 250:
            clean_prompt = clean_prompt[:245] + "..."
        text += f"### 👤 Ваш предыдущий вопрос:\n> {clean_prompt}\n\n"

    if last_resp:
        text += f"### 🤖 Предыдущий ответ агента:\n{last_resp}\n\n"
        text += "---\n*Вы можете продолжить диалог, написав следующее сообщение.*"
    else:
        text += "*(Предыдущий ответ в базе не сохранен, но контекст агента активен).*\n\nОтправьте сообщение, чтобы продолжить диалог!"

    await send_smart_message(bot, chat_id, text, reply_to_message_id=reply_to_message_id, message_thread_id=message_thread_id)

@dp.message(Command("resume", "continue", "c"))
async def handle_resume(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        target_id = parts[1].strip()
        await resume_conversation_and_send_last_response(
            chat_id=chat_id,
            user_id=user_id,
            target_id=target_id,
            reply_to_message_id=message.message_id,
            message_thread_id=thread_id
        )
        return

    # No argument provided -> render interactive conversation picker by TOPIC
    convs = await session_manager.get_user_conversations(user_id, session_key=session_key, limit=6)
    if not convs:
        text = (
            f"# 📂 Список сессий пуст\n\n"
            f"У вас пока нет сохраненных сессий диалога. Отправьте любое сообщение, чтобы начать первый диалог!"
        )
        await send_smart_message(bot, chat_id, text, reply_to_message_id=message.message_id, message_thread_id=thread_id)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    current_topic = await session_manager.get_active_topic(session_key)

    buttons = []
    for c in convs:
        cid = c["id"]
        topic_name = c.get("topic") or "Диалог"
        is_current = (cid == session.conversation_id)
        prefix = "👉 " if is_current else "💬 "
        btn_text = f"{prefix}{topic_name} ({c['date']})"
        buttons.append([InlineKeyboardButton(text=btn_text[:50], callback_data=f"resume_conv:{cid}")])

    buttons.append([InlineKeyboardButton(text="➕ Начать новый сеанс (/new)", callback_data="reset_conv_picker")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        f"# 📂 Выбор сохраненного диалога\n\n"
        f"**Текущая тема:** «{current_topic}»\n\n"
        f"Выберите тему диалога для возобновления контекста и просмотра прошлого ответа:"
    )
    await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("resume_conv:"))
async def handle_resume_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    conv_id = callback.data.split(":", 1)[1]
    await callback.answer("Возобновление диалога...")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await resume_conversation_and_send_last_response(
        chat_id=chat_id,
        user_id=user_id,
        target_id=conv_id,
        message_thread_id=thread_id
    )

@dp.callback_query(F.data == "reset_conv_picker")
async def handle_reset_picker_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await session_manager.reset_conversation(session_key)
    await callback.answer("Контекст очищен")
    try:
        await callback.message.edit_text(
            "<b>🔄 Начат новый чистый сеанс.</b>\nСледующее сообщение создаст новый диалог.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.message(F.forum_topic_created)
async def handle_forum_topic_created(message: types.Message):
    """Triggered when a user creates a new topic directly in the Telegram UI."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    title = message.forum_topic_created.name or f"Тема #{thread_id}"

    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_title=title
    )

    welcome_text = (
        f"# 🚀 Проект «{title}» создан!\n\n"
        f"Добро пожаловать в изолированную тему проекта **«{title}»**.\n\n"
        f"- **ID темы:** `{thread_id}`\n"
        f"- **📁 Рабочая папка (CWD):** `{session.working_dir}`\n"
        f"- **🧠 Модель:** `{session.model}`\n"
        f"- **⚙️ Режим:** `{session.execution_mode}`\n\n"
        f"> [!TIP]\n"
        f"> Все задачи и диалоги в этой теме выполняются независимо и параллельно с другими проектами!\n"
        f"> Чтобы привязать другую папку: `/dir C:\\путь\\к\\папке`"
    )
    await send_smart_message(bot, chat_id, welcome_text, message_thread_id=thread_id)

@dp.message(F.forum_topic_edited)
async def handle_forum_topic_edited(message: types.Message):
    """Triggered when a topic is renamed or edited in the Telegram UI."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    if not check_permission(user_id):
        return

    new_title = message.forum_topic_edited.name
    if new_title and thread_id:
        session_key = f"{chat_id}:{thread_id}"
        await session_manager.set_topic(session_key, new_title)
        logger.info(f"Updated topic name for {session_key} to '{new_title}'")

@dp.message(Command("project", "newtopic", "topic_new"))
async def handle_new_project_topic(message: types.Message):
    """Creates a native forum topic or switches project."""
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    chat = message.chat
    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        current_sess = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
        text = (
            f"# 🏗 Управление проектами и темами\n\n"
            f"**Текущий активный проект:** «{current_sess.topic_name}»\n"
            f"- **📁 Рабочая папка:** `{current_sess.working_dir}`\n"
            f"- **🧠 Модель:** `{current_sess.model}`\n\n"
            f"### 💡 Быстрые команды:\n"
            f"• `/project <название>` — создать нативную тему (топик) для проекта (например, `/project Funpay`)\n"
            f"• `/projects` — интерактивный список всех проектов\n"
            f"• `/topic <новое имя>` — переименовать текущую тему\n"
            f"• `/closetopic` — закрыть/удалить текущую тему\n"
            f"• `/dir` — управление рабочей папкой темы\n\n"
            f"> [!TIP]\n"
            f"> Каждый проект хранит собственную историю диалога, настройки и отдельную папку на диске. Задачи выполняются параллельно!"
        )
        await send_smart_message(bot, chat.id, text, reply_to_message_id=message.message_id, message_thread_id=thread_id)
        return

    title = parts[1].strip()

    # 1. First, attempt to create a native Telegram forum topic
    # (Works in forum supergroups AND in private chats with Topics Mode enabled)
    try:
        topic = await bot.create_forum_topic(chat_id=chat.id, name=title)
        new_thread_id = topic.message_thread_id

        new_session = await session_manager.get_session(
            user_id=user_id,
            chat_id=chat.id,
            thread_id=new_thread_id,
            topic_title=title
        )

        welcome_text = (
            f"# 🚀 Проект «{title}» создан!\n\n"
            f"Добро пожаловать в изолированную тему проекта **«{title}»**.\n\n"
            f"- **ID темы:** `{new_thread_id}`\n"
            f"- **📁 Рабочая папка:** `{new_session.working_dir}`\n"
            f"- **🧠 Модель:** `{new_session.model}`\n"
            f"- **⚙️ Режим:** `{new_session.execution_mode}`\n\n"
            f"> [!TIP]\n"
            f"> Задачи в разных темах выполняются полностью независимо и параллельно!\n"
            f"> Чтобы привязать другую папку, напишите: `/dir C:\\путь`"
        )
        await send_smart_message(
            bot=bot,
            chat_id=chat.id,
            text=welcome_text,
            message_thread_id=new_thread_id
        )

        await message.reply(
            f"✅ **Нативная тема «{title}» успешно создана в Telegram!** Перейдите в неё для начала работы."
        )
        return

    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        logger.info(f"create_forum_topic failed: {e}")
        if "not a forum" in err_msg or "chat is not a forum" in err_msg:
            # Native forum topics not enabled in this chat
            if chat.type == "private":
                me = await bot.get_me()
                topics_guide = (
                    f"# 💡 Нативные топики в боте отключены в @BotFather\n\n"
                    f"Telegram поддерживает **настоящие топики-вкладки прямо в диалоге с ботом**! Чтобы бот мог создавать отдельные темы-вкладки в этом чате, включите режим топиков в **@BotFather**:\n\n"
                    f"### 📋 Пошаговая инструкция (занимает 1 минуту):\n"
                    f"1. Откройте диалог с **@BotFather** в Telegram.\n"
                    f"2. Отправьте команду `/mybots` и выберите вашего бота (`@{me.username}`).\n"
                    f"3. Нажмите **Bot Settings** → **Topics** (или **Forum Topics**).\n"
                    f"4. Нажмите **Turn on** (Включить).\n"
                    f"5. *(Рекомендуется)* Также включите **Allow users to create topics**, чтобы создавать темы кнопкой **«+»** прямо в приложении Telegram.\n\n"
                    f"> [!NOTE]\n"
                    f"> Как только вы включите эту настройку в @BotFather, бот сможет создавать нативные вкладки для каждого проекта!\n\n"
                    f"---\n\n"
                    f"### 🔄 Временный режим виртуального проекта:\n"
                    f"Пока опция в BotFather выключена, проект **«{title}»** активирован в режиме виртуального переключения:"
                )

                new_sess, was_created, working_dir = await session_manager.switch_or_create_project(user_id, title)
                quick_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📁 Файлы (/dir)", callback_data="nav_dir:."),
                        InlineKeyboardButton(text="📋 Все проекты (/projects)", callback_data="show_projects_menu")
                    ],
                    [
                        InlineKeyboardButton(text="🔄 Очистить контекст (/new)", callback_data="reset_conv_picker")
                    ]
                ])

                status_line = "создан и активирован" if was_created else "активирован"
                topics_guide += (
                    f"\n\n- **Статус:** {status_line}\n"
                    f"- **📁 Рабочая папка:** `{new_sess.working_dir}`\n"
                    f"- **🧠 Модель:** `{new_sess.model}`\n\n"
                    f"> [!TIP]\n"
                    f"> Отправляйте задачи прямо сюда. Для смены проекта: `/projects`"
                )

                await send_smart_message(
                    bot,
                    chat.id,
                    topics_guide,
                    reply_to_message_id=message.message_id,
                    reply_markup=quick_keyboard,
                    message_thread_id=thread_id
                )
                return
            else:
                await message.reply(
                    f"❌ **Не удалось создать тему в группе:** убедитесь, что в настройках группы включены «Темы» (Topics), а бот является администратором с правом «Управление темами»."
                )
                return
        else:
            await message.reply(f"❌ **Не удалось создать тему:** `{str(e)}`")
            return
    except Exception as e:
        logger.exception("Unexpected error in create_forum_topic")
        await message.reply(f"❌ **Ошибка:** `{str(e)}`")

@dp.message(Command("closetopic", "close"))
async def handle_close_topic(message: types.Message):
    """Closes or deletes the current forum topic in Telegram."""
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    if not thread_id or thread_id == 0:
        await message.reply("ℹ️ Команда закрытия работает только внутри темы проекта.")
        return

    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        await message.reply("🔒 **Тема проекта успешно закрыта.**")
    except Exception:
        try:
            await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
            await message.reply("🗑️ **Тема проекта удалена.**")
        except Exception as e:
            await message.reply(f"❌ **Не удалось закрыть/удалить тему:** `{str(e)}`")

@dp.message(Command("reopentopic", "reopen"))
async def handle_reopen_topic(message: types.Message):
    """Reopens a closed forum topic in Telegram."""
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    if not thread_id or thread_id == 0:
        await message.reply("ℹ️ Команда работает только внутри форумной темы проекта.")
        return

    try:
        await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        await message.reply("🔓 **Тема проекта вновь открыта.**")
    except Exception as e:
        await message.reply(f"❌ **Не удалось открыть тему:** `{str(e)}`")

@dp.message(F.forum_topic_created)
async def handle_forum_topic_created(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    topic_name = message.forum_topic_created.name
    logger.info(f"Forum topic created: id={thread_id}, name='{topic_name}' in chat {chat_id}")

    session = await session_manager.get_session(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_title=topic_name
    )

    welcome_text = (
        f"# 🚀 Тема «{topic_name}» создана!\n\n"
        f"Добро пожаловать в рабочее пространство темы **«{topic_name}»**.\n\n"
        f"- **📁 Рабочая папка:** `{session.working_dir}`\n"
        f"- **🧠 Модель:** `{session.model}`\n"
        f"- **⚙️ Режим:** `{session.execution_mode}`\n\n"
        f"> [!TIP]\n"
        f"> Папка автоматически привязана к проекту. Чтобы сменить папку: `/dir <путь>`.\n"
        f"> Любые ваши сообщения и задачи в этой теме исполняются независимо от других тем!"
    )
    await send_smart_message(bot, chat_id, welcome_text, message_thread_id=thread_id)

@dp.message(F.forum_topic_edited)
async def handle_forum_topic_edited(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    new_name = message.forum_topic_edited.name
    if new_name and thread_id:
        session_key = f"{chat_id}:{thread_id}"
        await session_manager.set_topic(session_key, new_name)
        # Check if matching folder exists in Projects and auto-bind
        matched_dir = None
        try:
            p_dir = Path(r"C:\Users\Chatty\Projects")
            if p_dir.exists():
                for sub in p_dir.iterdir():
                    if sub.is_dir() and sub.name.lower() == new_name.lower():
                        matched_dir = str(sub.resolve())
                        break
        except Exception:
            pass
        if matched_dir:
            await session_manager.set_working_dir(session_key, matched_dir)
        logger.info(f"Forum topic {thread_id} edited to '{new_name}', working_dir={matched_dir or 'unchanged'}")

@dp.message(F.forum_topic_closed)
async def handle_forum_topic_closed(message: types.Message):
    logger.info(f"Forum topic {message.message_thread_id} closed")

@dp.message(F.forum_topic_reopened)
async def handle_forum_topic_reopened(message: types.Message):
    logger.info(f"Forum topic {message.message_thread_id} reopened")

@dp.message(Command("projects", "topics"))
async def handle_projects_list(message: types.Message):
    """Lists all projects and provides interactive switching buttons."""
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    chat = message.chat
    if not getattr(chat, "is_forum", False):
        # 1-on-1 private chat: list all user projects with interactive buttons
        projects = await session_manager.get_all_projects_for_user(user_id)
        active_key = await session_manager.get_active_session_key(user_id, chat_id, thread_id)

        lines = ["# 📂 Ваши проекты\n"]
        buttons = []
        for p in projects:
            is_active = (p.session_key == active_key)
            is_running = agy_bridge.is_process_running(p.session_key) or queue_manager.is_busy(p.session_key)
            q_size = queue_manager.get_queue_size(p.session_key)
            if is_running:
                status_desc = f"⚡ выполняется (+{q_size} в очереди)" if q_size > 0 else "⚡ выполняется задача"
            else:
                status_desc = "💤 свободен"
            active_badge = "🟢 **[АКТИВЕН]** " if is_active else ""
            lines.append(
                f"### {active_badge}📌 {p.topic_name}\n"
                f"- **Папка:** `{p.working_dir}`\n"
                f"- **Модель:** `{p.model}`\n"
                f"- **Статус:** {status_desc}\n"
            )
            btn_mark = "🟢 " if is_active else "📁 "
            btn_text = f"{btn_mark}{p.topic_name}"[:40]
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"switch_proj:{p.session_key}")])

        buttons.append([InlineKeyboardButton(text="➕ Создать проект (/project <имя>)", callback_data="create_proj_hint")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        lines.append("> [!TIP]\n> Нажмите кнопку нужного проекта для мгновенного переключения:")
        await send_smart_message(bot, chat_id, "\n".join(lines), reply_markup=keyboard, message_thread_id=thread_id)
        return

    # Telegram forum group: list forum topic projects
    sessions = await session_manager.get_all_sessions_for_chat(chat_id)
    if not sessions:
        text = (
            f"# 🏗 Список проектов пуст\n\n"
            f"В этом чате пока нет тем/проектов. Используйте `/project <имя>` для создания первого проекта!"
        )
        await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)
        return

    lines = ["# 📂 Активные проекты в этой группе\n"]
    for s in sessions:
        is_running = agy_bridge.is_process_running(s.session_key) or queue_manager.is_busy(s.session_key)
        q_size = queue_manager.get_queue_size(s.session_key)
        if is_running:
            status_icon = f"⚡ Выполняется задача (+{q_size} в очереди)" if q_size > 0 else "⚡ Выполняется задача"
        else:
            status_icon = "💤 Свободен"
        t_id_str = f"Тема #{s.thread_id}" if s.thread_id else "AntigravityTG"
        lines.append(
            f"### 📌 {s.topic_name} ({t_id_str})\n"
            f"- **Папка:** `{s.working_dir}`\n"
            f"- **Модель:** `{s.model}`\n"
            f"- **Статус:** {status_icon}\n"
        )

    lines.append("\n> [!TIP]\n> Задачи во всех темах выполняются параллельно и независимо.")
    await send_smart_message(bot, chat_id, "\n".join(lines), message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("switch_proj:"))
async def handle_switch_project_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, _ = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    target_session_key = callback.data.split("switch_proj:", 1)[1]
    await session_manager.set_active_session_key(user_id, target_session_key)
    session = await session_manager.get_session_by_key(target_session_key)
    proj_name = session.topic_name if session else target_session_key

    await callback.answer(f"Активный проект: {proj_name}")

    # Re-render keyboard with updated active checkmark
    projects = await session_manager.get_all_projects_for_user(user_id)
    buttons = []
    lines = ["# 📂 Ваши проекты\n"]
    for p in projects:
        is_active = (p.session_key == target_session_key)
        is_running = agy_bridge.is_process_running(p.session_key) or queue_manager.is_busy(p.session_key)
        q_size = queue_manager.get_queue_size(p.session_key)
        if is_running:
            status_desc = f"⚡ выполняется (+{q_size} в очереди)" if q_size > 0 else "⚡ выполняется задача"
        else:
            status_desc = "💤 свободен"
        active_badge = "🟢 **[АКТИВЕН]** " if is_active else ""
        lines.append(
            f"### {active_badge}📌 {p.topic_name}\n"
            f"- **Папка:** `{p.working_dir}`\n"
            f"- **Модель:** `{p.model}`\n"
            f"- **Статус:** {status_desc}\n"
        )
        btn_mark = "🟢 " if is_active else "📁 "
        btn_text = f"{btn_mark}{p.topic_name}"[:40]
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"switch_proj:{p.session_key}")])

    buttons.append([InlineKeyboardButton(text="➕ Создать проект (/project <имя>)", callback_data="create_proj_hint")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    lines.append(f"> [!NOTE]\n> Активный проект изменен на: **«{proj_name}»** (Папка: `{session.working_dir if session else ''}`).")

    try:
        await callback.message.edit_text(
            f"<b>✅ Активный проект изменен на: «{proj_name}»</b>\nПапка: <code>{session.working_dir if session else ''}</code>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.callback_query(F.data == "create_proj_hint")
async def handle_create_project_hint_callback(callback: CallbackQuery):
    await callback.answer("Отправьте в чат: /project <название>", show_alert=True)

@dp.callback_query(F.data == "show_projects_menu")
async def handle_show_projects_menu_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, _ = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    projects = await session_manager.get_all_projects_for_user(user_id)
    active_key = await session_manager.get_active_session_key(user_id, chat_id, thread_id)
    lines = ["# 📂 Ваши проекты\n"]
    buttons = []
    for p in projects:
        is_active = (p.session_key == active_key)
        is_running = agy_bridge.is_process_running(p.session_key) or queue_manager.is_busy(p.session_key)
        q_size = queue_manager.get_queue_size(p.session_key)
        if is_running:
            status_desc = f"⚡ выполняется (+{q_size} в очереди)" if q_size > 0 else "⚡ выполняется задача"
        else:
            status_desc = "💤 свободен"
        active_badge = "🟢 **[АКТИВЕН]** " if is_active else ""
        lines.append(
            f"### {active_badge}📌 {p.topic_name}\n"
            f"- **Папка:** `{p.working_dir}`\n"
            f"- **Модель:** `{p.model}`\n"
            f"- **Статус:** {status_desc}\n"
        )
        btn_mark = "🟢 " if is_active else "📁 "
        btn_text = f"{btn_mark}{p.topic_name}"[:40]
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"switch_proj:{p.session_key}")])

    buttons.append([InlineKeyboardButton(text="➕ Создать проект (/project <имя>)", callback_data="create_proj_hint")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    lines.append("> [!TIP]\n> Нажмите кнопку нужного проекта для мгновенного переключения:")
    await send_smart_message(bot, chat_id, "\n".join(lines), reply_markup=keyboard, message_thread_id=thread_id)

@dp.message(Command("topic", "title"))
async def handle_topic(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        current_topic = await session_manager.get_active_topic(session_key)
        text = (
            f"# 🏷 Тема текущего проекта / диалога\n\n"
            f"Текущее название: «{current_topic}»\n\n"
            f"Чтобы переименовать, напишите:\n"
            f"`/topic Новое название темы`"
        )
        await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)
        return

    new_title = parts[1].strip()
    await session_manager.set_topic(session_key, new_title)

    # If it's a forum topic, also rename the topic in Telegram!
    if thread_id and thread_id > 0:
        try:
            await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=new_title)
        except Exception as e:
            logger.info(f"Could not edit forum topic name in Telegram: {e}")

    text = f"✅ **Название проекта изменено на:** «{new_title}»"
    await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)

@dp.message(Command("status", "config", "settings"))
async def handle_status(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    topic = await session_manager.get_active_topic(session_key)
    is_busy = agy_bridge.is_process_running(session_key) or queue_manager.is_busy(session_key)
    q_size = queue_manager.get_queue_size(session_key)

    topic_label = f"Форумная тема #{thread_id}" if (thread_id and thread_id > 0) else f"Проект «{session.topic_name}»"
    status_label = f"⚡ Выполняет задачу (+{q_size} в очереди)" if (is_busy and q_size > 0) else ("⚡ Выполняет задачу прямо сейчас" if is_busy else "🟢 Свободен (готов к задачам)")
    queue_label = f"📥 {q_size} в ожидании" if q_size > 0 else "🟢 Свободна"

    text = (
        f"# 📊 Карточка состояния системы\n\n"
        f"### 💬 Активный проект / сессия\n"
        f"- **Тема:** «{topic}» ({topic_label})\n"
        f"- **Рабочая папка (CWD):** `{session.working_dir}`\n"
        f"- **Статус процесса:** {status_label}\n"
        f"- **Очередь сообщений:** {queue_label}\n\n"
        f"### 🤖 Параметры агента\n"
        f"- **Активная модель:** `{session.model}`\n"
        f"- **Глубина рассуждений (Effort):** `{session.effort}`\n"
        f"- **Режим выполнения (Mode):** `{session.execution_mode}`\n"
        f"- **Авто-подтверждение прав:** `{'Да ⚡' if session.auto_approve else 'Нет 🔒'}`\n"
        f"- **CLI бинарник:** `{config.AGY_PATH}`"
    )
    await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)

@dp.message(Command("model"))
async def handle_model(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        raw_val = parts[1].strip()
        norm_val = normalize_model_name(raw_val)
        await session_manager.set_model(session_key, norm_val)
        await send_smart_message(
            bot,
            chat_id,
            f"✅ **Модель установлена:** `{norm_val}`\n\n"
            f"> [!TIP]\n"
            f"> Уровень рассуждений настраивается отдельно командой `/effort`.",
            message_thread_id=thread_id
        )
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    norm_current = normalize_model_name(session.model)
    buttons = []
    for label, model_id in AVAILABLE_MODELS:
        prefix = "✅ " if model_id == norm_current else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_model:{model_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        f"# 🤖 Выбор нейросети\n\n"
        f"- **Текущая модель:** `{norm_current}`\n"
        f"- **Уровень рассуждений (Effort):** `{session.effort}`\n\n"
        f"Выберите базовую модель (глубина рассуждений регулируется командой `/effort`):"
    )
    await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("set_model:"))
async def handle_model_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    raw_model = callback.data.split(":", 1)[1]
    new_model = normalize_model_name(raw_model)
    await session_manager.set_model(session_key, new_model)
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    await callback.answer(f"Модель изменена на {new_model}")

    buttons = []
    for label, model_id in AVAILABLE_MODELS:
        prefix = "✅ " if model_id == new_model else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_model:{model_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await callback.message.edit_text(
            f"<b>✅ Модель успешно изменена на:</b> <code>{new_model}</code>\n"
            f"<i>Текущий уровень рассуждений: <code>{session.effort}</code> (регулируется через /effort)</i>\n\n"
            f"Выберите другую при необходимости:",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.message(Command("effort"))
async def handle_effort(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        val = parts[1].strip().lower()
        if val in ("low", "medium", "high", "xhigh", "max"):
            await session_manager.set_effort(session_key, val)
            await send_smart_message(bot, chat_id, f"✅ **Уровень рассуждений (Effort) установлен:** `{val}`", message_thread_id=thread_id)
            return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    buttons = []
    for label, val in AVAILABLE_EFFORTS:
        prefix = "✅ " if val == session.effort else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_effort:{val}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        f"# 🧠 Настройка глубины рассуждений (Effort)\n\n"
        f"- **Текущая модель:** `{session.model}`\n"
        f"- **Текущий уровень рассуждений:** `{session.effort}`\n\n"
        f"Определяет, сколько вычислительных ресурсов модель тратит на обдумывание перед ответом:"
    )
    await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("set_effort:"))
async def handle_effort_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    new_effort = callback.data.split(":", 1)[1]
    await session_manager.set_effort(session_key, new_effort)
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    await callback.answer(f"Effort: {new_effort}")

    buttons = []
    for label, val in AVAILABLE_EFFORTS:
        prefix = "✅ " if val == new_effort else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_effort:{val}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await callback.message.edit_text(
            f"<b>✅ Уровень рассуждений изменен на:</b> <code>{new_effort}</code>\n"
            f"<i>Модель: <code>{session.model}</code></i>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.message(Command("mode"))
async def handle_mode(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'accept-edits' else ''}🛠 Accept-Edits (Кодинг)",
                callback_data="set_exec_mode:accept-edits"
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'plan' else ''}📋 Plan (Планирование)",
                callback_data="set_exec_mode:plan"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"⚡ Авто-права: {'ВКЛ' if session.auto_approve else 'ВЫКЛ'}",
                callback_data="toggle_auto_approve"
            )
        ]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        f"# ⚙️ Режимы работы агента\n\n"
        f"- **Accept-Edits**: стандартный режим выполнения — агент пишет код и правит файлы.\n"
        f"- **Plan**: режим архитектурного проектирования — агент исследует репозиторий и составляет план без внесения правок.\n"
        f"- **Авто-права**: автоматическое одобрение вызова инструментов консоли и редактирования файлов."
    )
    await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("set_exec_mode:"))
async def handle_exec_mode_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    new_mode = callback.data.split(":", 1)[1]
    await session_manager.set_execution_mode(session_key, new_mode)
    await callback.answer(f"Режим изменен на {new_mode}")
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'accept-edits' else ''}🛠 Accept-Edits (Кодинг)",
                callback_data="set_exec_mode:accept-edits"
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'plan' else ''}📋 Plan (Планирование)",
                callback_data="set_exec_mode:plan"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"⚡ Авто-права: {'ВКЛ' if session.auto_approve else 'ВЫКЛ'}",
                callback_data="toggle_auto_approve"
            )
        ]
    ]
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception:
        pass

@dp.callback_query(F.data == "toggle_auto_approve")
async def handle_toggle_auto_approve(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    new_val = not session.auto_approve
    await session_manager.set_auto_approve(session_key, new_val)
    session.auto_approve = new_val
    await callback.answer(f"Авто-права: {'ВКЛ' if new_val else 'ВЫКЛ'}")

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'accept-edits' else ''}🛠 Accept-Edits (Кодинг)",
                callback_data="set_exec_mode:accept-edits"
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if session.execution_mode == 'plan' else ''}📋 Plan (Планирование)",
                callback_data="set_exec_mode:plan"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"⚡ Авто-права: {'ВКЛ' if session.auto_approve else 'ВЫКЛ'}",
                callback_data="toggle_auto_approve"
            )
        ]
    ]
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception:
        pass

def resolve_target_dir(arg: str, current_dir: str) -> Path:
    """
    Resolves a path: if absolute, returns it; if relative (e.g. subfolder name),
    resolves it relative to current_dir.
    """
    clean = os.path.expanduser(os.path.expandvars(arg.strip().strip('"').strip("'")))
    p = Path(clean)
    if p.is_absolute():
        return p.resolve()
    return (Path(current_dir) / p).resolve()

def get_dir_view(dir_path: Path) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Builds a formatted text and inline buttons for the directory."""
    subdirs = []
    try:
        if dir_path.exists() and dir_path.is_dir():
            for item in dir_path.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    subdirs.append(item.name)
            subdirs.sort(key=lambda s: s.lower())
    except Exception:
        pass

    text = f"# 📁 Рабочая папка агента\n\n**Текущий путь:** `{dir_path}`\n\n"
    if subdirs:
        text += "### 📂 Папки внутри:\n"
        for sd in subdirs[:12]:
            text += f"• `{sd}`\n"
        if len(subdirs) > 12:
            text += f"*...и еще {len(subdirs) - 12} папок*\n"
    else:
        text += "*Вложенных папок не обнаружено.*\n"

    buttons = []
    # Up button if not at drive root
    if dir_path.parent != dir_path:
        buttons.append([InlineKeyboardButton(text="⬆ Вверх (..)", callback_data="nav_dir:..")])

    row = []
    for sd in subdirs[:10]:
        row.append(InlineKeyboardButton(text=f"📁 {sd[:18]}", callback_data=f"nav_dir:{sd[:30]}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return text, keyboard

@dp.message(Command("dir"))
async def handle_dir(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    parts = message.text.split(maxsplit=1)
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    current_dir = Path(session.working_dir)

    # 1. No arguments: view current dir and subfolders
    if len(parts) == 1:
        text, keyboard = get_dir_view(current_dir)
        text += (
            "\n> [!TIP]\n"
            "> Вы можете открыть папку, нажав на кнопку ниже, или написав:\n"
            "> `/dir имя_папки` — открыть папку внутри текущей\n"
            "> `/dir C:\\полный\\путь` — открыть полный путь"
        )
        await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)
        return

    # 2. Argument provided: resolve relative or absolute path
    raw_arg = parts[1].strip()
    target_path = resolve_target_dir(raw_arg, session.working_dir)

    if not target_path.exists() or not target_path.is_dir():
        # Folder not found -> show error and list available subfolders in current dir
        text, keyboard = get_dir_view(current_dir)
        err_msg = (
            f"# ❌ Папка не найдена\n\n"
            f"Путь `{target_path}` не существует или не является директорией.\n\n"
            f"### 📂 Доступные папки в текущей директории (`{current_dir}`):\n"
        )
        await send_smart_message(bot, chat_id, err_msg, reply_markup=keyboard, message_thread_id=thread_id)
        return

    # Folder exists -> switch to it for this project/topic
    await session_manager.set_working_dir(session_key, str(target_path))
    text, keyboard = get_dir_view(target_path)
    success_header = (
        f"# ✅ Рабочая папка для этой темы открыта\n\n"
        f"> [!NOTE]\n"
        f"> Контекст сессии для этой темы автоматически сброшен для нового рабочего пространства.\n\n"
    )
    await send_smart_message(bot, chat_id, success_header + text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("nav_dir:"))
async def handle_nav_dir_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rel_name = callback.data.split(":", 1)[1]
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    target_path = resolve_target_dir(rel_name, session.working_dir)

    if not target_path.exists() or not target_path.is_dir():
        await callback.answer("Папка не найдена", show_alert=True)
        return

    await session_manager.set_working_dir(session_key, str(target_path))
    await callback.answer(f"Открыта папка: {target_path.name or str(target_path)}")

    text, keyboard = get_dir_view(target_path)
    success_header = (
        f"# ✅ Рабочая папка для этой темы открыта\n\n"
        f"> [!NOTE]\n"
        f"> Контекст сессии для этой темы автоматически сброшен для нового рабочего пространства.\n\n"
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_smart_message(bot, chat_id, success_header + text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.message(Command("sh", "cmd", "exec"))
async def handle_sh(message: types.Message):
    """Directly execute a shell command on the host computer."""
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        text = (
            f"# 💻 Прямое выполнение команд\n\n"
            f"Использование: `/sh <команда>`\n"
            f"Пример: `/sh git status` или `/sh dir`"
        )
        await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)
        return

    cmd_to_run = parts[1].strip()
    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)

    status_msg = await message.reply("⏳ <i>Выполняю консольную команду...</i>", parse_mode=ParseMode.HTML)
    output, code = await agy_bridge.execute_shell_command(cmd_to_run, session.working_dir)

    try:
        await status_msg.delete()
    except Exception:
        pass

    text = (
        f"# 💻 Результат выполнения: `{cmd_to_run}`\n\n"
        f"**Код возврата:** `{code}`\n\n"
        f"```shell\n{output}\n```"
    )
    await send_smart_message(bot, chat_id, text, reply_to_message_id=message.message_id, message_thread_id=thread_id)

@dp.message(Command("stop", "cancel"))
async def handle_stop(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    stopped_proc = agy_bridge.cancel_active_process(session_key)
    cancelled_items = await queue_manager.clear_queue(session_key)
    queue_count = len(cancelled_items)

    for it in cancelled_items:
        if it.notify_msg:
            try:
                await it.notify_msg.edit_text("🚫 <i>Запрос отменен командой /stop</i>", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    if stopped_proc and queue_count > 0:
        await message.reply(
            f"🛑 <b>Текущая операция агента остановлена.</b>\n"
            f"🧹 Из очереди отменено запросов: <b>{queue_count}</b>.",
            parse_mode=ParseMode.HTML
        )
    elif stopped_proc:
        await message.reply(
            "🛑 <b>Текущая операция агента в этой теме немедленно остановлена.</b>",
            parse_mode=ParseMode.HTML
        )
    elif queue_count > 0:
        await message.reply(
            f"🧹 <b>Очередь запросов очищена</b> (отменено запросов: <b>{queue_count}</b>).",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply(
            "ℹ️ В этой теме нет активных запущенных процессов или запросов в очереди.",
            parse_mode=ParseMode.HTML
        )

@dp.message(Command("queue", "q"))
async def handle_queue(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    session = await session_manager.get_session(user_id=user_id, chat_id=chat_id, thread_id=thread_id, session_key=session_key)
    current_item = queue_manager.get_current_item(session_key)
    pending_items = queue_manager.get_pending_items(session_key)

    if not current_item and not pending_items:
        text = (
            f"# 📥 Очередь задач: «{session.topic_name}»\n\n"
            f"ℹ️ **Очередь пуста.**\n"
            f"Все задачи выполнены. Агент готов к приему новых сообщений!"
        )
        await send_smart_message(bot, chat_id, text, message_thread_id=thread_id)
        return

    text = f"# 📥 Очередь задач: «{session.topic_name}»\n\n"

    if current_item:
        elapsed = int(time.time() - current_item.enqueued_at)
        prompt_snippet = current_item.prompt.strip().replace("\n", " ")
        if len(prompt_snippet) > 80:
            prompt_snippet = prompt_snippet[:77] + "..."
        text += (
            f"### ⚡ Сейчас выполняется:\n"
            f"- **Запрос:** {prompt_snippet}\n"
            f"- **В работе:** {elapsed} сек.\n\n"
        )

    if pending_items:
        text += f"### ⏳ Ожидают в очереди ({len(pending_items)}):\n"
        for idx, it in enumerate(pending_items, 1):
            p_snippet = it.prompt.strip().replace("\n", " ")
            if len(p_snippet) > 60:
                p_snippet = p_snippet[:57] + "..."
            text += f"{idx}. {p_snippet}\n"
        text += "\n"

    buttons = []
    if pending_items:
        buttons.append([InlineKeyboardButton(text="🗑️ Очистить очередь", callback_data=f"clear_queue:{session_key}")])
    if current_item:
        buttons.append([InlineKeyboardButton(text="🛑 Остановить текущую (/stop)", callback_data=f"stop_current:{session_key}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await send_smart_message(bot, chat_id, text, reply_markup=keyboard, message_thread_id=thread_id)

@dp.callback_query(F.data.startswith("cancel_q:"))
async def handle_cancel_queue_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    item_id = callback.data.split(":", 1)[1]
    s_key, removed = await queue_manager.remove_item_anywhere(item_id)
    if removed:
        await callback.answer("Запрос удален из очереди")
        try:
            await callback.message.edit_text(
                "🚫 <b>Запрос удален из очереди.</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    current = queue_manager.get_current_item(session_key)
    if current and current.item_id == item_id:
        await callback.answer("Этот запрос уже выполняется! Отправьте /stop для отмены.", show_alert=True)
    else:
        await callback.answer("Запрос уже выполнен или не найден.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass

@dp.callback_query(F.data.startswith("clear_queue:"))
async def handle_clear_queue_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    target_session_key = callback.data.split(":", 1)[1]
    cancelled = await queue_manager.clear_queue(target_session_key)
    for it in cancelled:
        if it.notify_msg:
            try:
                await it.notify_msg.edit_text("🚫 <i>Запрос отменен (очередь очищена)</i>", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    await callback.answer(f"Очередь очищена ({len(cancelled)} задач)")
    try:
        await callback.message.edit_text(
            f"🧹 <b>Очередь запросов очищена.</b> Удалено задач: {len(cancelled)}.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("stop_current:"))
async def handle_stop_current_callback(callback: CallbackQuery):
    user_id, chat_id, thread_id, session_key = await get_session_info(callback)
    if not check_permission(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    target_session_key = callback.data.split(":", 1)[1]
    stopped = agy_bridge.cancel_active_process(target_session_key)
    cancelled = await queue_manager.clear_queue(target_session_key)
    for it in cancelled:
        if it.notify_msg:
            try:
                await it.notify_msg.edit_text("🚫 <i>Запрос отменен командой /stop</i>", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    await callback.answer("Операция остановлена")
    try:
        await callback.message.edit_text(
            f"🛑 <b>Операция остановлена пользователем.</b>\nОтменено задач из очереди: {len(cancelled)}.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@dp.message(Command("help"))
async def handle_help(message: types.Message):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    help_text = (
        f"# 📖 Руководство пользователя Antigravity TG\n\n"
        f"Бот предоставляет полноценный удаленный терминал и интерфейс к агенту Google Antigravity на вашем ПК.\n\n"
        f"### 🏗 Проекты и многозадачность\n"
        f"- `/project <название>` — переключиться или создать проект (в личном чате или группе)\n"
        f"- `/project AntigravityTG` (или `/project main`) — переключиться на проект AntigravityTG\n"
        f"- `/projects` — интерактивный список всех проектов с кнопками быстрого переключения\n"
        f"- `/topic <название>` — переименовать текущий проект\n"
        f"- `/closetopic` / `/reopentopic` — закрыть или открыть тему (в группе)\n\n"
        f"> [!TIP]\n"
        f"> Каждый проект — это изолированное пространство с собственной рабочей папкой, моделью и контекстом. Задачи в разных проектах выполняются независимо и параллельно!\n\n"
        f"### 🕹 Команды управления сессией\n"
        f"- `/new` или `/reset` — начать чистый диалог в этом проекте\n"
        f"- `/resume` — выбор сохраненного диалога по проекту и просмотр последнего ответа\n"
        f"- `/status` — сводка параметров подключения, активного проекта и настроек\n"
        f"- `/queue` — состояние очереди сообщений в текущей теме (с кнопкой очистки)\n"
        f"- `/model` — выбор нейросети (Gemini 3.8, Pro, Claude Sonnet/Opus)\n"
        f"- `/effort` — регулировка глубины рассуждений (`low` .. `max`)\n"
        f"- `/mode` — переключение режимов (`accept-edits` / `plan`) и авто-прав\n"
        f"- `/dir` — просмотр и переключение рабочей папки проекта\n"
        f"- `/sh <команда>` — прямое выполнение команды Windows PowerShell\n"
        f"- `/stop` — аварийная остановка текущей задачи и очистка очереди\n\n"
        f"### 📥 Очередь сообщений\n"
        f"Если вы отправляете несколько сообщений подряд, пока агент занят, они автоматически встают в очередь и выполняются последовательно с сохранением общего контекста диалога.\n\n"
        f"### ⚡ Встроенные слэш-команды Antigravity\n"
        f"Вы можете отправлять любые команды CLI прямо в чат:\n"
        f"• `/skills` — просмотр установленных навыков агента\n"
        f"• `/usage` (или `/quota`) — мониторинг квоты и токенов\n"
        f"• `/credits` — проверка оставшихся кредитов\n"
        f"• `/agents` — список доступных субагентов\n"
        f"• `/permissions` — аудит разрешений на инструменты\n"
        f"• `/changelog` — список обновлений движка\n\n"
        f"### 📝 Форматирование сообщений\n"
        f"Все ответы приходят со структурированной разметкой (подсветка синтаксиса, цитаты, таблицы)."
    )
    await send_smart_message(bot, chat_id, help_text, reply_to_message_id=message.message_id, message_thread_id=thread_id)

async def execute_queue_item(item: QueueItem):
    user_id = item.user_id
    chat_id = item.chat_id
    thread_id = item.thread_id
    session_key = item.session_key
    message = item.message
    prompt = item.prompt

    session = await session_manager.get_session(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        session_key=session_key
    )

    proj_tag = f"[{session.topic_name}] " if (session.topic_name and not (thread_id and thread_id > 0)) else ""

    status_msg = None
    if item.notify_msg:
        try:
            await item.notify_msg.edit_text(
                f"⏳ <b>{proj_tag}</b><i>Думаю...</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
            status_msg = item.notify_msg
        except Exception:
            status_msg = None

    if status_msg is None:
        try:
            status_msg = await message.reply(f"⏳ <b>{proj_tag}</b><i>Думаю...</i>", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not send status message: {e}")

    stop_typing_event = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, chat_id, stop_typing_event, message_thread_id=thread_id))

    last_status_text = ""

    async def on_status_update(status_text: str):
        nonlocal last_status_text
        if status_text != last_status_text and status_msg:
            last_status_text = status_text
            try:
                await status_msg.edit_text(f"<b>{proj_tag}</b>{status_text}", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    response_text = ""
    new_conv_id = None
    stats = {}

    try:
        response_text, new_conv_id, stats = await agy_bridge.execute_prompt(
            prompt=prompt,
            session_key=session_key,
            user_id=user_id,
            conversation_id=session.conversation_id,
            model=session.model,
            effort=session.effort,
            execution_mode=session.execution_mode,
            working_dir=session.working_dir,
            auto_approve=session.auto_approve,
            on_status=on_status_update
        )

        if new_conv_id:
            # Save the turn, topic, and last response in local database for this specific session
            await session_manager.save_turn(
                session_key=session_key,
                user_id=user_id,
                conversation_id=new_conv_id,
                prompt=prompt,
                response=response_text,
                chat_id=chat_id,
                thread_id=thread_id
            )
    except Exception as e:
        logger.exception("Error executing prompt via AGY bridge")
        response_text = f"❌ **Ошибка обработки запроса:** `{str(e)}`"
    finally:
        stop_typing_event.set()
        try:
            await typing_task
        except Exception:
            pass

    # Delete progress status message
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Send formatted article message directly to the topic
    if response_text:
        final_text = response_text
        if not getattr(message.chat, "is_forum", False) and not (thread_id and thread_id > 0) and session.topic_name:
            final_text = f"📌 **[Проект: {session.topic_name}]**\n\n" + response_text

        await send_smart_message(
            bot=bot,
            chat_id=chat_id,
            text=final_text,
            reply_to_message_id=message.message_id,
            message_thread_id=thread_id
        )

# Register execute_queue_item processor with queue_manager
queue_manager.set_processor(execute_queue_item)

async def process_user_query(message: types.Message, prompt: str):
    user_id, chat_id, thread_id, session_key = await get_session_info(message)
    item_id = uuid.uuid4().hex[:8]

    item = QueueItem(
        item_id=item_id,
        message=message,
        prompt=prompt,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        session_key=session_key,
        enqueued_at=time.time()
    )

    pos, is_queued = await queue_manager.enqueue(item)

    if is_queued:
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отменить из очереди", callback_data=f"cancel_q:{item_id}")]
        ])
        wait_text = (
            f"📥 <b>Запрос добавлен в очередь</b> (позиция: <b>#{pos}</b>)\n"
            f"<i>Он выполнится автоматически после завершения предыдущих задач.</i>"
        )
        try:
            notify_msg = await message.reply(
                wait_text,
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_kb
            )
            item.notify_msg = notify_msg
        except Exception as e:
            logger.warning(f"Could not send queue notification: {e}")

@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    await process_user_query(message, message.text)

@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    doc = message.document
    dest_path = DOWNLOADS_DIR / doc.file_name
    await bot.download(doc, destination=dest_path)

    caption = message.caption or "Изучи этот файл и опиши его содержимое."
    prompt = f"Пользователь прислал файл на локальный диск: `{dest_path}`\n\nЗапрос: {caption}"
    await process_user_query(message, prompt)

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        await send_unauthorized_msg(message)
        return

    photo = message.photo[-1]
    photo_file = await bot.get_file(photo.file_id)
    ext = os.path.splitext(photo_file.file_path)[1] or ".jpg"
    dest_path = DOWNLOADS_DIR / f"{photo.file_unique_id}{ext}"
    await bot.download(photo, destination=dest_path)

    caption = message.caption or "Что изображено на этом снимке?"
    prompt = f"Пользователь прислал изображение на локальный диск: `{dest_path}`\n\nЗапрос: {caption}"
    await process_user_query(message, prompt)

async def main():
    if not config.BOT_TOKEN:
        print("\n" + "=" * 60)
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не задан!")
        print("Пожалуйста, откройте файл .env и вставьте ваш токен бота:")
        print("TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz")
        print("=" * 60 + "\n")
        return

    print("🚀 Инициализация базы данных сессий...")
    await session_manager.init_db()
    queue_manager.set_processor(execute_queue_item)
    print("📥 Очереди задач инициализированы...")

    print(f"🤖 Antigravity CLI путь: {config.AGY_PATH}")
    print(f"📁 Базовая рабочая папка: {config.DEFAULT_WORKING_DIR}")
    if config.ALLOWED_USERS:
        print(f"🔒 Разрешенные пользователи: {', '.join(map(str, config.ALLOWED_USERS))}")
    else:
        print("⚠️ ВНИМАНИЕ: ALLOWED_USERS не задан. Бот будет отклонять запросы и показывать ID пользователя.")

    try:
        me = await bot.get_me()
        topics_mode = getattr(me, "has_topics_enabled", False)
        users_can_create = getattr(me, "allows_users_to_create_topics", False)
        print(f"🤖 Telegram Бот: @{me.username} ({me.first_name})")
        print(f"📌 Нативный режим топиков (Topics Mode): {'ВКЛЮЧЕН ✅' if topics_mode else 'ВЫКЛЮЧЕН (включите в @BotFather: Bot Settings -> Topics) ⚠️'}")
        if topics_mode:
            print(f"👥 Создание тем пользователями: {'РАЗРЕШЕНО ✅' if users_can_create else 'ТОЛЬКО БОТ 🔒'}")
    except Exception as e:
        logger.warning(f"Could not check getMe topics status: {e}")

    retry_delay = 5
    while True:
        try:
            logger.info("✨ Запуск Telegram Polling...")
            await dp.start_polling(bot, handle_signals=False)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Получен сигнал остановки бота.")
            break
        except Exception as e:
            logger.error(f"Polling loop encountered an error: {e}", exc_info=True)

        logger.warning(f"Polling завершился. Перезапуск через {retry_delay}с...")
        await asyncio.sleep(retry_delay)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Бот остановлен.")
