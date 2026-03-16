import os
import logging
import re
import io
import threading
import time
import asyncio
import requests as req_lib
from flask import Flask, request, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── LOGGING ──────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT        = int(os.environ.get("PORT", 8080))
TOTAL_PARTS = 10

# ─── SESSION STORAGE ──────────────────────────────────────
# { user_id: { files:{1:bytes,...}, msg_id:int, chat_id:int, waiting_for:int|None } }
sessions: dict = {}

# ─── GLOBAL EVENT LOOP (shared) ───────────────────────────
loop: asyncio.AbstractEventLoop = None

# ─── FLASK APP ────────────────────────────────────────────
flask_app = Flask(__name__)

# ─── BUILD TELEGRAM APPLICATION ───────────────────────────
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)          # Webhook mode — no polling updater needed
    .build()
)

# ══════════════════════════════════════════════════════════
#  SRT PARSING & MERGE
# ══════════════════════════════════════════════════════════
def parse_srt(content: str) -> list[tuple[str, str]]:
    """Return list of (timing_line, subtitle_text) tuples."""
    content = content.strip().replace('\r\n', '\n').replace('\r', '\n')
    blocks  = re.split(r'\n{2,}', content)
    entries = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split('\n')
        if len(lines) < 3:
            continue
        # line 0 → index (must be integer)
        try:
            int(lines[0].strip())
        except ValueError:
            continue
        # line 1 → timing
        timing = lines[1].strip()
        if '-->' not in timing:
            continue
        # line 2+ → text
        text = '\n'.join(lines[2:]).strip()
        if text:
            entries.append((timing, text))

    return entries


def merge_srt_files(files_dict: dict) -> str:
    """Merge ordered SRT bytes dict → single SRT string."""
    all_entries: list[tuple[str, str]] = []

    for part_num in sorted(files_dict.keys()):
        raw = files_dict[part_num]
        for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
            try:
                content = raw.decode(enc)
                break
            except (UnicodeDecodeError, AttributeError):
                continue
        else:
            content = raw.decode('utf-8', errors='replace')

        all_entries.extend(parse_srt(content))

    # Re-number sequentially
    lines: list[str] = []
    for idx, (timing, text) in enumerate(all_entries, start=1):
        lines.append(str(idx))
        lines.append(timing)
        lines.append(text)
        lines.append('')

    return '\n'.join(lines).strip()


# ══════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════
def get_status_text(session: dict) -> str:
    files    = session.get("files", {})
    uploaded = len(files)
    waiting  = session.get("waiting_for")

    lines = [
        "🎬 *The Proposal — বাংলা Subtitle Merger*",
        "",
        f"📊 অগ্রগতি: `{uploaded}/{TOTAL_PARTS}` পার্ট আপলোড হয়েছে",
        "",
        "📁 *পার্টের অবস্থা:*",
    ]

    for i in range(1, TOTAL_PARTS + 1):
        if i in files:
            lines.append(f"  ✅ Part {i:02d}")
        elif waiting == i:
            lines.append(f"  📎 Part {i:02d}  ← ফাইল পাঠান")
        else:
            lines.append(f"  ⬜ Part {i:02d}")

    lines.append("")
    if uploaded == TOTAL_PARTS:
        lines.append("🎉 *সব পার্ট আপলোড হয়েছে!*")
        lines.append("নিচের বাটনে ক্লিক করে Merge করুন।")
    elif waiting:
        lines.append(f"📎 *Part {waiting}* এর `.srt` ফাইল এখন পাঠান।")
    else:
        remaining = TOTAL_PARTS - uploaded
        lines.append(f"⏳ আরো *{remaining}টি* পার্ট দরকার।")
        lines.append("নিচের বাটন থেকে পার্ট বেছে ফাইল পাঠান।")

    return '\n'.join(lines)


def get_keyboard(session: dict) -> InlineKeyboardMarkup:
    files    = session.get("files", {})
    waiting  = session.get("waiting_for")
    uploaded = len(files)

    buttons: list[list[InlineKeyboardButton]] = []

    # 2 buttons per row for parts
    row: list[InlineKeyboardButton] = []
    for i in range(1, TOTAL_PARTS + 1):
        if i in files:
            label = f"✅ Part {i}"
        elif waiting == i:
            label = f"📎 Part {i}…"
        else:
            label = f"⬜ Part {i}"
        row.append(InlineKeyboardButton(label, callback_data=f"sel_{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Action buttons
    action: list[InlineKeyboardButton] = []
    if uploaded == TOTAL_PARTS:
        action.append(InlineKeyboardButton("🔀 Merge করুন!", callback_data="merge"))
    if uploaded > 0:
        action.append(InlineKeyboardButton("🗑️ Reset", callback_data="reset"))
    if action:
        buttons.append(action)

    return InlineKeyboardMarkup(buttons)


# ══════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    sessions[user_id] = {
        "files": {}, "msg_id": None,
        "chat_id": chat_id, "waiting_for": None
    }

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=get_status_text(sessions[user_id]),
        parse_mode='Markdown',
        reply_markup=get_keyboard(sessions[user_id])
    )
    sessions[user_id]["msg_id"] = msg.message_id


