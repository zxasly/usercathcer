import asyncio
import random
import string
import logging
import json
import os
from datetime import date, datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Убираем спам от httpx и telegram polling
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BOT_TOKEN = "8881482663:AAFDXJlf_XVrgD9ZC0pe5UeoplC1T73jBxE"

# Вставь сюда свой Telegram user_id (узнать: @userinfobot)
ADMIN_IDS: set[int] = {1421793035}

DAILY_LIMIT = 3
DATA_FILE = "data.json"  # файл для хранения данных между перезапусками

user_attempts: dict[int, dict] = defaultdict(lambda: {"date": None, "count": 0})

# Premium: {user_id: datetime | None}  (None = навсегда)
premium_users: dict[int, datetime | None] = {}

# Забаненные пользователи
banned_users: set[int] = set()

# Ловушки: {username: [user_id, ...]}
traps: dict[str, list[int]] = defaultdict(list)

# Заявки на Premium: {request_id: {"user_id": int, "username": str, "display": str, "time": datetime}}
purchase_requests: dict[str, dict] = {}
# Маппинг user_id -> request_id (чтобы проверять дубли)
user_request_map: dict[int, str] = {}

# Кулдаун после отмены заявки: {user_id: datetime}
request_cooldown: dict[int, datetime] = {}

# Все пользователи бота: {user_id: {"username": str, "display": str, "first_seen": str}}
known_users: dict[int, dict] = {}

# Обязательные каналы для подписки: [{"id": int/str, "title": str, "link": str}]
required_channels: list[dict] = []

LETTERS = string.ascii_lowercase
CHARS_WITH_DIGITS = string.ascii_lowercase + string.digits
CHARS_NO_DIGITS = string.ascii_lowercase


# ---------------------------------------------------------------------------
# Сохранение / загрузка данных (JSON)
# ---------------------------------------------------------------------------

def save_data():
    """Сохраняет все данные в файл."""
    data = {
        "premium": {},
        "banned": list(banned_users),
        "traps": {k: v for k, v in traps.items()},
        "purchase_requests": {},
        "request_cooldown": {},
        "known_users": {str(k): v for k, v in known_users.items()},
        "required_channels": required_channels,
    }
    for uid, exp in premium_users.items():
        data["premium"][str(uid)] = exp.isoformat() if exp else None
    for uid, req in purchase_requests.items():
        data["purchase_requests"][str(uid)] = {
            "user_id": req["user_id"],
            "username": req["username"],
            "display": req["display"],
            "time": req["time"].isoformat(),
        }
    for uid, dt in request_cooldown.items():
        data["request_cooldown"][str(uid)] = dt.isoformat()
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")


def load_data():
    """Загружает данные из файла при старте."""
    global premium_users, banned_users
    if not os.path.exists(DATA_FILE):
        logger.info("Файл данных не найден, начинаем с чистого листа.")
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Premium
        for uid_str, exp_str in data.get("premium", {}).items():
            uid = int(uid_str)
            exp = datetime.fromisoformat(exp_str) if exp_str else None
            if exp is None or datetime.now() < exp:
                premium_users[uid] = exp

        # Баны
        banned_users = set(data.get("banned", []))

        # Ловушки
        for username, user_ids in data.get("traps", {}).items():
            traps[username] = user_ids

        # Заявки на покупку
        for req_id, req in data.get("purchase_requests", {}).items():
            purchase_requests[req_id] = {
                "user_id": req.get("user_id", int(req_id)),
                "username": req["username"],
                "display": req["display"],
                "time": datetime.fromisoformat(req["time"]),
            }
            user_request_map[req.get("user_id", int(req_id))] = req_id

        # Кулдауны
        for uid_str, dt_str in data.get("request_cooldown", {}).items():
            dt = datetime.fromisoformat(dt_str)
            if datetime.now() < dt:  # загружаем только актуальные
                request_cooldown[int(uid_str)] = dt

        # Известные пользователи
        for uid_str, udata in data.get("known_users", {}).items():
            known_users[int(uid_str)] = udata

        # Обязательные каналы
        for ch in data.get("required_channels", []):
            required_channels.append(ch)

        logger.info(
            f"Данные загружены: {len(premium_users)} premium, "
            f"{len(banned_users)} банов, {len(traps)} ловушек, "
            f"{len(purchase_requests)} заявок, "
            f"{len(required_channels)} каналов."
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")


# ---------------------------------------------------------------------------
# Хелперы: Premium / бан
# ---------------------------------------------------------------------------

def is_premium(user_id: int) -> bool:
    if user_id not in premium_users:
        return False
    expires = premium_users[user_id]
    if expires is None:
        return True  # навсегда
    if datetime.now() < expires:
        return True
    # истёк — убираем
    del premium_users[user_id]
    return False


def grant_premium(user_id: int, days: int):
    """days=0 → навсегда, иначе на N дней."""
    if days == 0:
        premium_users[user_id] = None
    else:
        premium_users[user_id] = datetime.now() + timedelta(days=days)
    save_data()


def revoke_premium(user_id: int):
    premium_users.pop(user_id, None)
    save_data()


def premium_expires_str(user_id: int) -> str:
    if user_id not in premium_users:
        return "нет"
    expires = premium_users[user_id]
    if expires is None:
        return "навсегда"
    return expires.strftime("%d.%m.%Y %H:%M")


def is_banned(user_id: int) -> bool:
    return user_id in banned_users


def attempts_str(user_id: int) -> str:
    """Возвращает строку с остатком попыток. Для Premium — ∞."""
    if is_premium(user_id):
        return "♾️ безлимитно"
    return str(get_remaining(user_id))
    """Генерирует короткий уникальный ID заявки вида REQ-XXXX."""
    while True:
        rid = f"REQ-{random.randint(1000, 9999)}"
        if rid not in purchase_requests:
            return rid


# ---------------------------------------------------------------------------
# Проверка подписки на каналы
# ---------------------------------------------------------------------------

async def check_subscriptions(bot: Bot, user_id: int) -> list[dict]:
    """Возвращает список каналов на которые пользователь НЕ подписан."""
    not_subscribed = []
    for ch in required_channels:
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


def subscription_wall_text(missing: list[dict]) -> str:
    lines = "\n".join(f"• <a href='{ch['link']}'>{ch['title']}</a>" for ch in missing)
    return (
        "🔒 <b>Для использования бота необходимо подписаться:</b>\n\n"
        f"{lines}\n\n"
        "После подписки нажми кнопку ниже."
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ])


# ---------------------------------------------------------------------------
# Генерация случайных ников (чистый рандом)
# ---------------------------------------------------------------------------

