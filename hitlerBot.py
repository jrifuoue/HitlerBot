import asyncio
import os
import random
import logging
import traceback
import sqlite3
import time

from threading import Thread

from flask import Flask
from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)


# =========================================================
# Flask
# =========================================================

app = Flask(__name__)


@app.route("/ping")
def ping():
    return "pong"


# =========================================================
# تنظیمات
# =========================================================

API_TOKEN = os.getenv("TOKEN")

if not API_TOKEN:
    raise RuntimeError("TOKEN environment variable is not set.")

bot = AsyncTeleBot(API_TOKEN)

# روی Render این متغیر را می‌توانی مثلاً /var/data بگذاری
# اگر وجود نداشته باشد، دیتابیس کنار فایل bot.py ساخته می‌شود.
DATA_DIR = os.getenv("DATA_DIR", ".")

os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "soaps.db")

BUILD_TIME = 60
FACTORY_LIFETIME = 100


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    filename=os.path.join(DATA_DIR, "bot_errors.log"),
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


# =========================================================
# Database
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row

    # برای چند درخواست همزمان SQLite بهتر کار می‌کند
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


def init_db():
    conn = get_db()

    # اطلاعات خود کاربر
    #
    # نکته مهم:
    # PRIMARY KEY فقط user_id است.
    #
    # بنابراین اطلاعات یک کاربر بین تمام گروه‌ها مشترک است.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,

            furnaces INTEGER DEFAULT 0,
            gas INTEGER DEFAULT 0,
            last REAL DEFAULT 0,

            fat INTEGER DEFAULT 0,
            ash INTEGER DEFAULT 0,
            soap INTEGER DEFAULT 0,

            factory_building INTEGER DEFAULT 0,
            factory_start_time REAL DEFAULT 0,
            factory_soaps_ready INTEGER DEFAULT 0,
            factory_total_built INTEGER DEFAULT 0
        )
    """)

    # مشخص می‌کند چه کاربری در چه گروهی دیده شده.
    #
    # خود اطلاعات اقتصادی کاربر اینجا نیست؛
    # فقط عضویت/دیده‌شدن کاربر در گروه نگهداری می‌شود.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# Safe async execute
# =========================================================

async def safe_execute(coro, *args, **kwargs):
    try:
        await coro(*args, **kwargs)

    except Exception as e:
        logging.error(
            "Exception in %s: %s\n%s",
            coro.__name__,
            str(e),
            traceback.format_exc()
        )

        message = kwargs.get("message")

        if message:
            try:
                await bot.reply_to(
                    message,
                    "⚠️ یه خطای غیرمنتظره رخ داد، بعداً دوباره امتحان کن!"
                )
            except Exception:
                pass


# =========================================================
# Factory
# =========================================================

class Factory:

    def __init__(self, info=None):
        info = info or {}

        self.building = bool(info.get("building", False))
        self.start_time = float(info.get("start_time", 0))
        self.soaps_ready = int(info.get("soaps_ready", 0))
        self.total_built = int(info.get("total_built", 0))


    async def produce_soaps(self, user):

        if not self.building:
            return

        now = time.time()

        elapsed = now - self.start_time

        if elapsed < BUILD_TIME:
            return

        cycles = int(elapsed // BUILD_TIME)

        if cycles <= 0:
            return

        # بیشتر از عمر کارخانه تولید نکن
        remaining_lifetime = FACTORY_LIFETIME - self.total_built

        if remaining_lifetime <= 0:
            self.building = False
            self.start_time = 0

            await user.save_to_data()
            return

        cycles = min(cycles, remaining_lifetime)

        # هر صابون:
        # 1 خاکستر + 1 چربی
        possible_cycles = min(
            cycles,
            user.ash,
            user.fat
        )

        # زمان چرخه‌های سپری‌شده را جلو می‌بریم
        # حتی اگر مواد اولیه کافی نبوده باشند.
        self.start_time += cycles * BUILD_TIME

        if possible_cycles <= 0:
            await user.save_to_data()
            return

        user.ash -= possible_cycles
        user.fat -= possible_cycles

        self.soaps_ready += possible_cycles
        self.total_built += possible_cycles

        # کارخانه بعد از 100 صابون خراب می‌شود
        if self.total_built >= FACTORY_LIFETIME:
            self.building = False
            self.start_time = 0

            try:
                await bot.send_message(
                    user.chat_id,
                    "🏭 کارخانه شما پس از ساخت 100 صابون خراب شد! باید دوباره بسازید."
                )
            except Exception:
                pass

        await user.save_to_data()


# =========================================================
# User
# =========================================================

class User:

    def __init__(self, chat_id, telegram_user):

        self.chat_id = chat_id

        # مهم:
        # داده‌های اقتصادی بر اساس user_id هستند،
        # نه chat_id.
        self.user_id = telegram_user.id

        self.first_name = telegram_user.first_name or "کاربر"

        self.furnaces = 0
        self.gas = 0
        self.last = 0

        self.fat = 0
        self.ash = 0
        self.soap = 0

        self.factory = Factory()

        self.load_from_db()


    def load_from_db(self):

        conn = get_db()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE user_id = ?
        """, (self.user_id,)).fetchone()

        if row:

            self.first_name = row["first_name"] or self.first_name

            self.furnaces = row["furnaces"]
            self.gas = row["gas"]
            self.last = row["last"]

            self.fat = row["fat"]
            self.ash = row["ash"]
            self.soap = row["soap"]

            self.factory = Factory({
                "building": bool(row["factory_building"]),
                "start_time": row["factory_start_time"],
                "soaps_ready": row["factory_soaps_ready"],
                "total_built": row["factory_total_built"]
            })

        conn.close()


    async def save_to_data(self):

        conn = get_db()

        conn.execute("""
            INSERT INTO users (
                user_id,
                first_name,

                furnaces,
                gas,
                last,

                fat,
                ash,
                soap,

                factory_building,
                factory_start_time,
                factory_soaps_ready,
                factory_total_built
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                first_name = excluded.first_name,

                furnaces = excluded.furnaces,
                gas = excluded.gas,
                last = excluded.last,

                fat = excluded.fat,
                ash = excluded.ash,
                soap = excluded.soap,

                factory_building = excluded.factory_building,
                factory_start_time = excluded.factory_start_time,
                factory_soaps_ready = excluded.factory_soaps_ready,
                factory_total_built = excluded.factory_total_built
        """, (
            self.user_id,
            self.first_name,

            self.furnaces,
            self.gas,
            self.last,

            self.fat,
            self.ash,
            self.soap,

            int(self.factory.building),
            self.factory.start_time,
            self.factory.soaps_ready,
            self.factory.total_built
        ))

        # ثبت اینکه این کاربر در این گروه دیده شده
        conn.execute("""
            INSERT OR IGNORE INTO chat_users (
                chat_id,
                user_id
            )
            VALUES (?, ?)
        """, (
            self.chat_id,
            self.user_id
        ))

        conn.commit()
        conn.close()


    async def check_factory(self):
        await self.factory.produce_soaps(self)
        await self.save_to_data()


