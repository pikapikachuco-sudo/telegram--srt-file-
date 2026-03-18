"""
╔══════════════════════════════════════════════╗
║     🎬  বাংলা সাবটাইটেল অনুবাদ বট           ║
║     Multi-Key · Self-Ping · Channel Guard     ║
╚══════════════════════════════════════════════╝
"""

import os, re, io, logging, threading, asyncio, time, requests
from flask import Flask
from groq import Groq, RateLimitError
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberStatus
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.error import TelegramError

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  ENV CONFIG
# ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
RENDER_URL      = os.environ.get("RENDER_URL", "")          # e.g. https://subtitle-bot.onrender.com
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "")          # e.g. @mychannel  OR  -1001234567890
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/yourchannel")

# Multiple Groq keys — comma-separated in env
_raw_keys = os.environ.get("GROQ_API_KEYS", os.environ.get("GROQ_API_KEY", ""))
GROQ_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if not GROQ_KEYS:
    raise ValueError("কোনো GROQ_API_KEYS পাওয়া যায়নি!")

BATCH_SIZE = 10   # lines per API call

# ──────────────────────────────────────────────
#  MULTI-KEY MANAGER  (round-robin + fallback)
# ──────────────────────────────────────────────
class KeyManager:
    def __init__(self, keys: list[str]):
        self.keys    = keys
        self.idx     = 0
        self.lock    = threading.Lock()
        self.clients = [Groq(api_key=k) for k in keys]
        log.info(f"🔑 {len(keys)}টি API key লোড হয়েছে")

    def current(self) -> Groq:
        with self.lock:
            return self.clients[self.idx]

    def rotate(self) -> int:
        with self.lock:
            self.idx = (self.idx + 1) % len(self.keys)
            log.info(f"🔄 API key #{self.idx + 1} তে স্যুইচ করা হয়েছে")
            return self.idx

    def total(self) -> int:
        return len(self.keys)

    def current_num(self) -> int:
        return self.idx + 1

km = KeyManager(GROQ_KEYS)

# ──────────────────────────────────────────────
#  FLASK KEEP-ALIVE
# ──────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "🟢 বট চলছে!", 200

@flask_app.route("/ping")
def ping():
    return "🏓 pong", 200

def _run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def _self_ping_loop():
    """বট নিজেই নিজেকে প্রতি ১০ মিনিটে ping করে — Render free tier ঘুমায় না"""
    if not RENDER_URL:
        log.warning("RENDER_URL সেট নেই — self-ping বন্ধ।")
        return
    time.sleep(30)   # startup delay
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/ping", timeout=10)
            log.info(f"🏓 Self-ping: {r.status_code}")
        except Exception as e:
            log.warning(f"Self-ping ব্যর্থ: {e}")
        time.sleep(600)   # 10 minutes

# ──────────────────────────────────────────────
#  USER STATES
# ──────────────────────────────────────────────
user_states: dict[int, dict] = {}

# ──────────────────────────────────────────────
#  SUBTITLE PARSER
# ──────────────────────────────────────────────
def parse_srt(content: str) -> list[dict]:
    blocks, seen = [], set()
    for raw in re.split(r"\n\s*\n", content.strip()):
        lines = raw.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            key = lines[1].strip()
            if key not in seen:
                seen.add(key)
                blocks.append({
                    "index": lines[0].strip(),
                    "time":  lines[1].strip(),
                    "text":  "\n".join(lines[2:]).strip(),
                    "translated": "",
                })
    return blocks

def parse_vtt(content: str) -> list[dict]:
    blocks, lines = [], content.splitlines()
    i = idx = 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    while i < len(lines):
        if "-->" in lines[i]:
            time_line, i = lines[i].strip(), i + 1
            texts = []
            while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                texts.append(lines[i].strip()); i += 1
            if texts:
                idx += 1
                blocks.append({"index": str(idx), "time": time_line,
                                "text": "\n".join(texts), "translated": ""})
        else:
            i += 1
    return blocks

def build_srt(blocks: list[dict]) -> str:
    return "\n\n".join(
        f"{b['index']}\n{b['time']}\n{b['translated'] or b['text']}"
        for b in blocks
    ) + "\n"

def build_vtt(blocks: list[dict]) -> str:
    parts = ["WEBVTT", ""]
    for b in blocks:
        parts.append(f"{b['time']}\n{b['translated'] or b['text']}")
    return "\n\n".join(parts)

# ──────────────────────────────────────────────
#  PIE-CHART PROGRESS DISPLAY
# ──────────────────────────────────────────────
def pie_chart(pct: int) -> str:
    """Unicode পাই চার্ট — ৮টি ধাপে"""
    # Braille-style arc segments
    stages = [
        "○",   # 0%
        "◔",   # ~12%
        "◔",   # ~25%
        "◑",   # ~37%
        "◑",   # ~50%
        "◕",   # ~62%
        "◕",   # ~75%
        "●",   # ~87%
        "●",   # 100%
    ]
    idx = min(int(pct / 100 * 8), 8)
    return stages[idx]

