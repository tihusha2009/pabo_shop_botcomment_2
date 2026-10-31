import os
import re
import random
import sqlite3
import logging
from telegram import Update, MessageOriginChannel
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB = sqlite3.connect("shop.db", timeout=30)
DB.execute("PRAGMA busy_timeout = 30000")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CANCEL_USERS = {
    "kari_escape",
    "tihomirkrd",
    "pabokim",
}

RANDOM_WORDS = {"рд", "рандом", "рандомный"}

LOTTERY_PATTERN = r"#(?:беспроигрышная|мини_лот|мини_бп|лот)_(\d+)"

# =====================================================
# DATABASE
# =====================================================
def init_db():
    with DB:
        DB.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                base_caption TEXT NOT NULL,
                PRIMARY KEY (channel_id, post_id)
            )
        """)
        DB.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                owner_user_id INTEGER,
                owner_label TEXT,
                PRIMARY KEY (channel_id, post_id, number)
            )
        """)
        DB.execute("""
            CREATE TABLE IF NOT EXISTS lots (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                lot_number INTEGER NOT NULL,
                ticket_count INTEGER NOT NULL,
                discussion_chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                list_message_id INTEGER,
                ticket_price INTEGER DEFAULT 0,
                lottery_hashtag TEXT DEFAULT ''
            )
        """)
        try:
            DB.execute("ALTER TABLE lots ADD COLUMN ticket_price INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            DB.execute("ALTER TABLE lots ADD COLUMN lottery_hashtag TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        DB.execute("""
            CREATE TABLE IF NOT EXISTS lot_tickets (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                owner_user_id INTEGER,
                owner_label TEXT,
                is_mini INTEGER DEFAULT 0,
                PRIMARY KEY (channel_id, post_id, number)
            )
        """)
        try:
            DB.execute("ALTER TABLE lot_tickets ADD COLUMN is_mini INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        DB.execute("""
            CREATE TABLE IF NOT EXISTS discussion_posts (
                discussion_chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                PRIMARY KEY (discussion_chat_id, thread_id)
            )
        """)
        
        DB.execute("""
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT
            )
        """)

def save_user(user):
    """Сохраняет информацию о пользователе в БД."""
    if not user:
        return
    username = user.username.lower() if user.username else None
    full_name = user.full_name
    with DB:
        DB.execute("""
            INSERT INTO known_users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, known_users.username),
                full_name = COALESCE(excluded.full_name, known_users.full_name)
        """, (user.id, username, full_name))

def find_user_by_username(username):
    """Ищет пользователя по username в БД."""
    if not username:
        return None
    username = username.lower().lstrip('@')
    row = DB.execute("""
        SELECT user_id, username, full_name FROM known_users
        WHERE LOWER(username) = ?
    """, (username,)).fetchone()
    return row

# =====================================================
# COMMON HELPERS
# =====================================================
def get_caption(message):
    return message.caption or message.text or ""

def user_label(user):
    if user.username:
        return "@" + user.username
    return user.full_name

def is_free_label(label):
    value = label.strip().lower()
    return (
        not value
        or value in {"-", "—"}
        or "свободно" in value
        or value == "free"
    )

def parse_ticket_price(text):
    if not text:
        return 0
    match = re.search(r"(\d+)\s*(?:₽|руб|р)\s*(?:за\s*)?(?:номерок|место|лот)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:цена|стоимость)[:\s]*(\d+)\s*(?:₽|руб|р)?", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*₽", text)
    if match:
        return int(match.group(1))
    return 0

# =====================================================
# NORMAL SHOP POSITIONS
# =====================================================
def extract_positions(text):
    lines = text.splitlines()
    positions = {}
    started = False
    previous_number = None

    for line in lines:
        if not started:
            if re.search(r"позиции\s*:", line, re.IGNORECASE):
                started = True
            continue

        match = re.match(r"^\s*(\d+)\s*[.)]\s*(.*?)\s*$", line)
        if match:
            number = int(match.group(1))
            label = match.group(2).strip()
            positions[number] = label
            previous_number = number
            continue

        if not line.strip():
            continue

        if previous_number is not None and line.strip().startswith("@"):
            positions[previous_number] = line.strip()
            previous_number = None
            continue

        if positions:
            break

    return positions

def get_or_create_shop(channel_id, post_id, text):
    positions = extract_positions(text)
    if not positions:
        return

    exists = DB.execute("""
        SELECT 1 FROM shops WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()

    with DB:
        if not exists:
            DB.execute("""
                INSERT INTO shops (channel_id, post_id, base_caption)
                VALUES (?, ?, ?)
            """, (channel_id, post_id, text))

        for number, label in positions.items():
            if is_free_label(label):
                owner_id = None
                owner_name = None
            else:
                owner_id = -1
                owner_name = label

            DB.execute("""
                INSERT OR IGNORE INTO slots (
                    channel_id, post_id, number, owner_user_id, owner_label
                ) VALUES (?, ?, ?, ?, ?)
            """, (channel_id, post_id, number, owner_id, owner_name))