# =========================================================
# Status
# =========================================================

async def send_status(user: User, message: Message):

    markup = InlineKeyboardMarkup(row_width=2)

    markup.add(
        InlineKeyboardButton(
            "🧼 جمع‌آوری صابون",
            callback_data="collect_soap"
        ),
        InlineKeyboardButton(
            "🏆 لیدربورد",
            callback_data="leaderboard"
        )
    )

    text = (
        f"📊 *وضعیت شما*\n\n"
        f"💪 چربی: {user.fat}\n"
        f"🔥 خاکستر: {user.ash}\n"
        f"🧼 صابون: {user.soap}\n"
        f"🏭 کارخانه فعال: "
        f"{'✅' if user.factory.building else '❌'}\n"
        f"🧼 صابون آماده برای جمع‌آوری: "
        f"{user.factory.soaps_ready}\n"
        f"📦 کل صابون تولید شده توسط کارخانه: "
        f"{user.factory.total_built}"
    )

    await bot.send_message(
        message.chat.id,
        text,
        reply_markup=markup,
        parse_mode="Markdown"
    )


# =========================================================
# Leaderboard
# =========================================================

async def get_leaderboard(chat_id):

    conn = get_db()

    rows = conn.execute("""
        SELECT
            users.first_name,
            users.soap
        FROM chat_users
        INNER JOIN users
            ON users.user_id = chat_users.user_id
        WHERE chat_users.chat_id = ?
        ORDER BY users.soap DESC
        LIMIT 10
    """, (chat_id,)).fetchall()

    conn.close()

    return rows