def generate_username(length: int, with_digits: bool) -> str:
    """Первый символ — буква, остальные — буквы + цифры (если with_digits)."""
    chars = CHARS_WITH_DIGITS if with_digits else CHARS_NO_DIGITS
    first = random.choice(LETTERS)
    rest = "".join(random.choices(chars, k=length - 1))
    return first + rest


# ---------------------------------------------------------------------------
# Оценка ника
# ---------------------------------------------------------------------------

def score_username(username: str) -> dict:
    """
    Считает ликвидность (1–10) и выдаёт грейд.
    Критерии:
    - Только буквы (без цифр/подчёркиваний) → +3
    - Все буквы разные → +1
    - Легко читается (нет сложных сочетаний) → +2
    - Короткое (5 букв лучше 6) → +1
    - Похоже на слово (есть гласные) → +2
    - Нет повторяющихся символов подряд → +1
    """
    score = 0
    u = username.lower()

    # Только буквы
    if u.isalpha():
        score += 3

    # Все символы уникальны
    if len(set(u)) == len(u):
        score += 1

    # Наличие гласных (читаемость)
    vowels = set("aeiou")
    vowel_count = sum(1 for c in u if c in vowels)
    vowel_ratio = vowel_count / len(u)
    if vowel_ratio >= 0.3:
        score += 2
    elif vowel_ratio >= 0.15:
        score += 1

    # Длина
    if len(u) == 5:
        score += 1

    # Нет двух одинаковых символов подряд
    has_double = any(u[i] == u[i+1] for i in range(len(u)-1))
    if not has_double:
        score += 1

    # Нет цифр — чище выглядит
    if u.isalpha():
        score += 1  # уже посчитано выше, но добавим ещё за "чистоту"
    else:
        score = max(1, score - 1)

    score = min(10, max(1, score))

    # Грейд
    if score >= 9:
        grade = "👑 Легенда"
    elif score >= 7:
        grade = "💎 Премиум"
    elif score >= 5:
        grade = "⭐ Хороший"
    elif score >= 3:
        grade = "📦 Обычный"
    else:
        grade = "🗑 Слабый"

    return {"score": score, "grade": grade}


# ---------------------------------------------------------------------------
# Оценка стоимости ника
# ---------------------------------------------------------------------------

def estimate_price(username: str) -> dict:
    """
    Оценивает примерную стоимость ника на Fragment в TON и USD.
    Базируется на длине, составе и читаемости.
    """
    u = username.lower()
    length = len(u)

    # Базовая цена по длине (TON)
    base = {4: 5000, 5: 500, 6: 50, 7: 10, 8: 5}.get(length, 3)

    multiplier = 1.0

    # Только буквы — дороже
    if u.isalpha():
        multiplier *= 2.5

    # Все уникальные символы
    if len(set(u)) == len(u):
        multiplier *= 1.3

    # Гласные — читаемость
    vowels = set("aeiou")
    vowel_ratio = sum(1 for c in u if c in vowels) / length
    if vowel_ratio >= 0.4:
        multiplier *= 1.5
    elif vowel_ratio >= 0.2:
        multiplier *= 1.2

    # Нет повторов подряд
    if not any(u[i] == u[i+1] for i in range(length - 1)):
        multiplier *= 1.2

    # Цифры снижают цену
    if any(c.isdigit() for c in u):
        multiplier *= 0.4

    ton_price = base * multiplier
    usd_price = ton_price * 6.5  # примерный курс TON/USD

    # Диапазон ±30%
    low_ton = ton_price * 0.7
    high_ton = ton_price * 1.3
    low_usd = usd_price * 0.7
    high_usd = usd_price * 1.3

    def fmt(n: float) -> str:
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return f"{n:.0f}"

    return {
        "ton_low": fmt(low_ton),
        "ton_high": fmt(high_ton),
        "usd_low": fmt(low_usd),
        "usd_high": fmt(high_usd),
        "ton_mid": fmt(ton_price),
    }


async def find_similar_username(bot: Bot, username: str, max_tries: int = 200) -> str | None:
    """
    Ищет свободный ник похожий на заданный:
    перестановки букв, замена одной буквы, добавление/удаление символа.
    """
    u = username.lower()
    candidates = set()

    # 1. Перестановки пар соседних букв
    for i in range(len(u) - 1):
        lst = list(u)
        lst[i], lst[i+1] = lst[i+1], lst[i]
        candidates.add("".join(lst))

    # 2. Замена каждой буквы на соседнюю по алфавиту
    for i in range(len(u)):
        for delta in (-1, 1):
            c = chr(ord(u[i]) + delta)
            if c.isalpha():
                candidates.add(u[:i] + c + u[i+1:])

    # 3. Добавить букву в конец
    for c in "aeiou":
        candidates.add(u + c)
        if len(u) > 5:
            candidates.add(u[:-1] + c)

    # 4. Случайные мутации
    for _ in range(max_tries - len(candidates)):
        idx = random.randint(0, len(u) - 1)
        c = random.choice(LETTERS)
        candidates.add(u[:idx] + c + u[idx+1:])

    # Фильтруем: только валидные (начинается с буквы, нужная длина ±1)
    valid = [
        c for c in candidates
        if c[0].isalpha() and abs(len(c) - len(u)) <= 1 and len(c) >= 5
    ]
    random.shuffle(valid)

    for candidate in valid[:max_tries]:
        result = await is_username_free(bot, candidate)
        if result is True:
            return candidate
        await asyncio.sleep(0.1)

    return None

import urllib.request
import urllib.error

# Кеш проверок: {username: bool}
_username_cache: dict[str, bool] = {}
_fragment_cache: dict[str, bool] = {}

# ============================================================
# Telethon клиент для быстрой проверки ников (без FloodWait)
# Вставь свои данные с my.telegram.org
# ============================================================
TG_API_ID = 34401324          # <-- вставь api_id (число)
TG_API_HASH = "f2e55ac1fc53a5701645e2db510fef02"       # <-- вставь api_hash (строка)
TG_SESSION = "checker" # имя файла сессии

_telethon_client = None
_telethon_flood_until: float = 0.0  # время до которого Telethon в бане


async def get_telethon():
    """Возвращает запущенный Telethon клиент."""
    global _telethon_client
    if _telethon_client is not None and _telethon_client.is_connected():
        return _telethon_client
    if not TG_API_ID or not TG_API_HASH:
        return None
    try:
        from telethon import TelegramClient
        client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
        # Авторизуемся как бот
        await client.start(bot_token=BOT_TOKEN)
        _telethon_client = client
        logger.info("✅ Telethon клиент запущен — быстрый поиск активен")
        return client
    except Exception as e:
        logger.warning(f"Telethon не запустился: {e}")
        logger.warning("Используется getChat fallback (медленнее)")
        return None


