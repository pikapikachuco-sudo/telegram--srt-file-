import os
import re
import io
import logging
import threading
import asyncio
import time

import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

# groq lazy import — RateLimitError handled by name check
import groq as groq_module

# ──────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
#  CONFIG FROM ENVIRONMENT
# ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
RENDER_URL     = os.environ.get("RENDER_URL", "")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "")
CHANNEL_LINK   = os.environ.get("CHANNEL_LINK", "https://t.me/yourchannel")

_raw = os.environ.get("GROQ_API_KEYS", os.environ.get("GROQ_API_KEY", ""))
GROQ_KEYS = [k.strip() for k in _raw.split(",") if k.strip()]

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable missing!")
if not GROQ_KEYS:
    raise RuntimeError("GROQ_API_KEYS environment variable missing!")

BATCH_SIZE = 10

# ──────────────────────────────────────────────────────────
#  MULTI-KEY MANAGER
# ──────────────────────────────────────────────────────────
class KeyManager:
    def __init__(self, keys):
        self.keys    = keys
        self._idx    = 0
        self._lock   = threading.Lock()
        self.clients = [groq_module.Groq(api_key=k) for k in keys]
        log.info(f"KeyManager: {len(keys)} টি API key লোড হয়েছে")

    @property
    def idx(self):
        return self._idx

    def current(self):
        with self._lock:
            return self.clients[self._idx]

    def current_num(self):
        with self._lock:
            return self._idx + 1

    def total(self):
        return len(self.keys)

    def rotate(self):
        with self._lock:
            self._idx = (self._idx + 1) % len(self.keys)
            log.info(f"API key #{self._idx + 1} তে স্যুইচ")
            return self._idx


km = KeyManager(GROQ_KEYS)

# ──────────────────────────────────────────────────────────
#  FLASK (keep-alive + self-ping)
# ──────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "OK", 200

@flask_app.route("/ping")
def ping_route():
    return "pong", 200

def _run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def _self_ping():
    if not RENDER_URL:
        log.warning("RENDER_URL নেই — self-ping বন্ধ")
        return
    time.sleep(60)
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/ping", timeout=10)
            log.info(f"Self-ping: {r.status_code}")
        except Exception as e:
            log.warning(f"Self-ping ব্যর্থ: {e}")
        time.sleep(600)

# ──────────────────────────────────────────────────────────
#  USER STATE
# ──────────────────────────────────────────────────────────
user_states: dict = {}

# ──────────────────────────────────────────────────────────
#  SUBTITLE PARSER
# ──────────────────────────────────────────────────────────
def parse_srt(text):
    blocks, seen = [], set()
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = chunk.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            key = lines[1].strip()
            if key not in seen:
                seen.add(key)
                blocks.append({
                    "index": lines[0].strip(),
                    "time":  lines[1].strip(),
                    "text":  "\n".join(lines[2:]).strip(),
                    "out":   "",
                })
    return blocks

def parse_vtt(text):
    blocks, lines = [], text.splitlines()
    i = idx = 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    while i < len(lines):
        if "-->" in lines[i]:
            tl = lines[i].strip()
            i += 1
            parts = []
            while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                parts.append(lines[i].strip())
                i += 1
            if parts:
                idx += 1
                blocks.append({"index": str(idx), "time": tl,
                               "text": "\n".join(parts), "out": ""})
        else:
            i += 1
    return blocks

def build_srt(blocks):
    return "\n\n".join(
        f"{b['index']}\n{b['time']}\n{b['out'] or b['text']}"
        for b in blocks
    ) + "\n"

def build_vtt(blocks):
    parts = ["WEBVTT", ""]
    for b in blocks:
        parts.append(f"{b['time']}\n{b['out'] or b['text']}")
    return "\n\n".join(parts)