# ══════════════════════════════════════════════════════════
#  CALLBACK (BUTTON) HANDLER
# ══════════════════════════════════════════════════════════
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    msg_id  = query.message.message_id

    # Ensure session exists
    if user_id not in sessions:
        sessions[user_id] = {
            "files": {}, "msg_id": msg_id,
            "chat_id": chat_id, "waiting_for": None
        }

    session = sessions[user_id]
    session["msg_id"]  = msg_id
    session["chat_id"] = chat_id
    data = query.data

    # ── Select a part ──────────────────────────────────────
    if data.startswith("sel_"):
        part_num = int(data.split("_")[1])
        session["waiting_for"] = part_num

        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=get_status_text(session),
            parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )

    # ── Merge ──────────────────────────────────────────────
    elif data == "merge":
        files = session.get("files", {})
        if len(files) != TOTAL_PARTS:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=get_status_text(session) + "\n\n❌ সব পার্ট আপলোড হয়নি!",
                parse_mode='Markdown',
                reply_markup=get_keyboard(session)
            )
            return

        # Show progress
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="⚙️ *Merge হচ্ছে…*\nঅনুগ্রহ করে একটু অপেক্ষা করুন।",
            parse_mode='Markdown'
        )

        try:
            merged      = merge_srt_files(files)
            entry_count = len(re.findall(r'^\d+$', merged, re.MULTILINE))
            buf         = io.BytesIO(merged.encode('utf-8'))
            buf.name    = "The_Proposal_2009_Bengali.srt"

            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(buf, filename="The_Proposal_2009_Bengali.srt"),
                caption=(
                    "✅ *Merge সম্পন্ন!*\n\n"
                    "🎬 The Proposal (2009) — বাংলা Subtitle\n"
                    f"📝 মোট *{entry_count}* টি subtitle entry\n\n"
                    "MX Player বা যেকোনো ভিডিও প্লেয়ারে ব্যবহার করুন। 🎉"
                ),
                parse_mode='Markdown'
            )

            # Reset & send fresh panel
            sessions[user_id] = {
                "files": {}, "msg_id": None,
                "chat_id": chat_id, "waiting_for": None
            }
            new_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_status_text(sessions[user_id]) + "\n\n🔄 নতুন করে শুরু করতে পারেন।",
                parse_mode='Markdown',
                reply_markup=get_keyboard(sessions[user_id])
            )
            sessions[user_id]["msg_id"] = new_msg.message_id

        except Exception as exc:
            logger.exception("Merge error")
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"❌ *Merge ব্যর্থ!*\n\n`{exc}`",
                parse_mode='Markdown',
                reply_markup=get_keyboard(session)
            )

    # ── Reset ──────────────────────────────────────────────
    elif data == "reset":
        sessions[user_id] = {
            "files": {}, "msg_id": msg_id,
            "chat_id": chat_id, "waiting_for": None
        }
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=get_status_text(sessions[user_id]) + "\n\n🔄 সব রিসেট হয়েছে।",
            parse_mode='Markdown',
            reply_markup=get_keyboard(sessions[user_id])
        )


# ══════════════════════════════════════════════════════════
#  FILE (DOCUMENT) HANDLER
# ══════════════════════════════════════════════════════════
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Delete the uploaded message to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass

    if user_id not in sessions or sessions[user_id].get("waiting_for") is None:
        return

    session  = sessions[user_id]
    part_num = session["waiting_for"]
    doc      = update.message.document

    # Validate .srt extension
    if not doc or not doc.file_name.lower().endswith('.srt'):
        txt = get_status_text(session) + f"\n\n❌ শুধু `.srt` ফাইল দিন!\nPart {part_num} এর ফাইল পাঠান।"
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=session["msg_id"],
            text=txt, parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )
        return

    # Download
    try:
        tg_file    = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        session["files"][part_num] = bytes(file_bytes)
        session["waiting_for"]     = None

        txt = get_status_text(session)
        if len(session["files"]) == TOTAL_PARTS:
            txt += "\n\n🎉 সব পার্ট প্রস্তুত! Merge করুন।"
        else:
            txt += f"\n\n✅ Part {part_num} সফলভাবে যোগ হয়েছে!"

        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=session["msg_id"],
            text=txt, parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )

    except Exception as exc:
        logger.exception("File download error")
        session["waiting_for"] = None
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=session["msg_id"],
            text=get_status_text(session) + "\n\n❌ ফাইল লোড সমস্যা। আবার চেষ্টা করুন।",
            parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )


# ══════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════
@flask_app.post(f"/webhook/{BOT_TOKEN}")
def webhook():
    data   = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run_coroutine_threadsafe(
        application.process_update(update), loop
    ).result(timeout=60)
    return jsonify({"ok": True})


@flask_app.get("/")
def index():
    return "✅ SRT Merger Bot is running!", 200


@flask_app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ══════════════════════════════════════════════════════════
#  KEEP ALIVE (prevents Render free-tier sleep)
# ══════════════════════════════════════════════════════════
def keep_alive():
    if not WEBHOOK_URL:
        return
    while True:
        time.sleep(14 * 60)          # every 14 minutes
        try:
            req_lib.get(f"{WEBHOOK_URL}/health", timeout=10)
            logger.info("Keep-alive ✓")
        except Exception as exc:
            logger.warning(f"Keep-alive failed: {exc}")


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    global loop

    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(cb_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, file_handler))

    # Create a persistent event loop in a background thread
    loop = asyncio.new_event_loop()

    def run_loop(lp: asyncio.AbstractEventLoop):
        asyncio.set_event_loop(lp)
        lp.run_forever()

    loop_thread = threading.Thread(target=run_loop, args=(loop,), daemon=True)
    loop_thread.start()

    # Initialize & set webhook
    async def setup():
        await application.initialize()
        webhook_path = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
        await application.bot.set_webhook(webhook_path)
        await application.start()
        logger.info(f"Webhook set → {webhook_path}")

    asyncio.run_coroutine_threadsafe(setup(), loop).result(timeout=30)

    # Start keep-alive thread
    threading.Thread(target=keep_alive, daemon=True).start()

    # Start Flask (blocking)
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