# Rate limiter для getChat fallback
_getchat_lock = asyncio.Lock()
_last_getchat: float = 0.0
GETCHAT_INTERVAL = 1.2


async def is_username_free(bot: Bot, username: str) -> bool | None:
    """
    Проверяет ник:
    1. Через Telethon (быстро, без FloodWait) — если настроен
    2. Через getChat с rate limiting — fallback
    True=свободен, False=занят, None=ошибка
    """
    global _last_getchat, GETCHAT_INTERVAL
    u = username.lower()

    if u in _username_cache:
        return _username_cache[u]

    # --- Telethon (быстро) ---
    tele = await get_telethon()
    if tele:
        try:
            from telethon.tl.functions.contacts import ResolveUsernameRequest
            from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError, FloodWaitError
            try:
                result = await tele(ResolveUsernameRequest(u))
                # Нашли — занят
                _username_cache[u] = False
                return False
            except (UsernameNotOccupiedError, UsernameInvalidError):
                _username_cache[u] = True
                return True
            except FloodWaitError as e:
                logger.warning(f"Telethon FloodWait {e.seconds}с")
                await asyncio.sleep(min(e.seconds, 30))
                return None
            except Exception as e:
                logger.debug(f"Telethon error @{u}: {e}")
                # Падаем на getChat
        except ImportError:
            pass

    # --- getChat fallback с rate limiting ---
    async with _getchat_lock:
        now = asyncio.get_event_loop().time()
        wait = GETCHAT_INTERVAL - (now - _last_getchat)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_getchat = asyncio.get_event_loop().time()

        try:
            await bot.get_chat(f"@{u}")
            _username_cache[u] = False
            return False
        except TelegramError as e:
            msg = str(e).lower()
            if any(x in msg for x in (
                "chat not found", "invalid username", "username not found",
                "user not found", "peer_id_invalid", "username_not_occupied",
                "username_invalid",
            )):
                _username_cache[u] = True
                return True
            if "flood" in msg or "too many" in msg or "retry" in msg:
                import re as _re
                m = _re.search(r"retry in (\d+)", msg)
                flood_wait = int(m.group(1)) if m else 30
                flood_wait = min(flood_wait, 60)
                GETCHAT_INTERVAL = min(GETCHAT_INTERVAL + 0.5, 5.0)
                logger.warning(f"FloodWait {flood_wait}с, интервал: {GETCHAT_INTERVAL:.1f}с")
                await asyncio.sleep(flood_wait)
                return None
            logger.debug(f"getChat error @{u}: {e}")
            return None


async def find_free_username(bot: Bot, length: int, with_digits: bool, max_tries: int = 500) -> str | None:
    seen = set()

    for i in range(max_tries):
        username = generate_username(length, with_digits)
        if username in seen:
            continue
        seen.add(username)

        result = await is_username_free(bot, username)

        if i % 20 == 0:
            logger.info(f"[Поиск] попытка {i+1}, @{username} → {result}")

        if result is True:
            logger.info(f"[Поиск] НАЙДЕН: @{username}")
            return username
        # None (ошибка сети) или False — просто идём дальше без паузы
        await asyncio.sleep(0.05)

    logger.warning(f"[Поиск] не нашёл за {max_tries} попыток")
    return None


# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------

def get_remaining(user_id: int) -> int:
    today = date.today()
    data = user_attempts[user_id]
    if data["date"] != today:
        data["date"] = today
        data["count"] = 0
    return DAILY_LIMIT - data["count"]


def use_attempt(user_id: int):
    today = date.today()
    data = user_attempts[user_id]
    if data["date"] != today:
        data["date"] = today
        data["count"] = 0
    data["count"] += 1


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 букв 💎 Premium", callback_data="len_5"),
            InlineKeyboardButton("6 букв", callback_data="len_6"),
        ],
        [
            InlineKeyboardButton("💰 Проверка стоимости", callback_data="price_menu"),
        ],
        [
            InlineKeyboardButton("🪤 Ловушка на ник", callback_data="trap_menu"),
            InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium"),
        ],
    ])


def digits_keyboard(length: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Без цифр", callback_data=f"search_{length}_nodigits"),
            InlineKeyboardButton("🔢 С цифрами", callback_data=f"search_{length}_digits"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ])


def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 букв 💎 Premium", callback_data="len_5"),
            InlineKeyboardButton("6 букв", callback_data="len_6"),
        ],
        [
            InlineKeyboardButton("💰 Проверка стоимости", callback_data="price_menu"),
        ],
        [
            InlineKeyboardButton("🪤 Ловушка на ник", callback_data="trap_menu"),
            InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium"),
        ],
    ])


def trap_keyboard(is_premium: bool) -> InlineKeyboardMarkup:
    if is_premium:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Указать юзернейм", callback_data="trap_set")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back")],
        ])


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

def main_text(user_id: int) -> str:
    att = attempts_str(user_id)
    return (
        "💎 *ПОИСК ЮЗЕРНЕЙМА*\n"
        "─────────────────────\n\n"
        "✔️ Каждый найденный ник проходит двойную проверку:\n"
        "• Telegram — не занят профилем, каналом или ботом\n"
        "• Fragment — не выставлен на аукцион или продажу\n\n"
        f"🎫 Осталось попыток сегодня: *{att}*\n\n"
        "Выберите раздел 👥"
    )


def digits_menu_text(length: int, user_id: int) -> str:
    att = attempts_str(user_id)
    return (
        f"💎 *Вы в разделе поиска {length} букв*\n\n"
        "Выберите ниже, найти юз с цифрами или без?\n\n"
        f"🎫 Осталось попыток: *{att}*"
    )