def format_leaderboard(rows):

    if not rows:
        return "🏆 هیچ کاربری در گپ موجود نیست."

    ranking_text = "🏆 رنکینگ کاربران بر اساس صابون:\n\n"

    for i, row in enumerate(rows, 1):
        name = row["first_name"] or "کاربر"
        soap = row["soap"] or 0

        ranking_text += (
            f"{i}. {name} — {soap} صابون\n"
        )

    return ranking_text


# =========================================================
# Process message
# =========================================================

async def process_message(message: Message):

    if not message.text:
        return

    text = message.text.strip().lower()

    chat_id = message.chat.id

    # اطلاعات کاربر از SQLite خوانده می‌شود
    user = User(
        chat_id,
        message.from_user
    )

    # تولید کارخانه قبل از اجرای دستور
    await user.check_factory()


    # =====================================================
    # شعار
    # =====================================================

    trigger_texts = [
        "مرگ بر اسرائیل",
        "مرگ بر یهود",
        "درود بر هیتلر",
        "هایل هیتلر",
        "卐",
        "درود بر کوروش",
        "مرگ بر بات ضحاک",
        "زنده باد هیتلر",
        "زنده باد نازیسم"
    ]

    if text in trigger_texts:

        now = time.time()

        if now - user.last >= 100:

            fat = random.randint(3, 15)

            user.fat += fat
            user.last = now

            await user.save_to_data()

            await bot.reply_to(
                message,
                f"{fat} چربی به حسابت اضافه شد!\n"
                f"کل چربی: {user.fat}"
            )

        return


    # =====================================================
    # خرید کوره
    # =====================================================

    elif text == "خرید کوره":

        if user.fat >= 10:

            user.fat -= 10

            user.furnaces = 1
            user.gas = 3

            await user.save_to_data()

            await bot.reply_to(
                message,
                f"🔥 کوره خریدی! ۳ بار قابل استفاده است.\n"
                f"چربی باقی‌مانده: {user.fat}"
            )

        else:

            await bot.reply_to(
                message,
                f"🚫 چربی کافی نداری! نیاز به ۱۰ واحد داری.\n"
                f"چربی فعلی: {user.fat}"
            )

        return


    # =====================================================
    # خرید گاز
    # =====================================================

    elif text == "خرید گاز":

        if user.furnaces <= 0:

            await bot.reply_to(
                message,
                "❌ تو کوره نداری! اول یه کوره بخر."
            )

            return

        if user.fat < 1:

            await bot.reply_to(
                message,
                "🚫 چربی کافی نداری! نیاز به 1 واحد داری."
            )

            return

        user.fat -= 1
        user.gas += 1

        await user.save_to_data()

        await bot.reply_to(
            message,
            "⛽ شما یک واحد گاز خریدید."
        )

        return


    # =====================================================
    # ساخت کارخانه
    # =====================================================

    elif text == "ساخت کارخانه":

        if user.factory.building:

            await bot.reply_to(
                message,
                "🏭 کارخانه در حال حاضر فعال است!"
            )

        else:

            user.factory.building = True
            user.factory.start_time = time.time()

            user.factory.soaps_ready = 0
            user.factory.total_built = 0

            await user.save_to_data()

            await bot.reply_to(
                message,
                "🏭 کارخانه ساخته شد و شروع به تولید صابون می‌کند "
                "(هر صابون ۱ دقیقه)."
            )

        return


    # =====================================================
    # جمع‌آوری صابون
    # =====================================================

    elif text in [
        "جمع آوری صابون",
        "جمع‌آوری صابون"
    ]:

        ready = user.factory.soaps_ready

        if ready <= 0:

            await bot.reply_to(
                message,
                "🧼 هنوز صابونی آماده نشده است."
            )

        else:

            user.soap += ready

            user.factory.soaps_ready = 0

            await user.save_to_data()

            await bot.reply_to(
                message,
                f"🧼 {ready} صابون جمع‌آوری شد! "
                f"کل صابون: {user.soap}"
            )

        return


    # =====================================================
    # برو تو کوره
    # =====================================================

    elif text == "برو تو کوره":

        if not message.reply_to_message:

            await bot.reply_to(
                message,
                "❌ برای 'برو تو کوره' باید به کسی ریپلای کنی!"
            )

            return

        if user.furnaces <= 0:

            await bot.reply_to(
                message,
                "🔥 تو کوره‌ای نداری! اول یه کوره بخر."
            )

            return

        if user.gas <= 0:

            await bot.reply_to(
                message,
                "⛔ گاز کوره تموم شده! با 'خرید گاز' پرش کن."
            )

            return


        # داده target هم از SQLite گرفته می‌شود
        #
        # مهم:
        # هیچ data مشترک در RAM نداریم.
        target = User(
            chat_id,
            message.reply_to_message.from_user
        )


        if target.user_id == user.user_id:

            await bot.reply_to(
                message,
                "😂 نمی‌تونی خودتو بندازی تو کوره!"
            )

            return


        ash = random.randint(3, 10)

        user.ash += ash

        user.gas -= 1

        target.soap = max(
            0,
            target.soap - 2
        )

        await user.save_to_data()
        await target.save_to_data()

        await bot.reply_to(
            message,
            f"🔥 {target.first_name} رفت تو کوره!\n"
            f"{target.first_name} {ash} خاکستر تولید شد 💨\n"
            f"گاز کوره باقی‌مانده: {user.gas}"
        )

        return


    # =====================================================
    # وضعیت
    # =====================================================

    elif text == "وضعیت":

        await send_status(
            user,
            message
        )

        return


    # =====================================================
    # لیدربورد
    # =====================================================

    elif text == "لیدربورد":

        rows = await get_leaderboard(chat_id)

        ranking_text = format_leaderboard(rows)

        await bot.reply_to(
            message,
            ranking_text
        )

        return