# ──────────────────────────────────────────────────────────
#  PROGRESS DISPLAY
# ──────────────────────────────────────────────────────────
def make_progress(done, total, key_num, key_total):
    pct    = int(done / total * 100) if total else 0
    filled = pct // 5   # 20 blocks
    bar    = "█" * filled + "░" * (20 - filled)

    # Pie symbol
    pie_symbols = ["○", "◔", "◔", "◑", "◑", "◕", "◕", "●", "●"]
    pie = pie_symbols[min(int(pct / 100 * 8), 8)]

    return (
        f"\n"
        f"  ╭──────────────────────────╮\n"
        f"  │   {pie}  অনুবাদ হচ্ছে...  {pct:>3}%  │\n"
        f"  ╰──────────────────────────╯\n"
        f"\n"
        f"  [{bar}]\n"
        f"\n"
        f"  ✅ সম্পন্ন  :  {done} টি\n"
        f"  ⏳ বাকি    :  {total - done} টি\n"
        f"  📊 মোট     :  {total} টি\n"
        f"  🔑 API Key :  #{key_num} / {key_total}\n"
    )

# ──────────────────────────────────────────────────────────
#  TRANSLATION
# ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """তুমি একজন দক্ষ চলচ্চিত্র সাবটাইটেল অনুবাদক।

নিয়ম:
১. ভাব ও অনুভূতি বজায় রেখে বাংলায় অনুবাদ করো — আক্ষরিক অনুবাদ একদম নয়
২. চরিত্রের আবেগ, রসিকতা, রাগ, ভালোবাসা সব ধরে রাখো
৩. HTML ট্যাগ যেমন <i> <b> হুবহু রেখে দাও
৪. বাংলা বানান ও যুক্তবর্ণ সঠিকভাবে লেখো
৫. সংখ্যা ইংরেজিতেই রাখো