def progress_block(done: int, total: int, key_num: int, key_total: int) -> str:
    pct    = int(done / total * 100) if total else 0
    filled = pct // 4          # 25 segments total
    empty  = 25 - filled
    bar    = "▓" * filled + "░" * empty
    pie    = pie_chart(pct)
    remain = total - done

    # "Pie chart" text art
    top    = "·" * 5
    mid    = f"  {pie}  {pct}%"

    lines = [
        f"",
        f"     ╭───────────────╮",
        f"     │  {pie}   {pct:>3}%  অনুবাদ  │",
        f"     ╰───────────────╯",
        f"",
        f"  ▓ [{bar}] ░",
        f"",
        f"  ✅ সম্পন্ন  : {done:>4} টি সংলাপ",
        f"  ⏳ বাকি    : {remain:>4} টি সংলাপ",
        f"  📊 মোট     : {total:>4} টি সংলাপ",
        f"  🔑 API Key : #{key_num} / {key_total}",
    ]
    return "\n".join(lines)

# ──────────────────────────────────────────────
#  TRANSLATION  (sync)
# ──────────────────────────────────────────────
_SYSTEM = """তুমি একজন দক্ষ চলচ্চিত্র সাবটাইটেল অনুবাদক।

তোমার কাজ:
• প্রতিটি সংলাপের **ভাব ও অনুভূতি** বজায় রেখে বাংলায় অনুবাদ করো
• আক্ষরিক অনুবাদ একদম নয় — বাংলা ভাষায় স্বাভাবিকভাবে যেভাবে বলা হয় সেভাবে লেখো
• চরিত্রের আবেগ, রসিকতা, রাগ, ভালোবাসা — সবকিছু ধরে রাখো
• HTML ট্যাগ যেমন <i>, <b>, <font> হুবহু রেখে দাও
• বাংলা লেখায় সঠিক বানান ও যুক্তবর্ণ ব্যবহার করো
• সংখ্যা বাংলায় লেখার দরকার নেই (১, ২ না লিখে 1, 2 রেখে দাও)

উত্তরের ফরম্যাট: শুধু "ক্রমনম্বর. অনুবাদ" — অন্য কোনো কথা লিখবে না"""

def _do_translate(client: Groq, texts: list[str]) -> list[str]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": f"নিচের সংলাপগুলো বাংলায় অনুবাদ করো:\n\n{numbered}"},
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

def translate_batch(texts: list[str]) -> list[str]:
    """Rotate keys on rate-limit error"""
    for attempt in range(km.total()):
        try:
            return _do_translate(km.current(), texts)
        except RateLimitError:
            log.warning(f"⚠️  Key #{km.current_num()} rate limit — পরের key তে যাচ্ছি")
            km.rotate()
        except Exception as e:
            log.error(f"Translation error: {e}")
            if attempt < km.total() - 1:
                km.rotate()
            else:
                raise
    raise RuntimeError("সমস্ত API Key exhausted হয়েছে।")

async def translate_async(texts: list[str]) -> list[str]:
    return await asyncio.to_thread(translate_batch, texts)

# ──────────────────────────────────────────────
#  CHANNEL MEMBERSHIP CHECK
# ──────────────────────────────────────────────
async def is_member(bot, user_id: int) -> bool:
    if not CHANNEL_ID:
        return True          # Channel check বন্ধ থাকলে সবাইকে allow
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return m.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as e:
        log.warning(f"Channel check error: {e}")
        return True          # Error হলে block না করে allow করো

# ──────────────────────────────────────────────
#  KEYBOARDS
# ──────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 কীভাবে ব্যবহার করবেন", callback_data="help"),
        ],
        [
            InlineKeyboardButton("ℹ️ বট সম্পর্কে",  callback_data="about"),
            InlineKeyboardButton("🔑 API স্ট্যাটাস", callback_data="status"),
        ],
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
        [InlineKeyboardButton("⬅️  মূল মেনুতে ফিরুন", callback_data="home")],
    ])

