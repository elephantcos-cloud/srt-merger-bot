import os
import logging
import re
import io
import threading
import time
import requests
from flask import Flask, request, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")   # e.g. https://your-app.onrender.com
PORT = int(os.environ.get("PORT", 8080))
TOTAL_PARTS = 10

# ─── USER SESSION STORAGE ─────────────────────────────────
# { user_id: { "files": {1: bytes, 2: bytes, ...}, "msg_id": int, "chat_id": int } }
sessions = {}

# ─── FLASK APP ────────────────────────────────────────────
flask_app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# ─── SRT MERGE FUNCTION ───────────────────────────────────
def parse_srt(content: str) -> list:
    """Parse SRT content into list of (timing, text) tuples."""
    content = content.strip()
    # Normalize line endings
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    
    blocks = re.split(r'\n\n+', content)
    entries = []
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split('\n')
        if len(lines) < 3:
            continue
        
        # First line should be index number
        try:
            int(lines[0].strip())
        except ValueError:
            continue
        
        # Second line should be timing
        timing = lines[1].strip()
        if '-->' not in timing:
            continue
        
        # Rest is text
        text = '\n'.join(lines[2:]).strip()
        if text:
            entries.append((timing, text))
    
    return entries


def merge_srt_files(files_dict: dict) -> str:
    """Merge multiple SRT files into one, re-numbering entries."""
    all_entries = []
    
    for part_num in sorted(files_dict.keys()):
        raw = files_dict[part_num]
        try:
            content = raw.decode('utf-8-sig')  # handles BOM
        except UnicodeDecodeError:
            try:
                content = raw.decode('latin-1')
            except Exception:
                content = raw.decode('utf-8', errors='replace')
        
        entries = parse_srt(content)
        all_entries.extend(entries)
    
    # Build merged SRT with sequential numbering
    output_lines = []
    for idx, (timing, text) in enumerate(all_entries, start=1):
        output_lines.append(str(idx))
        output_lines.append(timing)
        output_lines.append(text)
        output_lines.append('')  # blank line between blocks
    
    return '\n'.join(output_lines).strip()


# ─── UI HELPERS ───────────────────────────────────────────
def get_status_text(session: dict) -> str:
    files = session.get("files", {})
    uploaded = len(files)
    
    lines = ["🎬 *The Proposal — বাংলা Subtitle Merger*", ""]
    lines.append(f"📊 অগ্রগতি: `{uploaded}/{TOTAL_PARTS}` পার্ট আপলোড হয়েছে")
    lines.append("")
    lines.append("📁 *পার্টের অবস্থা:*")
    
    for i in range(1, TOTAL_PARTS + 1):
        if i in files:
            lines.append(f"  ✅ Part {i:02d}")
        else:
            lines.append(f"  ⬜ Part {i:02d}")
    
    lines.append("")
    if uploaded == TOTAL_PARTS:
        lines.append("🎉 *সব পার্ট আপলোড হয়েছে!*")
        lines.append("নিচের বাটনে ক্লিক করে Merge করুন।")
    else:
        remaining = TOTAL_PARTS - uploaded
        lines.append(f"⏳ আরো *{remaining}টি* পার্ট দরকার।")
        lines.append("নিচের বাটন থেকে পার্ট বেছে ফাইল পাঠান।")
    
    return '\n'.join(lines)