def result_text(username: str, length: int, user_id: int) -> str:
    ev = score_username(username)
    att = attempts_str(user_id)
    return (
        f"✅ <b>НИК НАЙДЕН!</b>\n\n"
        f"[ @{username}\n"
        f"  └ {length} букв\n\n"
        f"├ Ликвидность — {ev['score']}/10\n"
        f"├ Оценка — {ev['grade']}\n"
        f"└ ⚡ Свободен\n\n"
        f"🎫 Осталось попыток: <b>{att}</b>"
    )


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    user = update.effective_user
    user_id = user.id
    if is_banned(user_id):
        await update.message.reply_text("⛔ Вы заблокированы в этом боте.")
        return

    # Логируем пользователя
    uname = f"@{user.username}" if user.username else "нет"
    display = user.full_name or "—"
    if user_id not in known_users:
        known_users[user_id] = {
            "username": uname,
            "display": display,
            "first_seen": datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        save_data()
    else:
        known_users[user_id]["username"] = uname
        known_users[user_id]["display"] = display

    print(f"[/start] ID: {user_id} | {uname} | {display}")

    # Проверка подписки
    if required_channels:
        missing = await check_subscriptions(context.bot, user_id)
        if missing:
            await update.message.reply_text(
                subscription_wall_text(missing),
                parse_mode="HTML",
                reply_markup=subscription_keyboard(),
                disable_web_page_preview=True,
            )
            return

    await update.message.reply_text(
        main_text(user_id),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # Бан-проверка
    if is_banned(user_id):
        await query.answer("⛔ Вы заблокированы.", show_alert=True)
        return

    # Проверка подписки на каналы
    if data == "check_sub":
        if required_channels:
            missing = await check_subscriptions(context.bot, user_id)
            if missing:
                try:
                    await query.edit_message_text(
                        subscription_wall_text(missing),
                        parse_mode="HTML",
                        reply_markup=subscription_keyboard(),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass  # Message is not modified — игнорируем
                await query.answer("❌ Вы ещё не подписались на все каналы!", show_alert=True)
                return
        try:
            await query.edit_message_text(
                main_text(user_id),
                parse_mode="Markdown",
                reply_markup=main_keyboard(),
            )
        except Exception:
            pass
        return

    # Для всех остальных действий — тоже проверяем подписку
    if required_channels and data not in ("back",):
        missing = await check_subscriptions(context.bot, user_id)
        if missing:
            try:
                await query.edit_message_text(
                    subscription_wall_text(missing),
                    parse_mode="HTML",
                    reply_markup=subscription_keyboard(),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

    # --- Главное меню ---
    if data == "back":
        await query.edit_message_text(
            main_text(user_id),
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return

    # --- Выбор длины ---
    if data in ("len_5", "len_6"):
        length = 5 if data == "len_5" else 6

        # 5 букв — только для Premium
        if length == 5 and not is_premium(user_id):
            await query.edit_message_text(
                "💎 *Поиск 5-буквенных ников — только для Premium*\n\n"
                "5-значные юзернеймы — самые ценные и редкие.\n"
                "Оформи подписку чтобы получить доступ к поиску.\n\n"
                "Возможности Premium:\n"
                "• 🔍 Поиск 5-буквенных ников\n"
                "• 🪤 Ловушка на ник\n"
                "• ♾️ Безлимитные попытки поиска",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="back")],
                ]),
            )
            return

        await query.edit_message_text(
            digits_menu_text(length, user_id),
            parse_mode="Markdown",
            reply_markup=digits_keyboard(length),
        )
        return

    # --- Проверка стоимости ---
    if data == "price_menu":
        context.user_data["awaiting_price"] = True
        await query.edit_message_text(
            "💰 <b>Проверка стоимости ника</b>\n\n"
            "Введите юзернейм (без @) чтобы узнать примерную стоимость на Fragment.\n\n"
            "Если ник занят — найду похожий свободный.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
        )
        return

    # --- Ловушка на ник ---
    if data == "trap_menu":
        prem = is_premium(user_id)
        text = (
            "🪤 *Ловушка на ник* — функция Premium\n\n"
            "Укажите юзернейм, и как только он освободится — вы "
            "мгновенно получите уведомление.\n\n"
            + ("✅ У вас есть Premium-подписка" if prem else "💎 Требуется Premium-подписка")
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=trap_keyboard(prem),
        )
        return

    if data == "trap_set":
        if not is_premium(user_id):
            await query.answer("Требуется Premium!", show_alert=True)
            return
        context.user_data["awaiting_trap"] = True
        await query.edit_message_text(
            "🪤 *Ловушка на ник*\n\n"
            "Введите юзернейм (без @), за которым хотите следить:",
            parse_mode="Markdown",
        )
        return

    if data == "buy_premium":
        # Если уже есть заявка — показываем статус
        if user_id in user_request_map:
            req_id = user_request_map[user_id]
            await query.edit_message_text(
                f"💎 <b>Заявка на Premium</b>\n\n"
                f"✅ Вы уже создали обращение, ожидайте ответа администратора.\n"
                f"🆔 ID заявки: <code>{req_id}</code>\n\n"
                f"Обычно ответ приходит в течение нескольких минут.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отменить заявку", callback_data="cancel_request")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="back")],
                ]),
            )
            return

        # Проверяем кулдаун
        cooldown_until = request_cooldown.get(user_id)
        if cooldown_until and datetime.now() < cooldown_until:
            remaining_sec = int((cooldown_until - datetime.now()).total_seconds())
            mins = remaining_sec // 60
            secs = remaining_sec % 60
            await query.edit_message_text(
                f"⏳ <b>Подождите перед новой заявкой</b>\n\n"
                f"Осталось: <b>{mins}м {secs}с</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
            )
            return

        # Показываем цены
        await query.edit_message_text(
            "💎 <b>Premium-подписка</b>\n"
            "─────────────────────\n\n"
            "⭐ 1 день — 49⭐ / 49₽\n"
            "⭐ 7 дней — 99⭐ / 99₽\n"
            "⭐ 30 дней — 249⭐ / 249₽\n"
            "⭐ Навсегда — 499⭐ / 499₽\n\n"
            "Возможности Premium:\n"
            "• 🔍 Поиск 5-буквенных ников\n"
            "• 🪤 Ловушка на ник\n"
            "• ♾️ Безлимитные попытки поиска\n\n"
            "Нажми <b>Хочу купить</b> — администратор свяжется с тобой.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Хочу купить", callback_data="buy_premium_confirm")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")],
            ]),
        )
        return

    if data == "buy_premium_confirm":
        # Повторная проверка кулдауна и дубля
        if user_id in user_request_map:
            req_id = user_request_map[user_id]
            await query.edit_message_text(
                f"💎 <b>Заявка на Premium</b>\n\n"
                f"✅ Вы уже создали обращение, ожидайте ответа администратора.\n"
                f"🆔 ID заявки: <code>{req_id}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отменить заявку", callback_data="cancel_request")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="back")],
                ]),
            )
            return

        cooldown_until = request_cooldown.get(user_id)
        if cooldown_until and datetime.now() < cooldown_until:
            remaining_sec = int((cooldown_until - datetime.now()).total_seconds())
            await query.answer(f"⏳ Подождите ещё {remaining_sec // 60}м {remaining_sec % 60}с", show_alert=True)
            return

        # Создаём заявку
        user = query.from_user
        uname = f"@{user.username}" if user.username else "нет"
        display = user.full_name or "—"
        req_id = new_request_id()
        purchase_requests[req_id] = {
            "user_id": user_id,
            "username": uname,
            "display": display,
            "time": datetime.now(),
        }
        user_request_map[user_id] = req_id
        save_data()

        # Уведомляем всех админов
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🛒 <b>Новая заявка на Premium!</b>\n\n"
                    f"🆔 ID заявки: <code>{req_id}</code>\n"
                    f"👤 {display}\n"
                    f"🔗 {uname}\n"
                    f"🆔 User ID: <code>{user_id}</code>\n"
                    f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        await query.edit_message_text(
            f"✅ <b>Обращение создано!</b>\n\n"
            f"Вы создали обращение на покупку Premium.\n"
            f"Ожидайте — администратор свяжется с вами в ближайшее время.\n\n"
            f"🆔 ID заявки: <code>{req_id}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отменить заявку", callback_data="cancel_request")],
            ]),
        )
        return

    if data == "cancel_request":
        if user_id in user_request_map:
            req_id = user_request_map.pop(user_id)
            purchase_requests.pop(req_id, None)
            request_cooldown[user_id] = datetime.now() + timedelta(minutes=5)
            save_data()
            await query.edit_message_text(
                "❌ <b>Заявка отменена</b>\n\n"
                "⏳ Следующую заявку можно создать через <b>5 минут</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
            )
        else:
            await query.edit_message_text(
                "⚠️ Активной заявки нет.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
            )
        return

    # --- Поиск ---
    if data.startswith("search_"):
        parts = data.split("_")
        if len(parts) != 3:
            return
        length = int(parts[1])
        with_digits = parts[2] == "digits"

        # Premium — безлимит
        if not is_premium(user_id):
            remaining = get_remaining(user_id)
            if remaining <= 0:
                await query.edit_message_text(
                    "⛔ На сегодня попытки закончились.\nПриходи завтра!\n\n"
                    "💎 Купи Premium для безлимитного поиска.",
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )
                return
            use_attempt(user_id)

        digits_label = "с цифрами" if with_digits else "без цифр"
        await query.edit_message_text(
            f"🔍 Ищу свободный {length}-значный юзернейм ({digits_label})...\n"
            f"_(обычно занимает 5–15 секунд)_",
            parse_mode="Markdown",
        )

        bot: Bot = context.bot
        username = await find_free_username(bot, length, with_digits)
        remaining_after = get_remaining(user_id)

        if username:
            await query.edit_message_text(
                result_text(username, length, user_id),
                parse_mode="HTML",
                reply_markup=result_keyboard(),
            )
        else:
            await query.edit_message_text(
                "💎 *ПОИСК ЮЗЕРНЕЙМА*\n"
                "─────────────────────\n\n"
                "😔 Не удалось найти свободный ник.\n"
                "Попробуй ещё раз!\n\n"
                f"🎫 Осталось попыток сегодня: *{attempts_str(user_id)}*",
                parse_mode="Markdown",
                reply_markup=main_keyboard(),
            )
        return


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id

    if is_banned(user_id):
        await update.message.reply_text("⛔ Вы заблокированы в этом боте.")
        return

    text = update.message.text.strip().lstrip("@")

    # --- Ожидаем ввод юзернейма для проверки стоимости ---
    if context.user_data.get("awaiting_price"):
        context.user_data["awaiting_price"] = False
        username = text.lower()

        if not username.isalnum() or len(username) < 4:
            await update.message.reply_text(
                "⚠️ Некорректный юзернейм. Минимум 4 символа, только буквы и цифры.",
                reply_markup=main_keyboard(),
            )
            return

        status_msg = await update.message.reply_text(
            f"🔍 Проверяю <b>@{username}</b>...",
            parse_mode="HTML",
        )

        bot: Bot = context.bot
        is_free = await is_username_free(bot, username)
        price = estimate_price(username)
        ev = score_username(username)

        if is_free:
            text_out = (
                f"💰 <b>Оценка стоимости</b>\n\n"
                f"👤 @{username}\n"
                f"└ {len(username)} букв\n\n"
                f"├ Статус: ⚡ <b>Свободен</b>\n"
                f"├ Ликвидность: {ev['score']}/10\n"
                f"├ Оценка: {ev['grade']}\n\n"
                f"💎 Примерная стоимость:\n"
                f"├ TON: <b>{price['ton_low']} – {price['ton_high']}</b>\n"
                f"└ USD: <b>${price['usd_low']} – ${price['usd_high']}</b>\n\n"
                f"<i>Цена ориентировочная, реальная может отличаться.</i>"
            )
            await status_msg.edit_text(text_out, parse_mode="HTML", reply_markup=main_keyboard())
        else:
            # Ник занят — ищем похожий
            await status_msg.edit_text(
                f"🔍 @{username} занят. Ищу похожий свободный ник...",
                parse_mode="HTML",
            )
            similar = await find_similar_username(bot, username)

            if similar:
                sim_price = estimate_price(similar)
                sim_ev = score_username(similar)
                text_out = (
                    f"💰 <b>Оценка стоимости</b>\n\n"
                    f"👤 @{username} — ❌ занят\n\n"
                    f"🔄 Похожий свободный: <b>@{similar}</b>\n"
                    f"└ {len(similar)} букв\n\n"
                    f"├ Ликвидность: {sim_ev['score']}/10\n"
                    f"├ Оценка: {sim_ev['grade']}\n\n"
                    f"💎 Примерная стоимость @{similar}:\n"
                    f"├ TON: <b>{sim_price['ton_low']} – {sim_price['ton_high']}</b>\n"
                    f"└ USD: <b>${sim_price['usd_low']} – ${sim_price['usd_high']}</b>\n\n"
                    f"<i>Цена ориентировочная, реальная может отличаться.</i>"
                )
            else:
                text_out = (
                    f"💰 <b>Оценка стоимости</b>\n\n"
                    f"👤 @{username} — ❌ занят\n\n"
                    f"💎 Примерная стоимость если бы был свободен:\n"
                    f"├ TON: <b>{price['ton_low']} – {price['ton_high']}</b>\n"
                    f"└ USD: <b>${price['usd_low']} – ${price['usd_high']}</b>\n\n"
                    f"😔 Похожий свободный ник не найден.\n"
                    f"<i>Цена ориентировочная.</i>"
                )
            await status_msg.edit_text(text_out, parse_mode="HTML", reply_markup=main_keyboard())
        return

    # --- Ожидаем ввод юзернейма для ловушки ---
    if context.user_data.get("awaiting_trap"):
        context.user_data["awaiting_trap"] = False

        if not text.isalnum() or len(text) < 5:
            await update.message.reply_text(
                "⚠️ Некорректный юзернейм. Попробуй ещё раз.",
                reply_markup=main_keyboard(),
            )
            return

        traps[text.lower()].append(user_id)
        save_data()
        await update.message.reply_text(
            f"🪤 Ловушка установлена на <b>@{text.lower()}</b>\n\n"
            "Как только ник освободится — пришлю уведомление!",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    # Обычное сообщение — показываем главное меню
    await update.message.reply_text(
        main_text(user_id),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------

def admin_panel_text() -> str:
    total_premium = len(premium_users)
    total_banned = len(banned_users)
    pending = len(purchase_requests)
    return (
        "🛠 *Админ-панель*\n"
        "─────────────────────\n\n"
        f"👑 Premium пользователей: *{total_premium}*\n"
        f"🚫 Забанено: *{total_banned}*\n"
        f"🛒 Хотят купить Premium: *{pending}*\n\n"
        "Выберите действие:"
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    pending = len(purchase_requests)
    badge = f" ({pending})" if pending > 0 else ""
    ch_count = len(required_channels)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👑 Выдать Premium", callback_data="adm_give_prem"),
            InlineKeyboardButton("❌ Забрать Premium", callback_data="adm_revoke_prem"),
        ],
        [
            InlineKeyboardButton("🚫 Забанить", callback_data="adm_ban"),
            InlineKeyboardButton("✅ Разбанить", callback_data="adm_unban"),
        ],
        [
            InlineKeyboardButton(f"🛒 Хотят купить{badge}", callback_data="adm_requests"),
            InlineKeyboardButton("❌ Закрыть обращение", callback_data="adm_close_req"),
        ],
        [
            InlineKeyboardButton("📋 Список Premium", callback_data="adm_list_prem"),
            InlineKeyboardButton("📋 Список банов", callback_data="adm_list_ban"),
        ],
        [
            InlineKeyboardButton("👥 Все пользователи", callback_data="adm_users"),
        ],
        [
            InlineKeyboardButton(f"📢 Подписки ({ch_count})", callback_data="adm_channels"),
        ],
        [
            InlineKeyboardButton("📣 Рассылка", callback_data="adm_broadcast"),
        ],
    ])