# ──────────────────────────────────────────────
#  TEXT TEMPLATES
# ──────────────────────────────────────────────
def welcome_text(name: str) -> str:
    return (
        f"🎬 *স্বাগতম, {name}!*\n\n"
        "আমি আপনার সাবটাইটেল ফাইল স্বাভাবিক বাংলায় অনুবাদ করে দিই —\n"
        "আক্ষরিক নয়, একদম ভাব বুঝে ভাবানুবাদ!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📁  *সমর্থিত ফরম্যাট:*  `.srt`  ও  `.vtt`\n"
        "⏱  *টাইমকোড:*  অপরিবর্তিত থাকবে\n"
        "🔑  *API:*  একাধিক key — স্বয়ংক্রিয় rotation\n"
        "📊  *লাইভ প্রগতি:*  পাই চার্টসহ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "একটি সাবটাইটেল ফাইল পাঠিয়ে শুরু করুন 👇"
    )

JOIN_REQUIRED = (
    "🔒 *চ্যানেল সদস্যতা প্রয়োজন*\n\n"
    "এই বট ব্যবহার করতে হলে আমাদের চ্যানেলে Join করতে হবে।\n\n"
    "নিচের বাটনে ক্লিক করে Join করুন, তারপর ✅ বাটনে চাপুন।"
)

