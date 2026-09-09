import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, ReplyParameters
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "qna.sqlite3"
TOKEN_LENGTH = 32
MAX_MESSAGE_LENGTH = 2000
MAX_CAPTION_LENGTH = 900
MAX_MEDIA_SIZE_BYTES = 50 * 1024 * 1024
RATE_LIMIT_SECONDS = 10
ANONYMOUS_HEADER = "📨 Анонимное сообщение"
ANSWER_HEADER = "📬 Ответ на ваше анонимное сообщение"

SUPPORTED_CONTENT_TYPES = {
    "text",
    "photo",
    "video",
    "voice",
    "video_note",
    "audio",
    "document",
    "animation",
    "sticker",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
}

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

router = Router()


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_telegram_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                revoked_at INTEGER,
                FOREIGN KEY (owner_telegram_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                sender_telegram_id INTEGER PRIMARY KEY,
                link_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (sender_telegram_id) REFERENCES users(telegram_id),
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                sender_telegram_id INTEGER NOT NULL,
                recipient_telegram_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text',
                file_id TEXT,
                file_unique_id TEXT,
                caption TEXT,
                file_name TEXT,
                mime_type TEXT,
                media_group_id TEXT,
                metadata_json TEXT,
                telegram_message_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_sender_time
                ON messages(sender_telegram_id, created_at);

            CREATE TABLE IF NOT EXISTS reply_targets (
                owner_chat_id INTEGER NOT NULL,
                owner_message_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                sender_chat_id INTEGER NOT NULL,
                sender_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (owner_chat_id, owner_message_id),
                FOREIGN KEY (question_id) REFERENCES messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_reply_targets_question
                ON reply_targets(question_id);
            """
        )

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        migrations = {
            "content_type": "TEXT NOT NULL DEFAULT 'text'",
            "file_id": "TEXT",
            "file_unique_id": "TEXT",
            "caption": "TEXT",
            "file_name": "TEXT",
            "mime_type": "TEXT",
            "media_group_id": "TEXT",
            "metadata_json": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")


def now() -> int:
    return int(time.time())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def ensure_user(telegram_id: int) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users (telegram_id, created_at) VALUES (?, ?)",
            (telegram_id, now()),
        )


def create_link(owner_id: int) -> str:
    while True:
        token = secrets.token_urlsafe(TOKEN_LENGTH)
        try:
            with connect_db() as connection:
                connection.execute(
                    "INSERT INTO links (owner_telegram_id, token, token_hash, created_at) VALUES (?, ?, ?, ?)",
                    (owner_id, token, token_hash(token), now()),
                )
            return token
        except sqlite3.IntegrityError:
            continue


def get_active_link(owner_id: int) -> str | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT token FROM links WHERE owner_telegram_id = ? AND active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (owner_id,),
        ).fetchone()
    return row["token"] if row else None


def get_link_by_token(token: str) -> sqlite3.Row | None:
    with connect_db() as connection:
        return connection.execute(
            "SELECT id, owner_telegram_id FROM links WHERE token_hash = ? AND active = 1",
            (token_hash(token),),
        ).fetchone()


def set_session(sender_id: int, link_id: int) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO sessions (sender_telegram_id, link_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(sender_telegram_id) DO UPDATE SET link_id = excluded.link_id, "
            "updated_at = excluded.updated_at",
            (sender_id, link_id, now()),
        )


def clear_session(sender_id: int) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM sessions WHERE sender_telegram_id = ?", (sender_id,))


def get_session(sender_id: int) -> sqlite3.Row | None:
    with connect_db() as connection:
        return connection.execute(
            "SELECT s.link_id, l.owner_telegram_id FROM sessions s "
            "JOIN links l ON l.id = s.link_id WHERE s.sender_telegram_id = ? AND l.active = 1",
            (sender_id,),
        ).fetchone()


def recently_sent(sender_id: int, media_group_id: str | None = None) -> bool:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT created_at, media_group_id FROM messages WHERE sender_telegram_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
    if not row:
        return False
    if media_group_id and row["media_group_id"] == media_group_id:
        return False
    return now() - row["created_at"] < RATE_LIMIT_SECONDS


def save_message(
    link_id: int,
    sender_id: int,
    recipient_id: int,
    *,
    content_type: str,
    text: str = "",
    file_id: str | None = None,
    file_unique_id: str | None = None,
    caption: str | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
    media_group_id: str | None = None,
    metadata: dict | None = None,
    telegram_message_id: int | None = None,
) -> int:
    with connect_db() as connection:
        cursor = connection.execute(
            "INSERT INTO messages (link_id, sender_telegram_id, recipient_telegram_id, text, "
            "content_type, file_id, file_unique_id, caption, file_name, mime_type, media_group_id, "
            "metadata_json, telegram_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_id,
                sender_id,
                recipient_id,
                text,
                content_type,
                file_id,
                file_unique_id,
                caption,
                file_name,
                mime_type,
                media_group_id,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                telegram_message_id,
                now(),
            ),
        )
        return cursor.lastrowid


def save_reply_targets(
    question_id: int,
    owner_chat_id: int,
    owner_message_ids: list[int],
    sender_chat_id: int,
    sender_message_id: int,
) -> None:
    with connect_db() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO reply_targets "
            "(owner_chat_id, owner_message_id, question_id, sender_chat_id, sender_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    owner_chat_id,
                    owner_message_id,
                    question_id,
                    sender_chat_id,
                    sender_message_id,
                    now(),
                )
                for owner_message_id in owner_message_ids
            ],
        )


def get_reply_target(owner_chat_id: int, owner_message_id: int) -> sqlite3.Row | None:
    with connect_db() as connection:
        return connection.execute(
            "SELECT question_id, sender_chat_id, sender_message_id "
            "FROM reply_targets WHERE owner_chat_id = ? AND owner_message_id = ?",
            (owner_chat_id, owner_message_id),
        ).fetchone()


def delete_reply_targets(question_id: int) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM reply_targets WHERE question_id = ?", (question_id,))


def link_url(username: str, token: str) -> str:
    return f"https://t.me/{username}?start={token}"


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user or message.chat.type != "private":
        return

    user_id = message.from_user.id
    ensure_user(user_id)

    if command.args:
        link = get_link_by_token(command.args)
        if not link:
            await message.answer("Эта ссылка недействительна или отключена.")
            return
        if link["owner_telegram_id"] == user_id:
            await message.answer("Это ваша собственная ссылка. Отправьте её другим людям.")
            return
        set_session(user_id, link["id"])
        await message.answer(
            "Задавай вопрос или отправляй сообщение. Оно будет передано владельцу ссылки анонимно.\n\n"
            "Чтобы выбрать другую ссылку, просто открой её. Для отмены используй /cancel."
        )
        return

    token = get_active_link(user_id)
    if token is None:
        token = create_link(user_id)
    me = await message.bot.get_me()
    await message.answer(
        "Привет! Я помогу получать анонимные сообщения.\n\n"
        f"Твоя ссылка:\n{link_url(me.username, token)}\n\n"
        "Поделись ей, чтобы люди могли написать тебе.\n"
        "Команды: /link, /disable, /enable, /cancel, /help"
    )


@router.message(Command("link"))
async def show_link(message: Message) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    ensure_user(message.from_user.id)
    token = secrets.token_urlsafe(TOKEN_LENGTH)
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO links (owner_telegram_id, token, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (message.from_user.id, token, token_hash(token), now()),
        )
    me = await message.bot.get_me()
    await message.answer(f"Твоя ссылка:\n{link_url(me.username, token)}")


@router.message(Command("disable"))
async def disable_link(message: Message) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    with connect_db() as connection:
        connection.execute(
            "UPDATE links SET active = 0, revoked_at = ? WHERE owner_telegram_id = ? AND active = 1",
            (now(), message.from_user.id),
        )
    await message.answer("Приём новых сообщений отключён. Ссылка больше не работает.")


@router.message(Command("enable"))
async def enable_link(message: Message) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    ensure_user(message.from_user.id)
    token = create_link(message.from_user.id)
    me = await message.bot.get_me()
    await message.answer(f"Создал новую активную ссылку:\n{link_url(me.username, token)}")


@router.message(Command("cancel"))
async def cancel(message: Message) -> None:
    if message.from_user:
        clear_session(message.from_user.id)
    await message.answer("Текущий адресат сброшен.")


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Открой ссылку владельца, чтобы отправить ему анонимное текстовое сообщение.\n\n"
        "/link — показать новую ссылку\n"
        "/disable — отключить активную ссылку\n"
        "/enable — создать новую ссылку\n"
        "/cancel — отменить текущий режим отправки"
    )


def normalized_content_type(message: Message) -> str:
    content_type = message.content_type
    value = content_type.value if hasattr(content_type, "value") else str(content_type)
    return value.lower().split(".")[-1]


def caption_for_delivery(caption: str | None, header: str = ANONYMOUS_HEADER) -> str:
    if not caption:
        return header
    return f"{header}\n\n{caption}"


def media_object(message: Message, content_type: str):
    if content_type == "photo":
        return message.photo[-1] if message.photo else None
    return getattr(message, content_type, None)


async def send_anonymous_message(
    message: Message,
    recipient_id: int,
    content_type: str,
    *,
    header: str = ANONYMOUS_HEADER,
    reply_parameters: ReplyParameters | None = None,
):
    bot = message.bot
    caption = getattr(message, "caption", None)
    caption_text = caption_for_delivery(caption, header)
    reply_kwargs = {"reply_parameters": reply_parameters} if reply_parameters else {}

    if content_type == "text":
        return [await bot.send_message(recipient_id, f"{header}:\n\n{message.text}", **reply_kwargs)]
    if content_type == "photo":
        return [await bot.send_photo(recipient_id, message.photo[-1].file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "video":
        return [await bot.send_video(recipient_id, message.video.file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "voice":
        return [await bot.send_voice(recipient_id, message.voice.file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "audio":
        return [await bot.send_audio(recipient_id, message.audio.file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "document":
        return [await bot.send_document(recipient_id, message.document.file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "animation":
        return [await bot.send_animation(recipient_id, message.animation.file_id, caption=caption_text, **reply_kwargs)]
    if content_type == "video_note":
        media = await bot.send_video_note(recipient_id, message.video_note.file_id, **reply_kwargs)
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        return [media, header_message]
    if content_type == "sticker":
        media = await bot.send_sticker(recipient_id, message.sticker.file_id, **reply_kwargs)
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        return [media, header_message]
    if content_type == "contact":
        contact = message.contact
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        media = await bot.send_contact(
            recipient_id,
            phone_number=contact.phone_number,
            first_name=contact.first_name,
            last_name=contact.last_name,
            vcard=contact.vcard,
            **reply_kwargs,
        )
        return [header_message, media]
    if content_type == "location":
        location = message.location
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        media = await bot.send_location(
            recipient_id,
            latitude=location.latitude,
            longitude=location.longitude,
            horizontal_accuracy=location.horizontal_accuracy,
            live_period=location.live_period,
            heading=location.heading,
            proximity_alert_radius=location.proximity_alert_radius,
            **reply_kwargs,
        )
        return [header_message, media]
    if content_type == "venue":
        venue = message.venue
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        media = await bot.send_venue(
            recipient_id,
            latitude=venue.location.latitude,
            longitude=venue.location.longitude,
            title=venue.title,
            address=venue.address,
            foursquare_id=venue.foursquare_id,
            foursquare_type=venue.foursquare_type,
            google_place_id=venue.google_place_id,
            google_place_type=venue.google_place_type,
            **reply_kwargs,
        )
        return [header_message, media]
    if content_type == "poll":
        poll = message.poll
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        kwargs = {
            "chat_id": recipient_id,
            "question": poll.question,
            "options": [option.text for option in poll.options],
            "is_anonymous": True,
            "type": poll.type,
            "allows_multiple_answers": poll.allows_multiple_answers,
        }
        kwargs.update(reply_kwargs)
        if poll.type == "quiz" and poll.correct_option_id is not None:
            kwargs["correct_option_id"] = poll.correct_option_id
            kwargs["explanation"] = poll.explanation
        media = await bot.send_poll(**kwargs)
        return [header_message, media]
    if content_type == "dice":
        header_message = await bot.send_message(recipient_id, header, **reply_kwargs)
        media = await bot.send_dice(recipient_id, emoji=message.dice.emoji, **reply_kwargs)
        return [header_message, media]

    raise ValueError(f"Unsupported content type: {content_type}")


@router.message(F.chat.type == "private", F.reply_to_message)
async def reply_to_question(message: Message) -> None:
    if not message.from_user or not message.reply_to_message:
        return

    target = get_reply_target(message.chat.id, message.reply_to_message.message_id)
    if not target:
        await receive_message(message)
        return

    content_type = normalized_content_type(message)
    if content_type not in SUPPORTED_CONTENT_TYPES:
        await message.answer("Этот тип ответа пока не поддерживается.")
        return
    if content_type == "text" and (not message.text or len(message.text) > MAX_MESSAGE_LENGTH):
        await message.answer(f"Ответ слишком длинный. Максимум: {MAX_MESSAGE_LENGTH} символов.")
        return
    caption = getattr(message, "caption", None)
    if caption and len(caption) > MAX_CAPTION_LENGTH:
        await message.answer(f"Подпись слишком длинная. Максимум: {MAX_CAPTION_LENGTH} символов.")
        return

    file = media_object(message, content_type)
    file_size = getattr(file, "file_size", None)
    if file_size and file_size > MAX_MEDIA_SIZE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер: 50 МБ.")
        return

    reply_parameters = ReplyParameters(
        message_id=target["sender_message_id"],
        allow_sending_without_reply=True,
    )
    try:
        await send_anonymous_message(
            message,
            target["sender_chat_id"],
            content_type,
            header=ANSWER_HEADER,
            reply_parameters=reply_parameters,
        )
    except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError, ValueError):
        await message.answer("Не удалось доставить ответ отправителю.")
        return

    delete_reply_targets(target["question_id"])
    await message.answer("Ответ отправлен анонимно.")


@router.message(F.chat.type == "private")
async def receive_message(message: Message) -> None:
    if not message.from_user:
        return

    content_type = normalized_content_type(message)
    if content_type == "text" and message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Используй /help для справки.")
        return
    if content_type not in SUPPORTED_CONTENT_TYPES:
        await message.answer("Этот тип сообщения пока не поддерживается.")
        return

    session = get_session(message.from_user.id)
    if not session:
        await message.answer("Сначала открой персональную ссылку получателя.")
        return

    caption = getattr(message, "caption", None)
    if content_type == "text" and (not message.text or len(message.text) > MAX_MESSAGE_LENGTH):
        await message.answer(f"Сообщение слишком длинное. Максимум: {MAX_MESSAGE_LENGTH} символов.")
        return
    if caption and len(caption) > MAX_CAPTION_LENGTH:
        await message.answer(f"Подпись слишком длинная. Максимум: {MAX_CAPTION_LENGTH} символов.")
        return

    file = media_object(message, content_type)
    file_size = getattr(file, "file_size", None)
    if file_size and file_size > MAX_MEDIA_SIZE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер: 50 МБ.")
        return

    media_group_id = message.media_group_id
    if recently_sent(message.from_user.id, media_group_id):
        await message.answer(f"Подожди {RATE_LIMIT_SECONDS} секунд перед следующим сообщением.")
        return

    try:
        sent_messages = await send_anonymous_message(
            message, session["owner_telegram_id"], content_type
        )
    except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError, ValueError):
        await message.answer("Не удалось доставить сообщение: получатель отключил бота или ссылку.")
        clear_session(message.from_user.id)
        return

    file_id = getattr(file, "file_id", None)
    file_unique_id = getattr(file, "file_unique_id", None)
    metadata = {}
    if content_type == "audio":
        metadata = {"duration": message.audio.duration, "performer": message.audio.performer, "title": message.audio.title}
    elif content_type in {"video", "animation"}:
        media = getattr(message, content_type)
        metadata = {"duration": media.duration, "width": media.width, "height": media.height}
    elif content_type == "photo":
        metadata = {"width": file.width, "height": file.height}

    question_id = save_message(
        session["link_id"],
        message.from_user.id,
        session["owner_telegram_id"],
        content_type=content_type,
        text=message.text or "",
        file_id=file_id,
        file_unique_id=file_unique_id,
        caption=caption,
        file_name=getattr(file, "file_name", None),
        mime_type=getattr(file, "mime_type", None),
        media_group_id=media_group_id,
        metadata=metadata,
        telegram_message_id=sent_messages[-1].message_id,
    )
    save_reply_targets(
        question_id,
        session["owner_telegram_id"],
        [sent_message.message_id for sent_message in sent_messages],
        message.chat.id,
        message.message_id,
    )
    await message.answer("Сообщение отправлено анонимно.")


async def main() -> None:
    init_db()
    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