def duration_keyboard(action: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора срока Premium."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 день", callback_data=f"{action}_1"),
            InlineKeyboardButton("7 дней", callback_data=f"{action}_7"),
            InlineKeyboardButton("30 дней", callback_data=f"{action}_30"),
        ],
        [
            InlineKeyboardButton("♾️ Навсегда", callback_data=f"{action}_0"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="adm_back")],
    ])


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        admin_panel_text(),
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )


async def admin_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    await query.answer()
    data = query.data

    # Назад в панель
    if data == "adm_back":
        await query.edit_message_text(
            admin_panel_text(),
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        return

    # --- Выдать Premium: запросить ID ---
    if data == "adm_give_prem":
        context.user_data["adm_action"] = "give_prem"
        await query.edit_message_text(
            "👑 *Выдать Premium*\n\nВведите Telegram ID пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Забрать Premium: запросить ID ---
    if data == "adm_revoke_prem":
        context.user_data["adm_action"] = "revoke_prem"
        await query.edit_message_text(
            "❌ *Забрать Premium*\n\nВведите Telegram ID пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Забанить: запросить ID ---
    if data == "adm_ban":
        context.user_data["adm_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Забанить пользователя*\n\nВведите Telegram ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Разбанить: запросить ID ---
    if data == "adm_unban":
        context.user_data["adm_action"] = "unban"
        await query.edit_message_text(
            "✅ *Разбанить пользователя*\n\nВведите Telegram ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Список Premium ---
    if data == "adm_list_prem":
        if not premium_users:
            text = "👑 *Premium пользователи*\n\nСписок пуст."
        else:
            lines = []
            for uid, exp in premium_users.items():
                exp_str = "навсегда" if exp is None else exp.strftime("%d.%m.%Y")
                lines.append(f"• `{uid}` — до {exp_str}")
            text = "👑 *Premium пользователи:*\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Список банов ---
    if data == "adm_list_ban":
        if not banned_users:
            text = "🚫 *Забаненные*\n\nСписок пуст."
        else:
            lines = [f"• `{uid}`" for uid in banned_users]
            text = "🚫 *Забаненные пользователи:*\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Заявки на покупку ---
    if data == "adm_requests":
        if not purchase_requests:
            await query.edit_message_text(
                "🛒 *Заявки на Premium*\n\nЗаявок нет.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
            )
            return
        lines = []
        buttons = []
        for req_id, req in purchase_requests.items():
            t = req["time"].strftime("%d.%m %H:%M")
            lines.append(
                f"🆔 <code>{req_id}</code>\n"
                f"👤 {req['display']} | {req['username']}\n"
                f"🕐 {t}"
            )
            buttons.append([InlineKeyboardButton(
                f"💎 Выдать [{req_id}]",
                callback_data=f"adm_req_give_{req_id}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        text = "🛒 <b>Заявки на Premium:</b>\n\n" + "\n\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # --- Быстрая выдача Premium из заявки ---
    if data.startswith("adm_req_give_"):
        req_id = data[len("adm_req_give_"):]
        req = purchase_requests.get(req_id, {})
        if not req:
            await query.edit_message_text(
                "⚠️ Заявка не найдена.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
            )
            return
        target_id = req["user_id"]
        context.user_data["adm_target_id"] = target_id
        context.user_data["adm_req_id"] = req_id
        display = req.get("display", str(target_id))
        await query.edit_message_text(
            f"👑 Выдать Premium пользователю *{display}* (`{target_id}`)\n"
            f"🆔 Заявка: `{req_id}`\n\nВыберите срок:",
            parse_mode="Markdown",
            reply_markup=duration_keyboard("adm_dur"),
        )
        return

    # --- Все пользователи ---
    if data == "adm_users":
        if not known_users:
            text = "👥 *Пользователи*\n\nСписок пуст."
        else:
            lines = []
            for uid, udata in list(known_users.items())[-30:]:  # последние 30
                prem = "💎" if is_premium(uid) else ""
                ban = "🚫" if is_banned(uid) else ""
                lines.append(
                    f"{prem}{ban} {udata['display']} | {udata['username']} | `{uid}`"
                )
            total = len(known_users)
            text = f"👥 *Пользователи* (последние 30 из {total}):\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Закрыть обращение по ID ---
    if data == "adm_close_req":
        context.user_data["adm_action"] = "close_req"
        await query.edit_message_text(
            "❌ *Закрыть обращение*\n\n"
            "Введите ID заявки (например: `REQ-1234`)\n\n"
            "ID можно найти в разделе *Хотят купить*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Управление каналами ---
    if data == "adm_channels":
        lines = []
        buttons = []
        for i, ch in enumerate(required_channels):
            lines.append(f"• {ch['title']} ({ch['link']})")
            buttons.append([
                InlineKeyboardButton(f"🗑 Удалить: {ch['title'][:20]}", callback_data=f"adm_ch_del_{i}"),
                InlineKeyboardButton(f"🚪 Выйти", callback_data=f"adm_ch_leave_{i}"),
            ])
        text = (
            "📢 *Обязательные каналы*\n"
            "─────────────────────\n\n"
            + ("\n".join(lines) if lines else "Каналов нет.\n")
            + "\n\nДля добавления нажмите кнопку ниже."
        )
        buttons.append([InlineKeyboardButton("➕ Добавить канал", callback_data="adm_ch_add")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        return

    if data == "adm_ch_add":
        context.user_data["adm_action"] = "ch_add"
        await query.edit_message_text(
            "➕ *Добавить канал*\n\n"
            "Перешлите боту любое сообщение из канала, или введите username канала (например: `@mychannel`).\n\n"
            "⚠️ Бот должен быть администратором канала.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_channels")]]),
        )
        return

    if data.startswith("adm_ch_del_"):
        idx = int(data.split("_")[3])
        if 0 <= idx < len(required_channels):
            removed = required_channels.pop(idx)
            save_data()
            await query.answer(f"✅ Канал '{removed['title']}' удалён из списка.", show_alert=True)
        # Обновляем список
        data = "adm_channels"
        lines = []
        buttons = []
        for i, ch in enumerate(required_channels):
            lines.append(f"• {ch['title']} ({ch['link']})")
            buttons.append([
                InlineKeyboardButton(f"🗑 Удалить: {ch['title'][:20]}", callback_data=f"adm_ch_del_{i}"),
                InlineKeyboardButton(f"🚪 Выйти", callback_data=f"adm_ch_leave_{i}"),
            ])
        text = (
            "📢 *Обязательные каналы*\n"
            "─────────────────────\n\n"
            + ("\n".join(lines) if lines else "Каналов нет.\n")
            + "\n\nДля добавления нажмите кнопку ниже."
        )
        buttons.append([InlineKeyboardButton("➕ Добавить канал", callback_data="adm_ch_add")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("adm_ch_leave_"):
        idx = int(data.split("_")[3])
        if 0 <= idx < len(required_channels):
            ch = required_channels[idx]
            try:
                await context.bot.leave_chat(ch["id"])
                await query.answer(f"✅ Бот вышел из канала '{ch['title']}'.", show_alert=True)
                required_channels.pop(idx)
                save_data()
            except Exception as e:
                await query.answer(f"❌ Ошибка: {e}", show_alert=True)
        # Обновляем список
        lines = []
        buttons = []
        for i, ch in enumerate(required_channels):
            lines.append(f"• {ch['title']} ({ch['link']})")
            buttons.append([
                InlineKeyboardButton(f"🗑 Удалить: {ch['title'][:20]}", callback_data=f"adm_ch_del_{i}"),
                InlineKeyboardButton(f"🚪 Выйти", callback_data=f"adm_ch_leave_{i}"),
            ])
        text = (
            "📢 *Обязательные каналы*\n"
            "─────────────────────\n\n"
            + ("\n".join(lines) if lines else "Каналов нет.\n")
            + "\n\nДля добавления нажмите кнопку ниже."
        )
        buttons.append([InlineKeyboardButton("➕ Добавить канал", callback_data="adm_ch_add")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        return

    # --- Рассылка ---
    if data == "adm_broadcast":
        context.user_data["adm_action"] = "broadcast"
        await query.edit_message_text(
            "📣 *Рассылка*\n\n"
            f"Сообщение будет отправлено всем *{len(known_users)}* пользователям.\n\n"
            "Введите текст рассылки (поддерживается HTML):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        return

    # --- Выбор срока Premium: adm_dur_{days} ---
    if data.startswith("adm_dur_"):
        days = int(data.split("_")[2])
        target_id = context.user_data.get("adm_target_id")
        if not target_id:
            await query.edit_message_text("⚠️ Ошибка: ID не найден.", reply_markup=admin_keyboard())
            return
        grant_premium(target_id, days)
        exp_str = premium_expires_str(target_id)

        # Удаляем заявку если была
        req_id = context.user_data.pop("adm_req_id", None)
        was_request = False
        if req_id and req_id in purchase_requests:
            uid_of_req = purchase_requests[req_id].get("user_id")
            purchase_requests.pop(req_id)
            if uid_of_req:
                user_request_map.pop(uid_of_req, None)
            was_request = True
        elif target_id in user_request_map:
            rid = user_request_map.pop(target_id)
            purchase_requests.pop(rid, None)
            was_request = True
        save_data()

        await query.edit_message_text(
            f"✅ Premium выдан пользователю `{target_id}`\n"
            f"Действует: *{exp_str}*"
            + ("\n\n🛒 Заявка закрыта." if was_request else ""),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]),
        )
        # Уведомить пользователя
        try:
            await context.bot.send_message(
                target_id,
                f"🎉 Вам выдана *Premium-подписка*!\n"
                f"Действует: *{exp_str}*\n\n"
                f"Теперь доступны все функции бота:\n"
                f"• 🔍 Поиск 5-буквенных ников\n"
                f"• 🪤 Ловушка на ник\n"
                f"• ♾️ Безлимитные попытки",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        context.user_data.pop("adm_target_id", None)
        context.user_data.pop("adm_action", None)
        return


async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод ID от админа."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    action = context.user_data.get("adm_action")
    if not action:
        return

    text = update.message.text.strip()
    context.user_data.pop("adm_action", None)

    # --- Закрыть обращение по REQ-ID ---
    if action == "close_req":
        req_id = text.upper()
        if req_id not in purchase_requests:
            await update.message.reply_text(
                f"⚠️ Заявка `{req_id}` не найдена.\nПроверьте ID в разделе *Хотят купить*.",
                parse_mode="Markdown",
                reply_markup=admin_keyboard(),
            )
            return
        req = purchase_requests.pop(req_id)
        uid_of_req = req.get("user_id")
        if uid_of_req:
            user_request_map.pop(uid_of_req, None)
        save_data()
        await update.message.reply_text(
            f"✅ Заявка `{req_id}` закрыта.\n"
            f"👤 {req['display']} ({req['username']})",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        if uid_of_req:
            try:
                await context.bot.send_message(
                    uid_of_req,
                    "ℹ️ Ваша заявка на Premium была закрыта администратором.\n"
                    "Если у вас есть вопросы — обратитесь к администратору.",
                )
            except Exception:
                pass
        return

    # --- Добавить канал ---
    if action == "ch_add":
        username_raw = text.strip().lstrip("@")
        chat_id_input = f"@{username_raw}" if not username_raw.lstrip("-").isdigit() else int(username_raw)
        try:
            chat = await context.bot.get_chat(chat_id_input)
            ch_entry = {
                "id": chat.id,
                "title": chat.title or chat.username or str(chat.id),
                "link": f"https://t.me/{chat.username}" if chat.username else f"tg://openmessage?chat_id={chat.id}",
            }
            # Проверяем дубли
            if any(c["id"] == chat.id for c in required_channels):
                await update.message.reply_text(
                    "⚠️ Этот канал уже добавлен.",
                    reply_markup=admin_keyboard(),
                )
                return
            required_channels.append(ch_entry)
            save_data()
            await update.message.reply_text(
                f"✅ Канал *{ch_entry['title']}* добавлен!\n"
                f"Теперь пользователи должны подписаться на него.",
                parse_mode="Markdown",
                reply_markup=admin_keyboard(),
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Не удалось получить канал: {e}\n\n"
                "Убедитесь что бот является администратором канала.",
                reply_markup=admin_keyboard(),
            )
        return

    # --- Рассылка ---
    if action == "broadcast":
        broadcast_text = update.message.text  # сохраняем оригинал с форматированием
        status_msg = await update.message.reply_text(
            f"📣 Начинаю рассылку для {len(known_users)} пользователей..."
        )
        sent = 0
        failed = 0
        for uid in list(known_users.keys()):
            try:
                await context.bot.send_message(uid, broadcast_text, parse_mode="HTML")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # не флудим
        await status_msg.edit_text(
            f"📣 *Рассылка завершена*\n\n"
            f"✅ Отправлено: *{sent}*\n"
            f"❌ Не доставлено: *{failed}*",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        return

    # Для остальных действий парсим как int (user_id)
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ Некорректный ID. Введите число.")
        return

    if action == "give_prem":
        context.user_data["adm_target_id"] = target_id
        await update.message.reply_text(
            f"👑 Выдать Premium пользователю `{target_id}`\n\nВыберите срок:",
            parse_mode="Markdown",
            reply_markup=duration_keyboard("adm_dur"),
        )

    elif action == "revoke_prem":
        revoke_premium(target_id)
        await update.message.reply_text(
            f"❌ Premium снят с пользователя `{target_id}`",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        try:
            await context.bot.send_message(
                target_id,
                "ℹ️ Ваша Premium-подписка была отозвана администратором.",
            )
        except Exception:
            pass

    elif action == "ban":
        banned_users.add(target_id)
        save_data()
        await update.message.reply_text(
            f"🚫 Пользователь `{target_id}` заблокирован.",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        try:
            await context.bot.send_message(
                target_id,
                "⛔ Вы были заблокированы в этом боте.",
            )
        except Exception:
            pass

    elif action == "unban":
        banned_users.discard(target_id)
        save_data()
        await update.message.reply_text(
            f"✅ Пользователь `{target_id}` разблокирован.",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        try:
            await context.bot.send_message(
                target_id,
                "✅ Вы были разблокированы. Добро пожаловать обратно!",
            )
        except Exception:
            pass


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Публичная статистика бота."""
    user_id = update.effective_user.id
    if is_banned(user_id):
        return
    total_users = len(known_users)
    total_premium = len(premium_users)
    total_traps = sum(len(v) for v in traps.values())
    prem_status = "💎 Premium" if is_premium(user_id) else "👤 Обычный"
    remaining = get_remaining(user_id)
    await update.message.reply_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"💎 Premium подписчиков: <b>{total_premium}</b>\n"
        f"🪤 Активных ловушек: <b>{total_traps}</b>\n\n"
        f"─────────────────────\n"
        f"Ваш статус: <b>{prem_status}</b>\n"
        f"🎫 Попыток сегодня: <b>{remaining}</b> из {DAILY_LIMIT}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ---------------------------------------------------------------------------
# Фоновая задача: проверка ловушек каждые 2 минуты
# ---------------------------------------------------------------------------

async def check_traps(app: Application):
    while True:
        await asyncio.sleep(120)  # каждые 2 минуты
        if not traps:
            continue
        to_remove = []
        for username, user_ids in list(traps.items()):
            result = await is_username_free(app.bot, username)
            if result is True:
                for uid in user_ids:
                    try:
                        await app.bot.send_message(
                            uid,
                            f"🪤 <b>Ловушка сработала!</b>\n\n"
                            f"Ник <b>@{username}</b> теперь <b>свободен!</b>\n"
                            f"Скорее регистрируй: t.me/{username}",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось уведомить {uid}: {e}")
                to_remove.append(username)
                logger.info(f"Ловушка сработала: @{username}, уведомлено {len(user_ids)} чел.")
        if to_remove:
            for u in to_remove:
                del traps[u]
            save_data()  # сохраняем после удаления сработавших ловушек


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Вставь токен в переменную BOT_TOKEN")
        return

    # Загружаем сохранённые данные
    load_data()

    app = Application.builder().token(BOT_TOKEN).request(
        HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30)
    ).build()

    # Глобальный обработчик ошибок — подавляем мусорные ошибки
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        err_str = str(err)
        # Игнорируем "Message is not modified" — это не ошибка, просто дубль нажатия
        if "Message is not modified" in err_str:
            return
        # Игнорируем "Query is too old" — устаревшие callback'и
        if "Query is too old" in err_str or "query_id_invalid" in err_str.lower():
            return
        # Всё остальное логируем
        logger.warning(f"Ошибка при обработке обновления: {err_str}")

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    # Админские callback'и (adm_*) — отдельный хендлер с приоритетом
    app.add_handler(CallbackQueryHandler(admin_button_handler, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(button_handler))
    # Сообщения: сначала проверяем ввод от админа, потом обычный хендлер
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler), group=1)

    # Запускаем фоновую проверку ловушек
    async def post_init(application: Application):
        await get_telethon()  # запускаем Telethon при старте
        asyncio.create_task(check_traps(application))

    app.post_init = post_init

    print("🤖 Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
