import os
import re
import random
import sqlite3
import logging
from datetime import datetime, timedelta
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
AUCTION_PATTERN = r"#аукцион_(\d+)"

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
                PRIMARY KEY (channel_id, post_id)
            )
        """)
        DB.execute("""
            CREATE TABLE IF NOT EXISTS lot_tickets (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                owner_user_id INTEGER,
                owner_label TEXT,
                PRIMARY KEY (channel_id, post_id, number)
            )
        """)
        DB.execute("""
            CREATE TABLE IF NOT EXISTS discussion_posts (
                discussion_chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                PRIMARY KEY (discussion_chat_id, thread_id)
            )
        """)
        # Новая таблица для аукционов
        DB.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                auction_number INTEGER NOT NULL,
                base_caption TEXT NOT NULL,
                start_price INTEGER NOT NULL,
                min_step INTEGER NOT NULL,
                duration_minutes INTEGER NOT NULL,
                end_time TEXT,
                current_bid INTEGER,
                current_bidder_id INTEGER,
                current_bidder_label TEXT,
                is_active INTEGER DEFAULT 1,
                PRIMARY KEY (channel_id, post_id)
            )
        """)

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

# =====================================================
# AUCTION FUNCTIONS
# =====================================================
def find_auction(text):
    """Парсит данные аукциона из текста поста"""
    auction_match = re.search(AUCTION_PATTERN, text, re.IGNORECASE)
    if not auction_match:
        return None
    
    auction_number = int(auction_match.group(1))
    
    # Извлекаем начальную цену
    start_price_match = re.search(
        r"Начальная\s*(?:цена|ставка)[:\s]*(\d+)\s*₽?",
        text,
        re.IGNORECASE
    )
    
    # Извлекаем минимальный шаг
    min_step_match = re.search(
        r"Минимальный\s*(?:шаг|сумма)[:\s]*(\d+)\s*₽?",
        text,
        re.IGNORECASE
    )
    
    # Извлекаем продолжительность
    duration_match = re.search(
        r"Продолжительность[:\s]*(\d+)\s*минут?",
        text,
        re.IGNORECASE
    )
    
    if not all([start_price_match, min_step_match, duration_match]):
        return None
    
    start_price = int(start_price_match.group(1))
    min_step = int(min_step_match.group(1))
    duration_minutes = int(duration_match.group(1))
    
    return {
        'auction_number': auction_number,
        'start_price': start_price,
        'min_step': min_step,
        'duration_minutes': duration_minutes
    }