def render_shop_post(channel_id, post_id):
    row = DB.execute("""
        SELECT base_caption FROM shops WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    if not row:
        return None

    original = row[0]
    lines = original.splitlines()
    slots = DB.execute("""
        SELECT number, owner_label FROM slots
        WHERE channel_id = ? AND post_id = ? ORDER BY number
    """, (channel_id, post_id)).fetchall()
    slot_map = dict(slots)

    output = []
    started = False
    skip_username = False

    for line in lines:
        if skip_username:
            if line.strip().startswith("@"):
                skip_username = False
                continue
            skip_username = False

        if not started:
            output.append(line)
            if re.search(r"позиции\s*:", line, re.IGNORECASE):
                started = True
            continue

        match = re.match(r"^(\s*)(\d+)(\s*[.)]\s*)(.*)$", line)
        if not match:
            output.append(line)
            continue

        number = int(match.group(2))
        if number not in slot_map:
            output.append(line)
            continue

        prefix = match.group(1) + match.group(2) + match.group(3)
        original_label = match.group(4).strip()
        owner = slot_map[number] or "Свободно"
        output.append(prefix + owner)

        if not original_label and number in slot_map:
            skip_username = True

    return "\n".join(output)

async def update_shop_post(context, channel_id, post_id):
    text = render_shop_post(channel_id, post_id)
    if text is None:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=channel_id, message_id=post_id, text=text,
        )
        return
    except TelegramError:
        pass

    try:
        await context.bot.edit_message_caption(
            chat_id=channel_id, message_id=post_id, caption=text,
        )
    except TelegramError:
        logger.exception("Не удалось отредактировать пост %s", post_id)

def get_discussion_mapping(message):
    thread_id = message.message_thread_id
    if thread_id:
        return DB.execute("""
            SELECT channel_id, post_id FROM discussion_posts
            WHERE discussion_chat_id = ? AND thread_id = ?
        """, (message.chat.id, thread_id)).fetchone()

    root = message.reply_to_message
    if root and root.is_automatic_forward:
        origin = root.forward_origin
        if isinstance(origin, MessageOriginChannel):
            return origin.chat.id, origin.message_id

    return None

async def handle_shop_comment(update, context):
    message = update.effective_message
    if not message or not message.text or not message.from_user:
        return

    save_user(message.from_user)

    mapping = get_discussion_mapping(message)
    if not mapping:
        return

    channel_id, post_id = mapping

    shop = DB.execute("""
        SELECT 1 FROM shops WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()

    if not shop:
        root = message.reply_to_message
        if root and root.is_automatic_forward:
            get_or_create_shop(channel_id, post_id, get_caption(root))

    shop = DB.execute("""
        SELECT 1 FROM shops WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    if not shop:
        return

    text = message.text.strip()
    if not re.fullmatch(r"\d+(?:\s*(?:,|;|\s+и\s+|\s+)\s*\d+)*", text, re.IGNORECASE):
        return

    requested = list(dict.fromkeys(int(v) for v in re.findall(r"\d+", text)))
    if not requested:
        return

    rows = DB.execute("""
        SELECT number, owner_user_id, owner_label FROM slots
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchall()
    slot_map = {n: (oid, lbl) for n, oid, lbl in rows}

    missing = [n for n in requested if n not in slot_map]
    if missing:
        await message.reply_text("❌ Не найдены позиции: " + ", ".join(map(str, missing)))
        return

    user = message.from_user
    label = user_label(user)

    occupied = [n for n in requested if slot_map[n][0] is not None and slot_map[n][0] != user.id]
    if occupied:
        await message.reply_text("❌ Уже заняты позиции: " + ", ".join(map(str, occupied)))
        return

    assigned = []
    with DB:
        for number in requested:
            cursor = DB.execute("""
                UPDATE slots SET owner_user_id = ?, owner_label = ?
                WHERE channel_id = ? AND post_id = ? AND number = ?
                  AND (owner_user_id IS NULL OR owner_user_id = ?)
            """, (user.id, label, channel_id, post_id, number, user.id))
            if cursor.rowcount == 1:
                assigned.append(number)

    if assigned:
        await update_shop_post(context, channel_id, post_id)
        await message.reply_text(f"✅ {label}, вы записались на позиции: " + ", ".join(map(str, assigned)))

# =====================================================
# LOTTERIES
# =====================================================
def find_lottery(text):
    lot_match = re.search(LOTTERY_PATTERN, text, re.IGNORECASE)
    count_match = re.search(r"Всего\s*(\d+)\s*номерков", text, re.IGNORECASE)
    if not lot_match or not count_match:
        return None
    lot_number = int(lot_match.group(1))
    count = int(count_match.group(1))
    if count < 1:
        return None
    return lot_number, count

def lottery_list(channel_id, post_id, lot_number):
    rows = DB.execute("""
        SELECT number, owner_label, is_mini
        FROM lot_tickets
        WHERE channel_id = ? AND post_id = ?
        ORDER BY number
    """, (channel_id, post_id)).fetchall()
    lines = [f"🎟️ Номерки лотереи #{lot_number}", ""]
    for number, owner, is_mini in rows:
        if owner:
            if is_mini:
                lines.append(f"{number}. {owner} (мини)")
            else:
                lines.append(f"{number}. {owner}")
        else:
            lines.append(f"{number}. Свободно")
    return "\n".join(lines)

async def create_lottery(message, origin):
    text = get_caption(message)
    parsed = find_lottery(text)
    if not parsed:
        return

    lot_number, count = parsed
    channel_id = origin.chat.id
    post_id = origin.message_id

    exists = DB.execute("""
        SELECT 1 FROM lots WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    if exists:
        return

    ticket_price = parse_ticket_price(text)
    
    hashtag_match = re.search(LOTTERY_PATTERN, text, re.IGNORECASE)
    lottery_hashtag = hashtag_match.group(0) if hashtag_match else f"#лот_{lot_number}"

    with DB:
        DB.execute("""
            INSERT INTO lots (
                channel_id, post_id, lot_number, ticket_count,
                discussion_chat_id, thread_id, list_message_id, ticket_price, lottery_hashtag
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """, (
            channel_id, post_id, lot_number, count,
            message.chat.id, message.message_id, ticket_price, lottery_hashtag,
        ))
        DB.executemany("""
            INSERT INTO lot_tickets (
                channel_id, post_id, number, owner_user_id, owner_label, is_mini
            ) VALUES (?, ?, ?, NULL, NULL, 0)
        """, [(channel_id, post_id, n) for n in range(1, count + 1)])

    logger.info("Лотерея #%s создана: номерков=%s, цена=%s, хэштег=%s", lot_number, count, ticket_price, lottery_hashtag)

    try:
        sent = await message.reply_text(lottery_list(channel_id, post_id, lot_number))
    except TelegramError:
        logger.exception("Не удалось создать список лотереи")
        return

    with DB:
        DB.execute("""
            UPDATE lots SET list_message_id = ?
            WHERE channel_id = ? AND post_id = ?
        """, (sent.message_id, channel_id, post_id))

async def handle_root_post(update, context):
    message = update.effective_message
    if not message or not message.is_automatic_forward:
        return

    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return

    channel_id = origin.chat.id
    post_id = origin.message_id

    with DB:
        DB.execute("""
            INSERT OR IGNORE INTO discussion_posts (
                discussion_chat_id, thread_id, channel_id, post_id
            ) VALUES (?, ?, ?, ?)
        """, (message.chat.id, message.message_id, channel_id, post_id))

    get_or_create_shop(channel_id, post_id, get_caption(message))
    await create_lottery(message, origin)

def find_lottery_for_message(message):
    thread_id = message.message_thread_id
    if thread_id:
        result = DB.execute("""
            SELECT channel_id, post_id, lot_number, ticket_count, list_message_id, ticket_price, lottery_hashtag
            FROM lots WHERE discussion_chat_id = ? AND thread_id = ?
        """, (message.chat.id, thread_id)).fetchone()
        if result:
            return result

    reply = message.reply_to_message
    if reply:
        if reply.is_automatic_forward:
            origin = reply.forward_origin
            if isinstance(origin, MessageOriginChannel):
                return DB.execute("""
                    SELECT channel_id, post_id, lot_number, ticket_count, list_message_id, ticket_price, lottery_hashtag
                    FROM lots WHERE channel_id = ? AND post_id = ?
                """, (origin.chat.id, origin.message_id)).fetchone()
        return DB.execute("""
            SELECT channel_id, post_id, lot_number, ticket_count, list_message_id, ticket_price, lottery_hashtag
            FROM lots WHERE discussion_chat_id = ? AND list_message_id = ?
        """, (message.chat.id, reply.message_id)).fetchone()
    return None

def parse_lottery_request(text):
    lowered = text.strip().lower()
    if "отмена" in lowered:
        return [], 0

    words = re.findall(r"[a-zа-яё]+", lowered)
    random_count = sum(word in RANDOM_WORDS for word in words)

    leading = re.fullmatch(r"\s*(\d+)\s+(?:(?:рд|рандом|рандомный)\s*)+", lowered)
    if leading:
        return [], min(int(leading.group(1)), 1000)

    numbers = list(dict.fromkeys(int(v) for v in re.findall(r"\d+", lowered)))
    return numbers, random_count

async def refresh_lottery_list(context, chat_id, list_message_id, channel_id, post_id, lot_number):
    if not list_message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=list_message_id,
            text=lottery_list(channel_id, post_id, lot_number),
        )
    except TelegramError:
        logger.exception("Не удалось обновить список лотереи")

async def handle_lottery_comment(update, context):
    message = update.effective_message
    if not message or not message.text or not message.from_user:
        return False

    save_user(message.from_user)

    lot = find_lottery_for_message(message)
    if not lot:
        return False

    channel_id, post_id, lot_number, count, list_message_id, ticket_price, lottery_hashtag = lot
    text = message.text.strip()
    lowered = text.lower()
    user = message.from_user
    username = (user.username or "").lower()

    if "отмена" in lowered:
        if username not in CANCEL_USERS:
            await message.reply_text("❌ Отменять номерки могут только @Kari_escape, @tihomirkrd и @PaboKim.")
            return True

        numbers = list(dict.fromkeys(int(v) for v in re.findall(r"\d+", text)))
        if not numbers:
            await message.reply_text("Напиши, например: «номерки 3, 5 отмена».")
            return True

        invalid = [n for n in numbers if n < 1 or n > count]
        if invalid:
            await message.reply_text(f"❌ Номерки должны быть от 1 до {count}. Неверные номера: " + ", ".join(map(str, invalid)))
            return True

        with DB:
            DB.executemany("""
                UPDATE lot_tickets SET owner_user_id = NULL, owner_label = NULL, is_mini = 0
                WHERE channel_id = ? AND post_id = ? AND number = ?
            """, [(channel_id, post_id, n) for n in numbers])

        await message.reply_text("✅ Освобождены номерки: " + ", ".join(map(str, numbers)))
        await refresh_lottery_list(context, message.chat.id, list_message_id, channel_id, post_id, lot_number)
        return True

    explicit_numbers, random_count = parse_lottery_request(text)
    if not explicit_numbers and random_count == 0:
        return True

    invalid = [n for n in explicit_numbers if n < 1 or n > count]
    if invalid:
        await message.reply_text(f"❌ Номерки должны быть от 1 до {count}. Неверные номера: " + ", ".join(map(str, invalid)))
        return True

    label = user_label(user)
    assigned = []
    unavailable = []

    with DB:
        for number in explicit_numbers:
            cursor = DB.execute("""
                UPDATE lot_tickets SET owner_user_id = ?, owner_label = ?, is_mini = 0
                WHERE channel_id = ? AND post_id = ? AND number = ? AND owner_user_id IS NULL
            """, (user.id, label, channel_id, post_id, number))
            if cursor.rowcount == 1:
                assigned.append(number)
            else:
                unavailable.append(number)

        for _ in range(random_count):
            free = DB.execute("""
                SELECT number FROM lot_tickets
                WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NULL
            """, (channel_id, post_id)).fetchall()
            if not free:
                break

            number = random.choice([r[0] for r in free])
            cursor = DB.execute("""
                UPDATE lot_tickets SET owner_user_id = ?, owner_label = ?, is_mini = 0
                WHERE channel_id = ? AND post_id = ? AND number = ? AND owner_user_id IS NULL
            """, (user.id, label, channel_id, post_id, number))
            if cursor.rowcount == 1:
                assigned.append(number)

    responses = []
    if assigned:
        responses.append(f"✅ {label}, твои номерки: " + ", ".join(map(str, assigned)))
    if unavailable:
        responses.append("⚠️ Уже заняты: " + ", ".join(map(str, unavailable)))

    random_assigned = sum(1 for n in assigned if n not in explicit_numbers)
    if random_count > random_assigned:
        responses.append("⚠️ Не хватило свободных номерков для всех случайных заявок.")

    if not responses:
        responses.append("❌ Свободных номерков больше нет.")

    await message.reply_text("\n".join(responses))
    await refresh_lottery_list(context, message.chat.id, list_message_id, channel_id, post_id, lot_number)
    return True

# =====================================================
# ADMIN COMMANDS: ADD TICKETS
# =====================================================
async def add_ticket_cmd(update, context):
    """Команда /add для админов: вписать номерки за пользователя."""
    message = update.effective_message
    if not message:
        return

    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return

    args = context.args
    if len(args) < 2:
        await message.reply_text(
            "❌ Использование:\n"
            "/add <номерки> @username [мини]\n"
            "Примеры:\n"
            "/add 5 @user\n"
            "/add рд @user мини\n"
            "/add 1, 3 @user мини"
        )
        return

    target_user_str = args[-1]
    is_mini = False
    
    if target_user_str.lower() == "мини":
        if len(args) < 3:
            await message.reply_text("❌ Не указан пользователь")
            return
        is_mini = True
        target_user_str = args[-2]
        numbers_str = " ".join(args[:-2])
    else:
        numbers_str = " ".join(args[:-1])

    if not target_user_str.startswith("@"):
        await message.reply_text("❌ Укажите пользователя через @username")
        return
    
    user_info = find_user_by_username(target_user_str)
    if not user_info:
        await message.reply_text(
            f"❌ Пользователь {target_user_str} не найден.\n"
            "Он должен хотя бы раз написать в чат, чтобы бот его запомнил."
        )
        return
    
    target_user_id = user_info[0]
    target_username = user_info[1]
    target_full_name = user_info[2]
    target_label = f"@{target_username}" if target_username else target_full_name

    lot = find_lottery_for_message(message)
    if not lot:
        await message.reply_text("❌ Не найдена активная лотерея в этом треде")
        return
    
    channel_id, post_id, lot_number, lot_count, _, _, _ = lot

    if "рд" in numbers_str.lower() or "рандом" in numbers_str.lower():
        match = re.search(r"(\d+)", numbers_str)
        if not match:
            await message.reply_text("❌ Укажите количество рандомных номерков, например: /add 3 рд @user")
            return
        count = int(match.group(1))
        
        assigned = []
        for _ in range(count):
            free = DB.execute("""
                SELECT number FROM lot_tickets
                WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NULL
            """, (channel_id, post_id)).fetchall()
            if not free:
                break
            number = random.choice([r[0] for r in free])
            DB.execute("""
                UPDATE lot_tickets 
                SET owner_user_id = ?, owner_label = ?, is_mini = ?
                WHERE channel_id = ? AND post_id = ? AND number = ?
            """, (target_user_id, target_label, 1 if is_mini else 0, channel_id, post_id, number))
            assigned.append(number)
        
        if assigned:
            mini_text = " (мини)" if is_mini else ""
            await message.reply_text(f"✅ Выданы рандомные номерки{mini_text}: {', '.join(map(str, assigned))} для {target_label}")
            await refresh_lottery_list(context, message.chat.id, lot[4], channel_id, post_id, lot_number)
        else:
            await message.reply_text("❌ Нет свободных номерков")

    else:
        numbers = list(dict.fromkeys(int(v) for v in re.findall(r"\d+", numbers_str)))
        if not numbers:
            await message.reply_text("❌ Не указаны номерки")
            return

        invalid = [n for n in numbers if n < 1 or n > lot_count]
        if invalid:
            await message.reply_text(f"❌ Номерки должны быть от 1 до {lot_count}. Неверные: {invalid}")
            return

        assigned = []
        unavailable = []
        with DB:
            for number in numbers:
                cursor = DB.execute("""
                    UPDATE lot_tickets 
                    SET owner_user_id = ?, owner_label = ?, is_mini = ?
                    WHERE channel_id = ? AND post_id = ? AND number = ? AND owner_user_id IS NULL
                """, (target_user_id, target_label, 1 if is_mini else 0, channel_id, post_id, number))
                if cursor.rowcount == 1:
                    assigned.append(number)
                else:
                    unavailable.append(number)
        
        resp = []
        if assigned:
            mini_text = " (мини)" if is_mini else ""
            resp.append(f"✅ Выданы номерки{mini_text}: {', '.join(map(str, assigned))} для {target_label}")
        if unavailable:
            resp.append(f"⚠️ Заняты: {', '.join(map(str, unavailable))}")
        
        if resp:
            await message.reply_text("\n".join(resp))
            await refresh_lottery_list(context, message.chat.id, lot[4], channel_id, post_id, lot_number)

# =====================================================
# PAYMENT & RESULTS
# =====================================================
async def oplata_cmd(update, context):
    """Команда /oplata — считает оплату (только для админов)."""
    message = update.effective_message
    if not message:
        return

    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return

    thread_id = message.message_thread_id
    if not thread_id:
        await message.reply_text("❌ Команда работает только в комментариях к посту лотереи")
        return

    lot = DB.execute("""
        SELECT channel_id, post_id, lot_number, ticket_price, lottery_hashtag
        FROM lots WHERE discussion_chat_id = ? AND thread_id = ?
    """, (message.chat.id, thread_id)).fetchone()

    if not lot:
        await message.reply_text("❌ Лотерея не найдена")
        return

    channel_id, post_id, lot_number, ticket_price, lottery_hashtag = lot

    if ticket_price <= 0:
        await message.reply_text("❌ Цена номерка не указана в посте лотереи.")
        return

    # Определяем тип лотереи
    is_mini_lottery = "мини" in lottery_hashtag.lower()
    
    # Для мини-лотерей учитываем ВСЕ номерки, для обычных — только без "мини"
    if is_mini_lottery:
        tickets = DB.execute("""
            SELECT owner_user_id, owner_label, number FROM lot_tickets
            WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NOT NULL
            ORDER BY number
        """, (channel_id, post_id)).fetchall()
    else:
        tickets = DB.execute("""
            SELECT owner_user_id, owner_label, number FROM lot_tickets
            WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NOT NULL AND is_mini = 0
            ORDER BY number
        """, (channel_id, post_id)).fetchall()

    if not tickets:
        await message.reply_text("❌ Пока нет записавшихся участников")
        return

    user_data = {}
    for user_id, label, number in tickets:
        if user_id not in user_data:
            user_data[user_id] = {'label': label, 'numbers': []}
        user_data[user_id]['numbers'].append(number)

    lines = [f"💰 Оплата за лотерею #{lot_number}:", ""]
    total_sum = 0

    for user_id, data in user_data.items():
        label = data['label']
        count = len(data['numbers'])
        amount = ticket_price * count
        total_sum += amount
        nums_str = ", ".join(map(str, data['numbers']))
        lines.append(f"{label} — {ticket_price}₽ × {count} = {amount}₽ ({nums_str})")

    lines.append("")
    lines.append(f"<b>Итого: {total_sum}₽</b>")

    await message.reply_text("\n".join(lines), parse_mode="HTML")

async def oplata_post_cmd(update, context, bank_info):
    """Генерирует пост оплаты для канала."""
    message = update.effective_message
    if not message:
        return

    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return

    thread_id = message.message_thread_id
    if not thread_id:
        await message.reply_text("❌ Команда работает только в комментариях к посту лотереи")
        return

    lot = DB.execute("""
        SELECT channel_id, post_id, lot_number, ticket_price, lottery_hashtag
        FROM lots WHERE discussion_chat_id = ? AND thread_id = ?
    """, (message.chat.id, thread_id)).fetchone()

    if not lot:
        await message.reply_text("❌ Лотерея не найдена")
        return

    channel_id, post_id, lot_number, ticket_price, lottery_hashtag = lot

    if ticket_price <= 0:
        await message.reply_text("❌ Цена номерка не указана.")
        return

    # Определяем тип лотереи
    is_mini_lottery = "мини" in lottery_hashtag.lower()
    
    # Для мини-лотерей учитываем ВСЕ номерки, для обычных — только без "мини"
    if is_mini_lottery:
        tickets = DB.execute("""
            SELECT owner_user_id, owner_label, number FROM lot_tickets
            WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NOT NULL
            ORDER BY number
        """, (channel_id, post_id)).fetchall()
    else:
        tickets = DB.execute("""
            SELECT owner_user_id, owner_label, number FROM lot_tickets
            WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NOT NULL AND is_mini = 0
            ORDER BY number
        """, (channel_id, post_id)).fetchall()

    if not tickets:
        await message.reply_text("❌ Нет участников для формирования списка оплаты.")
        return

    user_data = {}
    for user_id, label, number in tickets:
        if user_id not in user_data:
            user_data[user_id] = {'label': label, 'numbers': []}
        user_data[user_id]['numbers'].append(number)

    participants_list = []
    idx = 1
    for user_id, data in user_data.items():
        label = data['label']
        count = len(data['numbers'])
        amount = ticket_price * count
        nums_str = ", ".join(map(str, data['numbers']))
        safe_label = label.replace("<", "&lt;").replace(">", "&gt;")
        line = f"{idx}. {safe_label} — {ticket_price}₽ × {count} = {amount}₽ ({nums_str})"
        participants_list.append(line)
        idx += 1

    header = (
        "ღ꧁ღ╭⊱ꕥꕥ⊱╮ღ꧂ღ\n"
        "              ОПЛАТА\n"
        "»»————— ★ —————««\n\n"
        "Перед оплатой не забудьте проверить номерки.\n"
        " ★ - оплачено\n\n"
        f"{bank_info}\n\n"
    )
    
    footer = (
        "\n‣ скрин о переводе в комментарии с указанием номера и юза\n"
        "‣ дедлайн: 12 часов\n\n"
        f"{lottery_hashtag} #straykids #оплата_лот"
    )

    full_text = header + "\n".join(participants_list) + footer

    try:
        await context.bot.send_message(chat_id=channel_id, text=full_text, parse_mode="HTML")
        await message.reply_text("✅ Пост оплаты опубликован в канале")
    except TelegramError as e:
        logger.exception("Ошибка публикации поста оплаты: %s", e)
        await message.reply_text("❌ Не удалось опубликовать пост")

async def oplata_kar_cmd(update, context):
    await oplata_post_cmd(update, context, "89087193297 Елена Д. (Озон банк)")

async def oplata_tih_cmd(update, context):
    await oplata_post_cmd(update, context, "89673136733 Тихомир У. (Озон банк)")

async def result_cmd(update, context):
    """Команда /result — выбирает победителя и публикует пост в канал."""
    message = update.effective_message
    if not message:
        return

    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return

    thread_id = message.message_thread_id
    if not thread_id:
        await message.reply_text("❌ Команда работает только в комментариях к посту лотереи")
        return

    lot = DB.execute("""
        SELECT channel_id, post_id, lot_number, lottery_hashtag
        FROM lots WHERE discussion_chat_id = ? AND thread_id = ?
    """, (message.chat.id, thread_id)).fetchone()

    if not lot:
        await message.reply_text("❌ Лотерея не найдена")
        return

    channel_id, post_id, lot_number, lottery_hashtag = lot

    tickets = DB.execute("""
        SELECT number, owner_label FROM lot_tickets
        WHERE channel_id = ? AND post_id = ? AND owner_user_id IS NOT NULL
    """, (channel_id, post_id)).fetchall()

    if not tickets:
        await message.reply_text("❌ Нет участников для розыгрыша")
        return

    winning_ticket = random.choice(tickets)
    winning_number = winning_ticket[0]
    winner_label = winning_ticket[1]

    result_text = (
        f"<b>РЕЗУЛЬТАТЫ.</b>\n\n"
        f"Победил номерок: <b>{winning_number}</b> — {winner_label}. "
        f"Поздравляем!\n\n"
        f"{lottery_hashtag}"
    )

    try:
        await context.bot.send_message(chat_id=channel_id, text=result_text, parse_mode="HTML")
        logger.info("Лотерея #%s: победитель %s (номерок %s)", lot_number, winner_label, winning_number)
        await message.reply_text("✅ Результаты опубликованы в канале")
    except TelegramError as e:
        logger.exception("Не удалось опубликовать результаты: %s", e)
        await message.reply_text("❌ Не удалось опубликовать результаты в канал")

# =====================================================
# ALL COMMENTS
# =====================================================
async def handle_all_comments(update, context):
    handled = await handle_lottery_comment(update, context)
    if handled:
        return
    await handle_shop_comment(update, context)

# =====================================================
# EDITED CHANNEL POSTS
# =====================================================
async def handle_edited_channel_post(update, context):
    message = update.edited_channel_post
    if not message:
        return

    channel_id = message.chat.id
    post_id = message.message_id
    text = get_caption(message)

    positions = extract_positions(text)
    if not positions:
        return

    with DB:
        DB.execute("""
            INSERT INTO shops (channel_id, post_id, base_caption)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id, post_id)
            DO UPDATE SET base_caption = excluded.base_caption
        """, (channel_id, post_id, text))

        for number, label in positions.items():
            if is_free_label(label):
                DB.execute("""
                    INSERT OR IGNORE INTO slots (
                        channel_id, post_id, number, owner_user_id, owner_label
                    ) VALUES (?, ?, ?, NULL, NULL)
                """, (channel_id, post_id, number))
            else:
                DB.execute("""
                    INSERT INTO slots (
                        channel_id, post_id, number, owner_user_id, owner_label
                    ) VALUES (?, ?, ?, -1, ?)
                    ON CONFLICT(channel_id, post_id, number)
                    DO UPDATE SET
                        owner_user_id = CASE
                            WHEN slots.owner_user_id IS NULL OR slots.owner_user_id = -1
                            THEN -1 ELSE slots.owner_user_id END,
                        owner_label = CASE
                            WHEN slots.owner_user_id IS NULL OR slots.owner_user_id = -1
                            THEN excluded.owner_label ELSE slots.owner_label END
                """, (channel_id, post_id, number, label))

# =====================================================
# COMMANDS
# =====================================================
async def start(update, context):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Привет! Я бот магазина PABO SHOP 🎟️\n\n"
            "Разбор: напиши номер позиции, например 6 или 6, 7.\n"
            "Лотерея: напиши любой номер или рд, например 1 или 3 рд.\n"
            "Случайные номерки: «3 рд» выдаёт три случайных номерка.\n"
            "Отмена номерков доступна только админам шопа.\n\n"
            "Команды для админов:\n"
            "/add <номерки> @user [мини] — вписать номерки\n"
            "/oplata — посчитать оплату (в чат)\n"
            "/oplata_kar — пост оплаты для Кари\n"
            "/oplata_tih — пост оплаты для Тихомира\n"
            "/result — выбрать победителя"
        )

async def ping(update, context):
    if update.effective_message:
        await update.effective_message.reply_text("Бот работает ✅")

# =====================================================
# START BOT
# =====================================================
def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN. Установи токен в переменную окружения.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    
    app.add_handler(CommandHandler("add", add_ticket_cmd))
    app.add_handler(CommandHandler("oplata", oplata_cmd))
    app.add_handler(CommandHandler("oplata_kar", oplata_kar_cmd))
    app.add_handler(CommandHandler("oplata_tih", oplata_tih_cmd))
    app.add_handler(CommandHandler("result", result_cmd))

    app.add_handler(MessageHandler(filters.ALL, handle_root_post), group=-1)
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_edited_channel_post),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_comments),
        group=0,
    )

    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