উত্তর শুধুমাত্র এই ফরম্যাটে দাও:
1. অনুবাদ
2. অনুবাদ
(কোনো বাড়তি কথা লিখবে না)"""


def _translate_sync(texts):
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    for attempt in range(km.total()):
        try:
            resp = km.current().chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": f"অনুবাদ করো:\n\n{numbered}"},
                ],
                temperature=0.2,
                max_tokens=3000,
            )
            raw    = resp.choices[0].message.content.strip()
            result = {}
            for line in raw.splitlines():
                m = re.match(r"^(\d+)\.\s*(.*)", line.strip())
                if m:
                    i = int(m.group(1)) - 1
                    if 0 <= i < len(texts):
                        result[i] = m.group(2).strip()
            return [result.get(i, texts[i]) for i in range(len(texts))]

        except Exception as e:
            err_name = type(e).__name__
            if "RateLimit" in err_name or "rate_limit" in str(e).lower():
                log.warning(f"Key #{km.current_num()} rate limit — পরের key তে যাচ্ছি")
                km.rotate()
            elif attempt < km.total() - 1:
                log.warning(f"Translation error ({err_name}), key rotate করছি")
                km.rotate()
            else:
                raise RuntimeError(f"সব API key ব্যর্থ: {e}")
    return texts


async def translate_async(texts):
    return await asyncio.to_thread(_translate_sync, texts)

# ──────────────────────────────────────────────────────────
#  CHANNEL CHECK
# ──────────────────────────────────────────────────────────
async def is_member(bot, user_id):
    if not CHANNEL_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except TelegramError as e:
        log.warning(f"Channel check error: {e}")
        return True

# ──────────────────────────────────────────────────────────
#  KEYBOARDS
# ──────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 ব্যবহার পদ্ধতি",  callback_data="help"),
         InlineKeyboardButton("ℹ️ বট সম্পর্কে",     callback_data="about")],
        [InlineKeyboardButton("🔑 API Key স্ট্যাটাস", callback_data="status")],
    ])

def kb_join():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 চ্যানেলে Join করুন", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Join করেছি — চেক করুন", callback_data="check_join")],
    ])

def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ অনুবাদ বাতিল করুন", callback_data="cancel")],
    ])

def kb_done():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 আরেকটি ফাইল অনুবাদ করুন", callback_data="home")],
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ মূল মেনু", callback_data="home")],
    ])

# ──────────────────────────────────────────────────────────
#  WELCOME TEXT
# ──────────────────────────────────────────────────────────
def welcome(name):
    return (
        f"🎬 *স্বাগতম, {name}!*\n\n"
        "আমি আপনার সাবটাইটেল ফাইল স্বাভাবিক বাংলায় অনুবাদ করে দিই।\n"
        "আক্ষরিক নয় — ভাব বুঝে ভাবানুবাদ!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📁 *ফরম্যাট :*  `.srt`  এবং  `.vtt`\n"
        "⏱ *টাইমকোড :*  সম্পূর্ণ অপরিবর্তিত\n"
        "🔑 *API Key :*  Multi-key rotation\n"
        "📊 *প্রগতি  :*  লাইভ পাই চার্ট\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "একটি সাবটাইটেল ফাইল পাঠিয়ে শুরু করুন 👇"
    )

# ──────────────────────────────────────────────────────────
#  HANDLERS
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            "🔒 *চ্যানেলে Join করুন*\n\nএই বট ব্যবহার করতে আমাদের চ্যানেলে Join করতে হবে।",
            parse_mode="Markdown", reply_markup=kb_join()
        )
        return
    await update.message.reply_text(
        welcome(user.first_name or "বন্ধু"),
        parse_mode="Markdown", reply_markup=kb_main()
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    uid  = q.from_user.id
    data = q.data
    await q.answer()

    if data == "home":
        await q.edit_message_text(
            welcome(q.from_user.first_name or "বন্ধু"),
            parse_mode="Markdown", reply_markup=kb_main()
        )

    elif data == "check_join":
        if await is_member(ctx.bot, uid):
            await q.edit_message_text(
                welcome(q.from_user.first_name or "বন্ধু"),
                parse_mode="Markdown", reply_markup=kb_main()
            )
        else:
            await q.answer("❌ এখনো Join করেননি!", show_alert=True)

    elif data == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার পদ্ধতি*\n\n"
            "১️⃣  `.srt` বা `.vtt` ফাইল পাঠান\n"
            "২️⃣  বট স্বয়ংক্রিয়ভাবে অনুবাদ শুরু করবে\n"
            "৩️⃣  লাইভ পাই চার্টে প্রগতি দেখুন\n"
            "৪️⃣  শেষে বাংলা ফাইল পাবেন\n\n"
            "💡 *টিপস:* UTF-8 ফাইল সবচেয়ে ভালো কাজ করে।",
            parse_mode="Markdown", reply_markup=kb_back()
        )

    elif data == "about":
        await q.edit_message_text(
            "ℹ️ *বট সম্পর্কে*\n\n"
            "🤖 *মডেল :* Llama 3.3 70B (Groq)\n"
            "🎯 *পদ্ধতি :* ভাবানুবাদ\n"
            "♻️ *Key :* Multi-key rotation\n"
            "🏓 *Uptime :* Self-ping\n\n"
            "Made with ❤️ for Bangla speakers",
            parse_mode="Markdown", reply_markup=kb_back()
        )

    elif data == "status":
        rows = "\n".join(
            f"  {'🟢' if i == km.idx else '⚪'} Key #{i+1}"
            for i in range(km.total())
        )
        await q.edit_message_text(
            f"🔑 *API Key স্ট্যাটাস*\n\n"
            f"মোট key: `{km.total()}` টি\n"
            f"সক্রিয়: `Key #{km.current_num()}`\n\n{rows}",
            parse_mode="Markdown", reply_markup=kb_back()
        )

    elif data == "cancel":
        if uid in user_states:
            user_states[uid]["cancelled"] = True
        await q.edit_message_text(
            "❌ *অনুবাদ বাতিল করা হয়েছে।*\n\nযেকোনো সময় নতুন ফাইল পাঠাতে পারেন।",
            parse_mode="Markdown", reply_markup=kb_main()
        )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            "🔒 চ্যানেলে Join করুন।",
            parse_mode="Markdown", reply_markup=kb_join()
        )
        return

    doc  = update.message.document
    fname = doc.file_name or "subtitle"

    if not (fname.lower().endswith(".srt") or fname.lower().endswith(".vtt")):
        await update.message.reply_text(
            "⚠️ শুধু `.srt` ও `.vtt` ফাইল সমর্থিত।",
            parse_mode="Markdown", reply_markup=kb_main()
        )
        return

    uid = user.id
    user_states[uid] = {"cancelled": False}

    msg = await update.message.reply_text(
        "⬇️ *ফাইল ডাউনলোড হচ্ছে...*",
        parse_mode="Markdown", reply_markup=kb_cancel()
    )

    try:
        tg_file    = await ctx.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        try:
            content = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = file_bytes.decode("latin-1")

        ftype  = "vtt" if fname.lower().endswith(".vtt") else "srt"
        blocks = parse_vtt(content) if ftype == "vtt" else parse_srt(content)

        if not blocks:
            await msg.edit_text(
                "❌ ফাইলটি পড়া যায়নি। সঠিক ফরম্যাটে আছে কিনা দেখুন।",
                reply_markup=kb_main()
            )
            return

        total = len(blocks)

        await msg.edit_text(
            f"🔄 *অনুবাদ শুরু হচ্ছে...*\n"
            f"{make_progress(0, total, km.current_num(), km.total())}",
            parse_mode="Markdown", reply_markup=kb_cancel()
        )

        last_edit = time.time()

        for i in range(0, total, BATCH_SIZE):
            if user_states.get(uid, {}).get("cancelled"):
                return

            batch  = blocks[i: i + BATCH_SIZE]
            texts  = [b["text"] for b in batch]
            result = await translate_async(texts)

            for j, t in enumerate(result):
                blocks[i + j]["out"] = t

            done = min(i + BATCH_SIZE, total)
            now  = time.time()

            if now - last_edit >= 2.5 or done == total:
                try:
                    await msg.edit_text(
                        f"🔄 *অনুবাদ চলছে...*\n"
                        f"{make_progress(done, total, km.current_num(), km.total())}",
                        parse_mode="Markdown", reply_markup=kb_cancel()
                    )
                    last_edit = now
                except Exception:
                    pass

        # Build output
        if ftype == "srt":
            out_content = build_srt(blocks)
            out_name    = re.sub(r"\.srt$", "_বাংলা.srt", fname, flags=re.IGNORECASE)
        else:
            out_content = build_vtt(blocks)
            out_name    = re.sub(r"\.vtt$", "_বাংলা.vtt", fname, flags=re.IGNORECASE)

        await msg.edit_text(
            f"✅ *অনুবাদ সম্পন্ন!*\n"
            f"{make_progress(total, total, km.current_num(), km.total())}",
            parse_mode="Markdown", reply_markup=kb_done()
        )

        await update.message.reply_document(
            document=io.BytesIO(out_content.encode("utf-8")),
            filename=out_name,
            caption=(
                f"🎉 *{out_name}*\n\n"
                f"✅ {total} টি সংলাপ অনুবাদ হয়েছে\n"
                f"⏱ টাইমকোড সম্পূর্ণ অপরিবর্তিত"
            ),
            parse_mode="Markdown",
        )

        await msg.edit_text(
            f"🎊 *সম্পন্ন হয়েছে!*\n\n"
            f"📁 `{out_name}`\n"
            f"📊 মোট: {total} টি সংলাপ\n\n"
            f"আরেকটি ফাইল পাঠাতে পারেন।",
            parse_mode="Markdown", reply_markup=kb_done()
        )

    except Exception as e:
        log.error("Error in on_document", exc_info=True)
        try:
            await msg.edit_text(
                f"❌ *সমস্যা হয়েছে:*\n`{str(e)[:200]}`\n\nআবার চেষ্টা করুন।",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except Exception:
            pass
    finally:
        user_states.pop(uid, None)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_member(ctx.bot, update.effective_user.id):
        await update.message.reply_text(
            "🔒 চ্যানেলে Join করুন।",
            reply_markup=kb_join()
        )
        return
    await update.message.reply_text(
        "📁 একটি `.srt` বা `.vtt` ফাইল পাঠান।",
        parse_mode="Markdown", reply_markup=kb_main()
    )


# ──────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────
def main():
    threading.Thread(target=_run_flask,  daemon=True).start()
    threading.Thread(target=_self_ping,  daemon=True).start()

    log.info(f"বট শুরু হচ্ছে | {km.total()} API key | Channel: {CHANNEL_ID or 'বন্ধ'}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