# ──────────────────────────────────────────────
#  HANDLERS
# ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "বন্ধু"

    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            JOIN_REQUIRED, parse_mode="Markdown", reply_markup=kb_join()
        )
        return

    await update.message.reply_text(
        welcome_text(name), parse_mode="Markdown", reply_markup=kb_main()
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    user = q.from_user
    await q.answer()

    # ── check_join ──
    if data == "check_join":
        if await is_member(ctx.bot, user.id):
            await q.edit_message_text(
                welcome_text(user.first_name or "বন্ধু"),
                parse_mode="Markdown", reply_markup=kb_main()
            )
        else:
            await q.answer("❌ আপনি এখনো Join করেননি!", show_alert=True)
        return

    # ── home ──
    if data == "home":
        await q.edit_message_text(
            welcome_text(user.first_name or "বন্ধু"),
            parse_mode="Markdown", reply_markup=kb_main()
        )

    # ── help ──
    elif data == "help":
        txt = (
            "📖 *ব্যবহার পদ্ধতি*\n\n"
            "১️⃣  `.srt` বা `.vtt` ফাইল পাঠান\n"
            "২️⃣  বট স্বয়ংক্রিয়ভাবে অনুবাদ শুরু করবে\n"
            "৩️⃣  লাইভ পাই চার্টে প্রগতি দেখুন\n"
            "৪️⃣  শেষে বাংলা সাবটাইটেল ফাইল পাবেন\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 *টিপস:*\n"
            "• ফাইল UTF-8 এনকোডেড হলে সেরা ফলাফল\n"
            "• একটি অনুবাদ চলাকালে বাতিল করা যাবে\n"
            "• বড় ফাইলে একটু বেশি সময় লাগবে\n"
            "• একাধিক API key থাকায় limit-এ আটকাবে না"
        )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb_back())

    # ── about ──
    elif data == "about":
        txt = (
            "ℹ️ *বট সম্পর্কে*\n\n"
            "🤖 *AI মডেল:*  Llama 3.3 70B (Groq)\n"
            "🌐 *অনুবাদ:*  যেকোনো ভাষা → বাংলা\n"
            "🎯 *পদ্ধতি:*  ভাবানুবাদ — আক্ষরিক নয়\n"
            "⚡ *স্পিড:*   অত্যন্ত দ্রুত\n"
            "♻️ *Key:*    Multi-key rotation\n"
            "🏓 *Uptime:*  Self-ping (Render free)\n\n"
            "Made with ❤️ for Bangla speakers"
        )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb_back())

    # ── status ──
    elif data == "status":
        txt = (
            f"🔑 *API Key স্ট্যাটাস*\n\n"
            f"মোট key: `{km.total()}` টি\n"
            f"বর্তমান সক্রিয়: `Key #{km.current_num()}`\n\n"
            + "\n".join(
                f"  {'🟢' if i == km.idx else '⚪'} Key #{i+1}"
                for i in range(km.total())
            )
        )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb_back())

    # ── cancel ──
    elif data == "cancel":
        uid = q.from_user.id
        if uid in user_states:
            user_states[uid]["cancelled"] = True
        await q.edit_message_text(
            "❌ *অনুবাদ বাতিল করা হয়েছে।*\n\nযেকোনো সময় নতুন ফাইল পাঠাতে পারেন।",
            parse_mode="Markdown", reply_markup=kb_main()
        )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # ── Channel guard ──
    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            JOIN_REQUIRED, parse_mode="Markdown", reply_markup=kb_join()
        )
        return

    doc      = update.message.document
    filename = doc.file_name or "subtitle"

    if not (filename.lower().endswith(".srt") or filename.lower().endswith(".vtt")):
        await update.message.reply_text(
            "⚠️ *শুধু `.srt` ও `.vtt` ফাইল সমর্থিত।*\n"
            "অন্য ফরম্যাট হলে আগে কনভার্ট করুন।",
            parse_mode="Markdown", reply_markup=kb_main()
        )
        return

    uid = user.id
    user_states[uid] = {"cancelled": False}

    # ── Status message ──
    msg = await update.message.reply_text(
        "⬇️ *ফাইল ডাউনলোড হচ্ছে...*",
        parse_mode="Markdown", reply_markup=kb_cancel()
    )

    try:
        tg_file    = await ctx.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        # Try UTF-8, fallback to latin-1
        try:
            content = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = file_bytes.decode("latin-1")

        ftype  = "vtt" if filename.lower().endswith(".vtt") else "srt"
        blocks = parse_vtt(content) if ftype == "vtt" else parse_srt(content)

        if not blocks:
            await msg.edit_text(
                "❌ *ফাইলটি পড়া সম্ভব হয়নি।*\n\nসঠিক ফরম্যাটে আছে কিনা দেখুন।",
                parse_mode="Markdown", reply_markup=kb_main()
            )
            return

        total = len(blocks)
        await msg.edit_text(
            f"🎬 *অনুবাদ প্রস্তুতি হচ্ছে...*\n"
            f"{progress_block(0, total, km.current_num(), km.total())}",
            parse_mode="Markdown", reply_markup=kb_cancel()
        )

        # ── Batch translate ──
        last_edit = time.time()
        for i in range(0, total, BATCH_SIZE):
            if user_states.get(uid, {}).get("cancelled"):
                return

            batch  = blocks[i: i + BATCH_SIZE]
            texts  = [b["text"] for b in batch]
            result = await translate_async(texts)

            for j, t in enumerate(result):
                blocks[i + j]["translated"] = t

            done = min(i + BATCH_SIZE, total)

            # Throttle edits (max once every 2s to avoid Telegram flood)
            now = time.time()
            if now - last_edit >= 2.0 or done == total:
                try:
                    await msg.edit_text(
                        f"🔄 *অনুবাদ চলছে...*\n"
                        f"{progress_block(done, total, km.current_num(), km.total())}",
                        parse_mode="Markdown", reply_markup=kb_cancel()
                    )
                    last_edit = now
                except Exception:
                    pass

        # ── Build output file ──
        if ftype == "srt":
            out = build_srt(blocks)
            out_name = re.sub(r"\.srt$", "_বাংলা.srt", filename, flags=re.IGNORECASE)
        else:
            out = build_vtt(blocks)
            out_name = re.sub(r"\.vtt$", "_বাংলা.vtt", filename, flags=re.IGNORECASE)

        # ── Final status ──
        await msg.edit_text(
            f"✅ *অনুবাদ সম্পন্ন!*\n"
            f"{progress_block(total, total, km.current_num(), km.total())}\n\n"
            f"📥 ফাইল পাঠানো হচ্ছে...",
            parse_mode="Markdown", reply_markup=kb_done()
        )

        await update.message.reply_document(
            document=io.BytesIO(out.encode("utf-8")),
            filename=out_name,
            caption=(
                f"🎉 *{out_name}*\n\n"
                f"✅ {total} টি সংলাপ অনুবাদ হয়েছে\n"
                f"⏱ টাইমকোড সম্পূর্ণ অপরিবর্তিত"
            ),
            parse_mode="Markdown",
        )

        await msg.edit_text(
            f"🎊 *সব ঠিকঠাক সম্পন্ন!*\n\n"
            f"📁 ফাইল: `{out_name}`\n"
            f"📊 মোট সংলাপ: {total} টি\n\n"
            f"আরেকটি ফাইল পাঠাতে পারেন।",
            parse_mode="Markdown", reply_markup=kb_done()
        )

    except Exception as e:
        log.error("Document processing error", exc_info=True)
        try:
            await msg.edit_text(
                f"❌ *সমস্যা হয়েছে:*\n`{str(e)[:250]}`\n\nআবার চেষ্টা করুন।",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except Exception:
            pass
    finally:
        user_states.pop(uid, None)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Non-command, non-document text — just show menu"""
    if not await is_member(ctx.bot, update.effective_user.id):
        await update.message.reply_text(
            JOIN_REQUIRED, parse_mode="Markdown", reply_markup=kb_join()
        )
        return
    await update.message.reply_text(
        "📁 একটি `.srt` বা `.vtt` সাবটাইটেল ফাইল পাঠান।",
        parse_mode="Markdown", reply_markup=kb_main()
    )

# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────
def main():
    # Flask health server
    threading.Thread(target=_run_flask,      daemon=True).start()
    # Self-ping loop
    threading.Thread(target=_self_ping_loop, daemon=True).start()

    log.info(f"🤖 বট শুরু হচ্ছে | {km.total()} API key | Channel: {CHANNEL_ID or 'বন্ধ'}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
