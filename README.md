# Anonymous Q&A Bot

A local Telegram bot for receiving anonymous messages through a personal link.

Each owner gets a link in the following format:

```
https://t.me/<bot_username>?start=<token>
```

Another user opens the link, sends a message, and the bot delivers it to the owner. The owner receives the message from the bot and cannot see the sender's Telegram profile.

## Features

* a personal link for each user;
* persistent storage of links and sessions in SQLite;
* disabling and regenerating personal links;
* support for text, photos, videos, voice messages, video notes, audio files, documents, GIFs, animations, and stickers;
* support for contacts, locations, venues, polls, and dice emojis;
* message length limit of 2000 characters;
* message rate limiting;
* handling of invalid and disabled links;
* owner replies through Telegram Reply on anonymous messages;
* replies can contain text or any supported media type.

## Requirements

* Python 3.12 or newer;
* a Telegram bot created through [@BotFather](https://t.me/BotFather).

## Installation

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```dotenv
BOT_TOKEN=token_from_BotFather
```

## Running

```bash
python main.py
```

The bot uses long polling. The `qna.sqlite3` database is created automatically on the first launch.

## Commands

| Command    | Action                                         |
| ---------- | ---------------------------------------------- |
| `/start`   | Show the active personal link or create one    |
| `/link`    | Create a new personal link                     |
| `/disable` | Disable all active links                       |
| `/enable`  | Create a new active link                       |
| `/cancel`  | Clear the currently selected message recipient |
| `/help`    | Show help                                      |

## Testing

A full test requires two Telegram accounts:

1. Open the bot with the first account and send `/start`.
2. Copy the generated link.
3. Open the link with the second account.
4. Send a text message.
5. Make sure the message is delivered to the link owner from the bot.
6. Reply to the received message using Telegram Reply.
7. Make sure the reply is delivered to the sender without exposing the sender's profile to the owner.

## MVP Limitations

* media albums are delivered as separate messages;
* Telegram system messages and unsupported entities are not forwarded;
* a local SQLite database is used.

## Anonymity Risks

* The bot technically receives the Telegram IDs of both the sender and the owner, so complete anonymity from Telegram, the server owner, and the developer cannot be guaranteed.
* `qna.sqlite3` stores the sender, the owner, the question content, and its delivery history. This allows the server owner to reconstruct the connection between users.
* To support Reply, the bot temporarily stores a link between the owner's notification message and the original sender. This temporary mapping is deleted after the reply is successfully delivered, but the main question record remains in SQLite.
* The text and content of the owner's reply are not stored separately in SQLite, although Telegram and server logs may temporarily contain them.
* The owner may be able to indirectly identify the sender based on writing style, message content, timing, and the sequence of replies.
* Telegram can see the participants and messages passing through the bot.
* Access to the server, database file, or logs may expose senders, owners, and message history.
* Replies are sent on behalf of the bot, but Telegram may show the sender that the response is a Reply to their original message.
# Анонимный Q&A-бот

Локальный Telegram-бот для приема анонимных текстовых сообщений по персональной ссылке.

Владелец получает ссылку вида:

```text
https://t.me/<bot_username>?start=<token>
```

Другой пользователь открывает ссылку, пишет сообщение, а бот доставляет его владельцу. Владелец видит сообщение от имени бота и не видит Telegram-профиль отправителя.

## Возможности

- персональная ссылка для каждого пользователя;
- постоянное хранение ссылок и сессий в SQLite;
- отключение и перевыпуск ссылок;
- прием текста, фото, видео, голосовых, кружков, аудио, файлов, GIF, анимаций и стикеров;
- прием контактов, геолокаций, мест, опросов и эмодзи-кубиков;
- ограничение сообщения до 2000 символов;
- ограничение частоты отправки сообщений;
- обработка невалидных и отключенных ссылок.
- ответы владельца через Reply на анонимное сообщение;
- поддержка ответов текстом и всеми поддерживаемыми типами медиа.

## Требования

- Python 3.12 или новее;
- Telegram-бот, созданный через [@BotFather](https://t.me/BotFather).

## Установка

Создай виртуальное окружение и установи зависимости:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Создай файл `.env` в корне проекта:

```dotenv
BOT_TOKEN=токен_от_BotFather
```

## Запуск

```bash
python main.py
```

Бот работает через long polling. База данных `qna.sqlite3` создается автоматически при первом запуске.

## Команды

| Команда | Действие |
| --- | --- |
| `/start` | Показать активную персональную ссылку или создать её |
| `/link` | Создать новую персональную ссылку |
| `/disable` | Отключить все активные ссылки |
| `/enable` | Создать новую активную ссылку |
| `/cancel` | Сбросить выбранного получателя сообщения |
| `/help` | Показать справку |

## Проверка

Для полноценной проверки нужны два Telegram-аккаунта:

1. На первом аккаунте открыть бота и отправить `/start`.
2. Скопировать полученную ссылку.
3. На втором аккаунте открыть эту ссылку.
4. Отправить текстовое сообщение.
5. Убедиться, что первое сообщение пришло владельцу ссылки от имени бота.
6. Ответить на полученное сообщение через Reply.
7. Убедиться, что ответ пришёл отправителю без раскрытия его профиля владельцу.

## Ограничения MVP

- альбомы доставляются отдельными сообщениями;
- системные сообщения Telegram и неподдерживаемые сущности не пересылаются;
- используется локальный SQLite;

## Риски анонимности

- Бот технически получает Telegram ID отправителя и владельца, поэтому полная анонимность от Telegram, владельца сервера и разработчика не гарантируется.
- В `qna.sqlite3` сохраняются отправитель, владелец, содержимое вопроса и история его доставки. Это позволяет владельцу сервера восстановить связь между людьми.
- Для Reply временно сохраняется связь между сообщением-уведомлением владельца и исходным отправителем. После успешной доставки ответа эта временная связь удаляется, но основная запись вопроса остаётся в SQLite.
- Текст и содержимое ответа владельца отдельно в SQLite не сохраняются, но Telegram и серверные логи могут их временно содержать.
- Владелец может косвенно установить личность отправителя по стилю, содержанию, времени и последовательности ответов.
- Telegram видит участников и сообщения, проходящие через бота.
- Доступ к серверу, файлу базы данных или логам может раскрыть отправителей, владельцев и переписку.
- Ответы отправляются от имени бота, но Telegram может показать отправителю, что ответ является Reply на его исходное сообщение.