def render_auction_post(channel_id, post_id):
    """Формирует текст поста с информацией о текущей ставке"""
    row = DB.execute("""
        SELECT base_caption, current_bid, current_bidder_label, is_active, end_time
        FROM auctions
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not row:
        return None
    
    base_caption, current_bid, current_bidder_label, is_active, end_time = row
    
    # Удаляем старые блоки ставок и победителя
    lines = base_caption.split('\n')
    filtered_lines = []
    skip_next = False
    
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        
        # Пропускаем старые блоки
        if re.match(r"^(Последняя ставка|Победитель)[:\s]", line):
            skip_next = True
            continue
        
        filtered_lines.append(line)
    
    base_text = '\n'.join(filtered_lines).rstrip()
    
    # Добавляем актуальный блок
    if is_active:
        if current_bid and current_bidder_label:
            bid_info = f"\n\nПоследняя ставка:\n{current_bidder_label} — {current_bid}₽"
        else:
            bid_info = f"\n\nПоследняя ставка:\nПока нет ставок"
    else:
        if current_bid and current_bidder_label:
            bid_info = f"\n\nПобедитель:\n{current_bidder_label} — {current_bid}₽"
        else:
            bid_info = "\n\nАукцион завершен без ставок"
    
    return base_text + bid_info

async def update_auction_post(context, channel_id, post_id):
    """Обновляет пост аукциона"""
    text = render_auction_post(channel_id, post_id)
    if text is None:
        return
    
    try:
        await context.bot.edit_message_text(
            chat_id=channel_id,
            message_id=post_id,
            text=text,
        )
        return
    except TelegramError:
        pass
    
    try:
        await context.bot.edit_message_caption(
            chat_id=channel_id,
            message_id=post_id,
            caption=text,
        )
    except TelegramError:
        logger.exception(
            "Не удалось обновить пост аукциона %s в канале %s",
            post_id,
            channel_id,
        )

async def create_auction(message, origin):
    """Создает новый аукцион"""
    parsed = find_auction(get_caption(message))
    if not parsed:
        return
    
    channel_id = origin.chat.id
    post_id = origin.message_id
    
    exists = DB.execute("""
        SELECT 1 FROM auctions
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if exists:
        return
    
    end_time = datetime.now() + timedelta(minutes=parsed['duration_minutes'])
    
    with DB:
        DB.execute("""
            INSERT INTO auctions (
                channel_id, post_id, auction_number, base_caption,
                start_price, min_step, duration_minutes, end_time,
                current_bid, current_bidder_id, current_bidder_label,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1)
        """, (
            channel_id,
            post_id,
            parsed['auction_number'],
            get_caption(message),
            parsed['start_price'],
            parsed['min_step'],
            parsed['duration_minutes'],
            end_time.isoformat(),
        ))
    
    await update_auction_post(context, message.chat.id, post_id)

def find_auction_for_message(message):
    """Находит аукцион для комментария"""
    thread_id = message.message_thread_id
    if thread_id:
        result = DB.execute("""
            SELECT channel_id, post_id, auction_number, start_price,
                   min_step, duration_minutes, end_time, current_bid,
                   current_bidder_id, current_bidder_label, is_active
            FROM auctions
            WHERE discussion_chat_id = ? AND thread_id = ?
        """, (message.chat.id, thread_id)).fetchone()
        if result:
            return result
    
    reply = message.reply_to_message
    if reply and reply.is_automatic_forward:
        origin = reply.forward_origin
        if isinstance(origin, MessageOriginChannel):
            return DB.execute("""
                SELECT channel_id, post_id, auction_number, start_price,
                       min_step, duration_minutes, end_time, current_bid,
                       current_bidder_id, current_bidder_label, is_active
                FROM auctions
                WHERE channel_id = ? AND post_id = ?
            """, (origin.chat.id, origin.message_id)).fetchone()
    
    return None

def parse_bid(text):
    """Парсит ставку из комментария"""
    match = re.search(r"(\d+)\s*₽?", text)
    if match:
        return int(match.group(1))
    return None

async def handle_auction_comment(update, context):
    """Обрабатывает ставки в комментариях"""
    message = update.effective_message
    if not message or not message.text or not message.from_user:
        return False
    
    auction = find_auction_for_message(message)
    if not auction:
        return False
    
    (channel_id, post_id, auction_number, start_price, min_step,
     duration_minutes, end_time_str, current_bid, current_bidder_id,
     current_bidder_label, is_active) = auction
    
    user = message.from_user
    label = user_label(user)
    text = message.text.strip()
    
    bid = parse_bid(text)
    if bid is None:
        return False
    
    # Проверяем, активен ли аукцион
    if not is_active:
        await message.reply_text("❌ Аукцион уже завершен")
        return True
    
    # Проверяем, не истекло ли время
    if end_time_str:
        end_time = datetime.fromisoformat(end_time_str)
        if datetime.now() > end_time:
            await message.reply_text("❌ Время аукциона вышло")
            return True
    
    # Проверяем минимальную ставку
    if current_bid is None:
        min_required = start_price
    else:
        min_required = current_bid + min_step
    
    if bid < min_required:
        await message.reply_text(
            f"❌ Минимальная ставка: {min_required}₽"
        )
        return True
    
    # Принимаем ставку
    with DB:
        DB.execute("""
            UPDATE auctions
            SET current_bid = ?, current_bidder_id = ?, current_bidder_label = ?
            WHERE channel_id = ? AND post_id = ?
        """, (bid, user.id, label, channel_id, post_id))
    
    await message.reply_text(f"✅ Ставка {bid}₽ принята")
    await update_auction_post(context, channel_id, post_id)
    
    return True

async def finalize_auction(context, channel_id, post_id):
    """Завершает аукцион по истечении времени"""
    auction = DB.execute("""
        SELECT current_bid, current_bidder_label, is_active
        FROM auctions
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not auction or not auction[2]:
        return
    
    current_bid, current_bidder_label, _ = auction
    
    with DB:
        DB.execute("""
            UPDATE auctions
            SET is_active = 0
            WHERE channel_id = ? AND post_id = ?
        """, (channel_id, post_id))
    
    await update_auction_post(context, channel_id, post_id)

async def check_auctions(context):
    """Проверяет окончание аукционов"""
    now = datetime.now().isoformat()
    
    expired = DB.execute("""
        SELECT channel_id, post_id
        FROM auctions
        WHERE is_active = 1 AND end_time <= ?
    """, (now,)).fetchall()
    
    for channel_id, post_id in expired:
        await finalize_auction(context, channel_id, post_id)

async def stop_auction(update, context):
    """Команда /stop_ayk - досрочно остановить аукцион"""
    message = update.effective_message
    if not message:
        return
    
    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return
    
    # Находим аукцион в текущем чате
    reply = message.reply_to_message
    if not reply or not reply.is_automatic_forward:
        await message.reply_text("❌ Ответьте на пост аукциона")
        return
    
    origin = reply.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        await message.reply_text("❌ Ответьте на пост аукциона")
        return
    
    channel_id = origin.chat.id
    post_id = origin.message_id
    
    with DB:
        DB.execute("""
            UPDATE auctions
            SET is_active = 0
            WHERE channel_id = ? AND post_id = ?
        """, (channel_id, post_id))
    
    await update_auction_post(context, channel_id, post_id)
    await message.reply_text("✅ Аукцион остановлен")

async def start_auction(update, context):
    """Команда /start_ayk - возобновить аукцион"""
    message = update.effective_message
    if not message:
        return
    
    username = (message.from_user.username or "").lower()
    if username not in CANCEL_USERS:
        await message.reply_text("❌ У вас нет прав для этой команды")
        return
    
    reply = message.reply_to_message
    if not reply or not reply.is_automatic_forward:
        await message.reply_text("❌ Ответьте на пост аукциона")
        return
    
    origin = reply.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        await message.reply_text("❌ Ответьте на пост аукциона")
        return
    
    channel_id = origin.chat.id
    post_id = origin.message_id
    
    auction = DB.execute("""
        SELECT duration_minutes, end_time
        FROM auctions
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not auction:
        await message.reply_text("❌ Аукцион не найден")
        return
    
    duration_minutes, end_time_str = auction
    
    # Если время вышло, продлеваем на исходную длительность
    if end_time_str:
        end_time = datetime.fromisoformat(end_time_str)
        if datetime.now() > end_time:
            new_end_time = datetime.now() + timedelta(minutes=duration_minutes)
            with DB:
                DB.execute("""
                    UPDATE auctions
                    SET is_active = 1, end_time = ?
                    WHERE channel_id = ? AND post_id = ?
                """, (new_end_time.isoformat(), channel_id, post_id))
        else:
            with DB:
                DB.execute("""
                    UPDATE auctions
                    SET is_active = 1
                    WHERE channel_id = ? AND post_id = ?
                """, (channel_id, post_id))
    else:
        new_end_time = datetime.now() + timedelta(minutes=duration_minutes)
        with DB:
            DB.execute("""
                UPDATE auctions
                SET is_active = 1, end_time = ?
                WHERE channel_id = ? AND post_id = ?
            """, (new_end_time.isoformat(), channel_id, post_id))
    
    await update_auction_post(context, channel_id, post_id)
    await message.reply_text("✅ Аукцион возобновлен")

# =====================================================
# NORMAL SHOP POSITIONS
# =====================================================
def extract_positions(text):
    """
    Поддерживает форматы:
    Позиции:
    1.
    2. @username
    3.@username
    Также поддерживает:
     1.
     @username
     2.
    """
    lines = text.splitlines()
    positions = {}
    started = False
    previous_number = None
    
    for line in lines:
        if not started:
            if re.search(r"позиции\s*:", line, re.IGNORECASE):
                started = True
            continue
        
        match = re.match(
            r"^\s*(\d+)\s*[.)]\s*(.*?)\s*$",
            line,
        )
        
        if match:
            number = int(match.group(1))
            label = match.group(2).strip()
            positions[number] = label
            previous_number = number
            continue
        
        if not line.strip():
            continue
        
        if (
            previous_number is not None
            and line.strip().startswith("@")
        ):
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
        SELECT 1 FROM shops
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    with DB:
        if not exists:
            DB.execute("""
                INSERT INTO shops (
                    channel_id, post_id, base_caption
                )
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
                    channel_id, post_id, number,
                    owner_user_id, owner_label
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                channel_id,
                post_id,
                number,
                owner_id,
                owner_name,
            ))

def render_shop_post(channel_id, post_id):
    row = DB.execute("""
        SELECT base_caption FROM shops
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not row:
        return None
    
    original = row[0]
    lines = original.splitlines()
    
    slots = DB.execute("""
        SELECT number, owner_label
        FROM slots
        WHERE channel_id = ? AND post_id = ?
        ORDER BY number
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
        
        match = re.match(
            r"^(\s*)(\d+)(\s*[.)]\s*)(.*)$",
            line,
        )
        
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
        
        if (
            not original_label
            and number in slot_map
        ):
            skip_username = True
    
    return "\n".join(output)

async def update_shop_post(context, channel_id, post_id):
    text = render_shop_post(channel_id, post_id)
    if text is None:
        return
    
    try:
        await context.bot.edit_message_text(
            chat_id=channel_id,
            message_id=post_id,
            text=text,
        )
        return
    except TelegramError:
        pass
    
    try:
        await context.bot.edit_message_caption(
            chat_id=channel_id,
            message_id=post_id,
            caption=text,
        )
    except TelegramError:
        logger.exception(
            "Не удалось отредактировать пост %s в канале %s",
            post_id,
            channel_id,
        )

def get_discussion_mapping(message):
    thread_id = message.message_thread_id
    if thread_id:
        return DB.execute("""
            SELECT channel_id, post_id
            FROM discussion_posts
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
    
    mapping = get_discussion_mapping(message)
    if not mapping:
        return
    
    channel_id, post_id = mapping
    
    shop = DB.execute("""
        SELECT 1 FROM shops
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not shop:
        root = message.reply_to_message
        if root and root.is_automatic_forward:
            get_or_create_shop(
                channel_id,
                post_id,
                get_caption(root),
            )
    
    shop = DB.execute("""
        SELECT 1 FROM shops
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if not shop:
        return
    
    text = message.text.strip()
    
    if not re.fullmatch(
        r"\d+(?:\s*(?:,|;|\s+и\s+|\s+)\s*\d+)*",
        text,
        re.IGNORECASE,
    ):
        return
    
    requested = list(dict.fromkeys(
        int(value) for value in re.findall(r"\d+", text)
    ))
    
    if not requested:
        return
    
    rows = DB.execute("""
        SELECT number, owner_user_id, owner_label
        FROM slots
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchall()
    
    slot_map = {
        number: (owner_id, label)
        for number, owner_id, label in rows
    }
    
    missing = [
        number for number in requested
        if number not in slot_map
    ]
    
    if missing:
        await message.reply_text(
            "❌ Не найдены позиции: "
            + ", ".join(map(str, missing))
        )
        return
    
    user = message.from_user
    label = user_label(user)
    
    occupied = [
        number for number in requested
        if slot_map[number][0] is not None
        and slot_map[number][0] != user.id
    ]
    
    if occupied:
        await message.reply_text(
            "❌ Уже заняты позиции: "
            + ", ".join(map(str, occupied))
        )
        return
    
    assigned = []
    
    with DB:
        for number in requested:
            cursor = DB.execute("""
                UPDATE slots
                SET owner_user_id = ?, owner_label = ?
                WHERE channel_id = ?
                  AND post_id = ?
                  AND number = ?
                  AND (
                      owner_user_id IS NULL
                      OR owner_user_id = ?
                  )
            """, (
                user.id,
                label,
                channel_id,
                post_id,
                number,
                user.id,
            ))
            
            if cursor.rowcount == 1:
                assigned.append(number)
    
    if assigned:
        await update_shop_post(context, channel_id, post_id)
        await message.reply_text(
            f"✅ {label}, вы записались на позиции: "
            + ", ".join(map(str, assigned))
        )

# =====================================================
# LOTTERIES
# =====================================================
def find_lottery(text):
    lot_match = re.search(LOTTERY_PATTERN, text, re.IGNORECASE)
    count_match = re.search(
        r"Всего\s*(\d+)\s*номерков",
        text,
        re.IGNORECASE,
    )
    
    if not lot_match or not count_match:
        return None
    
    lot_number = int(lot_match.group(1))
    count = int(count_match.group(1))
    
    if count < 1:
        return None
    
    return lot_number, count

def lottery_list(channel_id, post_id, lot_number):
    rows = DB.execute("""
        SELECT number, owner_label
        FROM lot_tickets
        WHERE channel_id = ? AND post_id = ?
        ORDER BY number
    """, (channel_id, post_id)).fetchall()
    
    lines = [f"🎟️ Номерки лотереи #{lot_number}", ""]
    
    for number, owner in rows:
        lines.append(f"{number}. {owner or 'Свободно'}")
    
    return "\n".join(lines)

async def create_lottery(message, origin):
    parsed = find_lottery(get_caption(message))
    if not parsed:
        return
    
    lot_number, count = parsed
    channel_id = origin.chat.id
    post_id = origin.message_id
    
    exists = DB.execute("""
        SELECT 1 FROM lots
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if exists:
        return
    
    with DB:
        DB.execute("""
            INSERT INTO lots (
                channel_id, post_id, lot_number, ticket_count,
                discussion_chat_id, thread_id, list_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL)
        """, (
            channel_id,
            post_id,
            lot_number,
            count,
            message.chat.id,
            message.message_id,
        ))
        
        DB.executemany("""
            INSERT INTO lot_tickets (
                channel_id, post_id, number,
                owner_user_id, owner_label
            )
            VALUES (?, ?, ?, NULL, NULL)
        """, [
            (channel_id, post_id, number)
            for number in range(1, count + 1)
        ])
    
    try:
        sent = await message.reply_text(
            lottery_list(channel_id, post_id, lot_number)
        )
    except TelegramError:
        logger.exception("Не удалось создать список лотереи")
        return
    
    with DB:
        DB.execute("""
            UPDATE lots
            SET list_message_id = ?
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
            )
            VALUES (?, ?, ?, ?)
        """, (
            message.chat.id,
            message.message_id,
            channel_id,
            post_id,
        ))
    
    get_or_create_shop(channel_id, post_id, get_caption(message))
    await create_lottery(message, origin)
    await create_auction(message, origin)

def find_lottery_for_message(message):
    thread_id = message.message_thread_id
    if thread_id:
        result = DB.execute("""
            SELECT channel_id, post_id, lot_number,
                   ticket_count, list_message_id
            FROM lots
            WHERE discussion_chat_id = ? AND thread_id = ?
        """, (message.chat.id, thread_id)).fetchone()
        
        if result:
            return result
    
    reply = message.reply_to_message
    if reply:
        if reply.is_automatic_forward:
            origin = reply.forward_origin
            if isinstance(origin, MessageOriginChannel):
                return DB.execute("""
                    SELECT channel_id, post_id, lot_number,
                           ticket_count, list_message_id
                    FROM lots
                    WHERE channel_id = ? AND post_id = ?
                """, (
                    origin.chat.id,
                    origin.message_id,
                )).fetchone()
        
        return DB.execute("""
            SELECT channel_id, post_id, lot_number,
                   ticket_count, list_message_id
            FROM lots
            WHERE discussion_chat_id = ?
              AND list_message_id = ?
        """, (
            message.chat.id,
            reply.message_id,
        )).fetchone()
    
    return None

def parse_lottery_request(text):
    lowered = text.strip().lower()
    
    if "отмена" in lowered:
        return [], 0
    
    words = re.findall(r"[a-zа-яё]+", lowered)
    random_count = sum(word in RANDOM_WORDS for word in words)
    
    leading = re.fullmatch(
        r"\s*(\d+)\s+(?:(?:рд|рандом|рандомный)\s*)+",
        lowered,
    )
    
    if leading:
        return [], min(int(leading.group(1)), 1000)
    
    numbers = list(dict.fromkeys(
        int(value) for value in re.findall(r"\d+", lowered)
    ))
    
    return numbers, random_count

async def refresh_lottery_list(
    context,
    chat_id,
    list_message_id,
    channel_id,
    post_id,
    lot_number,
):
    if not list_message_id:
        return
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=list_message_id,
            text=lottery_list(channel_id, post_id, lot_number),
        )
    except TelegramError:
        logger.exception("Не удалось обновить список лотереи")

async def handle_lottery_comment(update, context):
    message = update.effective_message
    if not message or not message.text or not message.from_user:
        return False
    
    lot = find_lottery_for_message(message)
    if not lot:
        return False
    
    channel_id, post_id, lot_number, count, list_message_id = lot
    text = message.text.strip()
    lowered = text.lower()
    user = message.from_user
    username = (user.username or "").lower()
    
    if "отмена" in lowered:
        if username not in CANCEL_USERS:
            await message.reply_text(
                "❌ Отменять номерки могут только "
                "@Kari_escape, @tihomirkrd и @PaboKim."
            )
            return True
        
        numbers = list(dict.fromkeys(
            int(value) for value in re.findall(r"\d+", text)
        ))
        
        if not numbers:
            await message.reply_text(
                "Напиши, например: «номерки 3, 5 отмена»."
            )
            return True
        
        invalid = [n for n in numbers if n < 1 or n > count]
        
        if invalid:
            await message.reply_text(
                f"❌ Номерки должны быть от 1 до {count}. "
                "Неверные номера: "
                + ", ".join(map(str, invalid))
            )
            return True
        
        with DB:
            DB.executemany("""
                UPDATE lot_tickets
                SET owner_user_id = NULL, owner_label = NULL
                WHERE channel_id = ? AND post_id = ? AND number = ?
            """, [
                (channel_id, post_id, number)
                for number in numbers
            ])
        
        await message.reply_text(
            "✅ Освобождены номерки: "
            + ", ".join(map(str, numbers))
        )
        
        await refresh_lottery_list(
            context, message.chat.id, list_message_id,
            channel_id, post_id, lot_number,
        )
        
        return True
    
    explicit_numbers, random_count = parse_lottery_request(text)
    
    if not explicit_numbers and random_count == 0:
        return True
    
    invalid = [
        number for number in explicit_numbers
        if number < 1 or number > count
    ]
    
    if invalid:
        await message.reply_text(
            f"❌ Номерки должны быть от 1 до {count}. "
            "Неверные номера: "
            + ", ".join(map(str, invalid))
        )
        return True
    
    label = user_label(user)
    assigned = []
    unavailable = []
    
    with DB:
        for number in explicit_numbers:
            cursor = DB.execute("""
                UPDATE lot_tickets
                SET owner_user_id = ?, owner_label = ?
                WHERE channel_id = ?
                  AND post_id = ?
                  AND number = ?
                  AND owner_user_id IS NULL
            """, (
                user.id,
                label,
                channel_id,
                post_id,
                number,
            ))
            
            if cursor.rowcount == 1:
                assigned.append(number)
            else:
                unavailable.append(number)
        
        for _ in range(random_count):
            free = DB.execute("""
                SELECT number
                FROM lot_tickets
                WHERE channel_id = ?
                  AND post_id = ?
                  AND owner_user_id IS NULL
            """, (channel_id, post_id)).fetchall()
            
            if not free:
                break
            
            number = random.choice([row[0] for row in free])
            
            cursor = DB.execute("""
                UPDATE lot_tickets
                SET owner_user_id = ?, owner_label = ?
                WHERE channel_id = ?
                  AND post_id = ?
                  AND number = ?
                  AND owner_user_id IS NULL
            """, (
                user.id,
                label,
                channel_id,
                post_id,
                number,
            ))
            
            if cursor.rowcount == 1:
                assigned.append(number)
    
    responses = []
    
    if assigned:
        responses.append(
            f"✅ {label}, твои номерки: "
            + ", ".join(map(str, assigned))
        )
    
    if unavailable:
        responses.append(
            "⚠️ Уже заняты: "
            + ", ".join(map(str, unavailable))
        )
    
    random_assigned = sum(
        1 for number in assigned if number not in explicit_numbers
    )
    
    if random_count > random_assigned:
        responses.append(
            "⚠️ Не хватило свободных номерков для всех случайных заявок."
        )
    
    if not responses:
        responses.append("❌ Свободных номерков больше нет.")
    
    await message.reply_text("\n".join(responses))
    
    await refresh_lottery_list(
        context,
        message.chat.id,
        list_message_id,
        channel_id,
        post_id,
        lot_number,
    )
    
    return True

# =====================================================
# ALL COMMENTS
# =====================================================
async def handle_all_comments(update, context):
    handled = await handle_auction_comment(update, context)
    if handled:
        return
    
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
    
    # Обновляем аукцион, если это пост аукциона
    auction = DB.execute("""
        SELECT 1 FROM auctions
        WHERE channel_id = ? AND post_id = ?
    """, (channel_id, post_id)).fetchone()
    
    if auction:
        with DB:
            DB.execute("""
                UPDATE auctions
                SET base_caption = ?
                WHERE channel_id = ? AND post_id = ?
            """, (text, channel_id, post_id))
        
        await update_auction_post(context, channel_id, post_id)
        return
    
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
                        channel_id, post_id, number,
                        owner_user_id, owner_label
                    )
                    VALUES (?, ?, ?, NULL, NULL)
                """, (channel_id, post_id, number))
            else:
                DB.execute("""
                    INSERT INTO slots (
                        channel_id, post_id, number,
                        owner_user_id, owner_label
                    )
                    VALUES (?, ?, ?, -1, ?)
                    ON CONFLICT(channel_id, post_id, number)
                    DO UPDATE SET
                        owner_user_id = CASE
                            WHEN slots.owner_user_id IS NULL
                              OR slots.owner_user_id = -1
                            THEN -1
                            ELSE slots.owner_user_id
                        END,
                        owner_label = CASE
                            WHEN slots.owner_user_id IS NULL
                              OR slots.owner_user_id = -1
                            THEN excluded.owner_label
                            ELSE slots.owner_label
                        END
                """, (channel_id, post_id, number, label))

# =====================================================
# COMMANDS
# =====================================================
async def start(update, context):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Привет! Я бот магазина PABO SHOP 🎟️\n\n"
            "Разбор: напиши номер позиции, например 6 или 6, 7.\n"
            "Лотерея: напиши любой номер или рд, например 1 или 3 рд. \n"
            "Случайные номерки: «3 рд» выдаёт три случайных номерка.\n"
            "Аукцион: напиши ставку в рублях, например 250₽\n"
            "Отмена номерков доступна только админам шопа."
        )

async def ping(update, context):
    if update.effective_message:
        await update.effective_message.reply_text("Бот работает ✅")

# =====================================================
# START BOT
# =====================================================
def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. "
            "Установи токен в переменную окружения."
        )
    
    init_db()
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("stop_ayk", stop_auction))
    app.add_handler(CommandHandler("start_ayk", start_auction))
    
    app.add_handler(
        MessageHandler(filters.ALL, handle_root_post),
        group=-1,
    )
    
    app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_CHANNEL_POST,
            handle_edited_channel_post,
        ),
        group=0,
    )
    
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_all_comments,
        ),
        group=0,
    )
    
    # Добавляем проверку окончания аукционов каждые 30 секунд
    app.job_queue.run_repeating(check_auctions, interval=30, first=10)
    
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()