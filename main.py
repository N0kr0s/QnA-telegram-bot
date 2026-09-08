import asyncio
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "qna.sqlite3"
TOKEN_LENGTH = 32
MAX_MESSAGE_LENGTH = 2000
RATE_LIMIT_SECONDS = 10

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
                text TEXT NOT NULL,
                telegram_message_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_sender_time
                ON messages(sender_telegram_id, created_at);
            """
        )


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


def recently_sent(sender_id: int) -> bool:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT created_at FROM messages WHERE sender_telegram_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
    return bool(row and now() - row["created_at"] < RATE_LIMIT_SECONDS)


def save_message(link_id: int, sender_id: int, recipient_id: int, text: str) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO messages (link_id, sender_telegram_id, recipient_telegram_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (link_id, sender_id, recipient_id, text, now()),
        )


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


@router.message(F.chat.type == "private", F.text)
async def receive_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    session = get_session(message.from_user.id)
    if not session:
        await message.answer("Сначала открой персональную ссылку получателя.")
        return
    if len(message.text) > MAX_MESSAGE_LENGTH:
        await message.answer(f"Сообщение слишком длинное. Максимум: {MAX_MESSAGE_LENGTH} символов.")
        return
    if recently_sent(message.from_user.id):
        await message.answer(f"Подожди {RATE_LIMIT_SECONDS} секунд перед следующим сообщением.")
        return

    try:
        await message.bot.send_message(
            chat_id=session["owner_telegram_id"],
            text=f"📨 Анонимное сообщение:\n\n{message.text}",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer("Не удалось доставить сообщение: получатель отключил бота или ссылку.")
        clear_session(message.from_user.id)
        return

    save_message(session["link_id"], message.from_user.id, session["owner_telegram_id"], message.text)
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