def get_keyboard(session: dict) -> InlineKeyboardMarkup:
    files = session.get("files", {})
    waiting_for = session.get("waiting_for", None)
    uploaded = len(files)
    
    buttons = []
    
    # Part selection buttons (2 per row)
    row = []
    for i in range(1, TOTAL_PARTS + 1):
        if i in files:
            label = f"✅ Part {i}"
        elif waiting_for == i:
            label = f"📎 Part {i} ..."
        else:
            label = f"⬜ Part {i}"
        
        row.append(InlineKeyboardButton(label, callback_data=f"select_{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    # Action buttons
    action_row = []
    
    if uploaded == TOTAL_PARTS:
        action_row.append(
            InlineKeyboardButton("🔀 Merge করুন!", callback_data="merge")
        )
    
    if uploaded > 0:
        action_row.append(
            InlineKeyboardButton("🗑️ Reset", callback_data="reset")
        )
    
    if action_row:
        buttons.append(action_row)
    
    return InlineKeyboardMarkup(buttons)


# ─── HANDLERS ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    sessions[user_id] = {"files": {}, "msg_id": None, "chat_id": chat_id, "waiting_for": None}
    
    text = get_status_text(sessions[user_id])
    keyboard = get_keyboard(sessions[user_id])
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )
    sessions[user_id]["msg_id"] = msg.message_id


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    # Init session if needed
    if user_id not in sessions:
        sessions[user_id] = {
            "files": {}, "msg_id": query.message.message_id,
            "chat_id": chat_id, "waiting_for": None
        }
    
    session = sessions[user_id]
    session["msg_id"] = query.message.message_id
    session["chat_id"] = chat_id
    data = query.data
    
    if data.startswith("select_"):
        part_num = int(data.split("_")[1])
        session["waiting_for"] = part_num
        
        text = get_status_text(session)
        text += f"\n\n📎 *Part {part_num}* এর ফাইল পাঠান:"
        keyboard = get_keyboard(session)
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=session["msg_id"],
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    elif data == "merge":
        files = session.get("files", {})
        if len(files) != TOTAL_PARTS:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=session["msg_id"],
                text=get_status_text(session) + "\n\n❌ সব পার্ট আপলোড হয়নি!",
                parse_mode='Markdown',
                reply_markup=get_keyboard(session)
            )
            return
        
        # Show merging status
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=session["msg_id"],
            text="⚙️ *Merge হচ্ছে...*\n\nঅনুগ্রহ করে একটু অপেক্ষা করুন।",
            parse_mode='Markdown'
        )
        
        try:
            merged = merge_srt_files(files)
            merged_bytes = merged.encode('utf-8')
            srt_file = io.BytesIO(merged_bytes)
            srt_file.name = "The_Proposal_2009_Bengali.srt"
            
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(srt_file, filename="The_Proposal_2009_Bengali.srt"),
                caption=(
                    "✅ *Merge সম্পন্ন!*\n\n"
                    "🎬 The Proposal (2009) — বাংলা Subtitle\n"
                    f"📝 ফাইলে মোট {len(re.findall(r'^\\d+$', merged, re.MULTILINE))} টি entry আছে।\n\n"
                    "MX Player বা যেকোনো ভিডিও প্লেয়ারে ব্যবহার করুন। 🎉"
                ),
                parse_mode='Markdown'
            )
            
            # Reset session
            sessions[user_id] = {
                "files": {}, "msg_id": None,
                "chat_id": chat_id, "waiting_for": None
            }
            
            # Send new control panel
            new_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_status_text(sessions[user_id]) + "\n\n🔄 নতুন করে শুরু করতে পারেন।",
                parse_mode='Markdown',
                reply_markup=get_keyboard(sessions[user_id])
            )
            sessions[user_id]["msg_id"] = new_msg.message_id
            
        except Exception as e:
            logger.error(f"Merge error: {e}")
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=session["msg_id"],
                text=f"❌ *Merge ব্যর্থ হয়েছে!*\n\nError: `{str(e)}`",
                parse_mode='Markdown',
                reply_markup=get_keyboard(session)
            )
    
    elif data == "reset":
        sessions[user_id] = {
            "files": {}, "msg_id": session["msg_id"],
            "chat_id": chat_id, "waiting_for": None
        }
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=sessions[user_id]["msg_id"],
            text=get_status_text(sessions[user_id]) + "\n\n🔄 সব রিসেট হয়েছে।",
            parse_mode='Markdown',
            reply_markup=get_keyboard(sessions[user_id])
        )


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id not in sessions or sessions[user_id].get("waiting_for") is None:
        await update.message.delete()
        return
    
    session = sessions[user_id]
    part_num = session["waiting_for"]
    
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.srt'):
        # Delete the message and notify
        await update.message.delete()
        text = get_status_text(session)
        text += f"\n\n❌ শুধুমাত্র `.srt` ফাইল গ্রহণযোগ্য!\nPart {part_num} এর ফাইল পাঠান।"
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=session["msg_id"],
            text=text,
            parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )
        return
    
    # Delete the user's file message to keep chat clean
    await update.message.delete()
    
    # Download file
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        
        session["files"][part_num] = bytes(file_bytes)
        session["waiting_for"] = None
        
        text = get_status_text(session)
        if len(session["files"]) == TOTAL_PARTS:
            text += "\n\n🎉 সব পার্ট প্রস্তুত! Merge করুন।"
        else:
            text += f"\n\n✅ Part {part_num} সফলভাবে যোগ হয়েছে!"
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=session["msg_id"],
            text=text,
            parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )
    except Exception as e:
        logger.error(f"File download error: {e}")
        session["waiting_for"] = None
        text = get_status_text(session)
        text += f"\n\n❌ ফাইল লোড করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।"
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=session["msg_id"],
            text=text,
            parse_mode='Markdown',
            reply_markup=get_keyboard(session)
        )


# ─── FLASK WEBHOOK ROUTE ──────────────────────────────────
@flask_app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    import asyncio
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run(application.process_update(update))
    return jsonify({"ok": True})


@flask_app.route("/", methods=["GET"])
def index():
    return "✅ SRT Merger Bot is running!", 200


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ─── KEEP ALIVE (prevents Render free tier sleep) ─────────
def keep_alive():
    """Ping self every 14 minutes to prevent Render from sleeping."""
    if not WEBHOOK_URL:
        return
    while True:
        time.sleep(14 * 60)
        try:
            requests.get(f"{WEBHOOK_URL}/health", timeout=10)
            logger.info("Keep-alive ping sent.")
        except Exception as e:
            logger.warning(f"Keep-alive failed: {e}")


# ─── SETUP & RUN ──────────────────────────────────────────
def setup_handlers():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, file_handler))


async def set_webhook():
    webhook_path = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
    await application.bot.set_webhook(webhook_path)
    logger.info(f"Webhook set: {webhook_path}")


if __name__ == "__main__":
    import asyncio
    
    setup_handlers()
    
    # Initialize application
    asyncio.run(application.initialize())
    
    # Set webhook
    asyncio.run(set_webhook())
    
    # Start keep-alive thread
    if WEBHOOK_URL:
        t = threading.Thread(target=keep_alive, daemon=True)
        t.start()
        logger.info("Keep-alive thread started.")
    
    # Run Flask
    flask_app.run(host="0.0.0.0", port=PORT)