# =========================================================
# Callback
# =========================================================

async def process_callback(call: CallbackQuery):

    chat_id = call.message.chat.id

    user = User(
        chat_id,
        call.from_user
    )

    await user.check_factory()


    # =====================================================
    # جمع‌آوری صابون
    # =====================================================

    if call.data == "collect_soap":

        ready = user.factory.soaps_ready

        if ready <= 0:

            await bot.answer_callback_query(
                call.id,
                "🧼 هنوز صابونی آماده نشده است."
            )

        else:

            user.soap += ready

            user.factory.soaps_ready = 0

            await user.save_to_data()

            await bot.answer_callback_query(
                call.id,
                f"🧼 {ready} صابون جمع‌آوری شد! "
                f"کل صابون: {user.soap}"
            )


    # =====================================================
    # لیدربورد
    # =====================================================

    elif call.data == "leaderboard":

        rows = await get_leaderboard(chat_id)

        ranking_text = format_leaderboard(rows)

        await bot.answer_callback_query(
            call.id
        )

        await bot.send_message(
            chat_id,
            ranking_text
        )


# =========================================================
# Start / Help
# =========================================================

@bot.message_handler(commands=["start", "help"])
async def send_welcome(message: Message):

    await safe_execute(
        bot.reply_to,
        message,

        "سلام! دستورات:\n"
        "1. شعار => چربی می‌گیری\n"
        "2. خرید کوره => کوره بخرید\n"
        "3. ریپلای 'برو تو کوره' => خاکستر می‌گیرید\n"
        "4. خرید گاز => گاز برای کوره بخرید\n"
        "5. وضعیت => وضعیت خود را ببینید (با دکمه‌ها)\n"
        "6. لیدربورد => لیدربورد را ببینید (با دکمه‌ها)"
    )


# =========================================================
# Message Handler
# =========================================================

@bot.message_handler(
    func=lambda m: True,
    content_types=["text"]
)
async def reply_text(message: Message):

    await safe_execute(
        process_message,
        message=message
    )


# =========================================================
# Callback Handler
# =========================================================

@bot.callback_query_handler(
    func=lambda call: True
)
async def callback_handler(call):

    await safe_execute(
        process_callback,
        call=call
    )


# =========================================================
# Bot
# =========================================================

async def main():

    print("Bot is running...")

    await bot.infinity_polling()


# =========================================================
# Keep Alive
# =========================================================

def keep_alive():

    while True:

        try:

            # روی Render آدرس واقعی همین سرویس را می‌گیرد.
            # دیگر giftybot.onrender.com هاردکد نشده.
            url = os.getenv("RENDER_EXTERNAL_URL")

            if url:

                requests.get(
                    f"{url}/ping",
                    timeout=10
                )

        except Exception as e:

            logging.error(
                "Keep-alive error: %s",
                e
            )

        # هر 5 دقیقه
        time.sleep(300)


# =========================================================
# Flask
# =========================================================

def run_flask():

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )


# =========================================================
# Start
# =========================================================

init_db()

Thread(
    target=run_flask,
    daemon=True
).start()

Thread(
    target=keep_alive,
    daemon=True
).start()


if __name__ == "__main__":

    asyncio.run(main())
