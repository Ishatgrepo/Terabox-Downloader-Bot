#!/usr/bin/env python
# pylint: disable=logging-fstring-interpolation, disable=broad-except, disable=invalid-name
import asyncio
import httpx # Retained for other potential uses, though not primary download
import os
import re
import logging
import time
from urllib.parse import urlparse, quote 
# Removed: from requests import post, get, RequestException 
from collections import deque
from datetime import datetime, timedelta # Added timedelta

# Aria2c integration
from aria2p import API as Aria2API, Client as Aria2Client, Download as Aria2Download

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup 
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler 
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest # For handling message not modified

# === Configuration ===
BOT_TOKEN = "7893919705:AAE9b6jpHFdxzQQIucrNMEvje2u7N8uL15o"
DUMP_CHANNEL_ID_STR = "-1002281669966" 
FORCE_SUB_CHANNEL_ID_STR = None 
ADMIN_USER_IDS = [6469067345] 

# Aria2c Configuration
ARIA2_RPC_HOST = "http://localhost"
ARIA2_RPC_PORT = 6800
ARIA2_RPC_SECRET = "" # Add secret if your aria2c RPC is secured

DUMP_CHANNEL_ID = None
FORCE_SUB_CHANNEL_ID = None 

# Initialize Aria2 API client
try:
    aria2_client = Aria2Client(host=ARIA2_RPC_HOST, port=ARIA2_RPC_PORT, secret=ARIA2_RPC_SECRET)
    aria2 = Aria2API(aria2_client)
    # Set global options for aria2c
    aria2_options_dict = {
        "max-tries": "50", "retry-wait": "3", "continue": "true", "allow-overwrite": "true",
        "min-split-size": "4M", "split": "10", "max-connection-per-server": "10",
        "max-concurrent-downloads": "5", # Adjust as needed
        # "dir": "./aria2_downloads" # Optional: specify download directory for aria2c
    }
    aria2.set_global_options(aria2_options_dict)
    logging.info("Aria2c client initialized and global options set.")
except Exception as e:
    logging.error(f"Failed to connect to Aria2c or set options: {e}. Ensure aria2c is running and configured correctly.")
    aria2 = None # Set to None if connection fails

def _initialize_config():
    global DUMP_CHANNEL_ID, FORCE_SUB_CHANNEL_ID
    if DUMP_CHANNEL_ID_STR and DUMP_CHANNEL_ID_STR != "YOUR_DUMP_CHANNEL_ID_HERE":
        try:
            DUMP_CHANNEL_ID = int(DUMP_CHANNEL_ID_STR)
            logger.info(f"Initial DUMP_CHANNEL_ID set to: {DUMP_CHANNEL_ID}")
        except ValueError:
            logging.error(f"Invalid initial DUMP_CHANNEL_ID_STR: {DUMP_CHANNEL_ID_STR}. Must be an integer.")
            DUMP_CHANNEL_ID = None
    else:
         logging.info("Initial DUMP_CHANNEL_ID_STR not set or is placeholder.")
         DUMP_CHANNEL_ID = None

    if FORCE_SUB_CHANNEL_ID_STR and FORCE_SUB_CHANNEL_ID_STR.lower() not in ['none', 'clear', '']:
        try:
            FORCE_SUB_CHANNEL_ID = int(FORCE_SUB_CHANNEL_ID_STR)
            logger.info(f"Initial FORCE_SUB_CHANNEL_ID set to ID: {FORCE_SUB_CHANNEL_ID}")
        except ValueError:
            FORCE_SUB_CHANNEL_ID = FORCE_SUB_CHANNEL_ID_STR
            logger.info(f"Initial FORCE_SUB_CHANNEL_ID set to Username/String: {FORCE_SUB_CHANNEL_ID}")
    else:
        logger.info("Initial FORCE_SUB_CHANNEL_ID_STR not set or is 'none'/'clear'. Force subscription disabled.")
        FORCE_SUB_CHANNEL_ID = None

# === Logging Setup ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING) 
logging.getLogger("aria2p").setLevel(logging.WARNING) # Silence aria2p info logs too

MAX_LOG_ENTRIES = 200 
log_buffer = deque(maxlen=MAX_LOG_ENTRIES)

class MemoryLogHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    def emit(self, record):
        log_entry = self.format(record)
        log_buffer.append(log_entry)

memory_handler = MemoryLogHandler()
memory_handler.setLevel(logging.INFO) 
logging.getLogger().addHandler(memory_handler) 

class DirectDownloadLinkException(Exception): # Renamed for clarity
    pass
class Aria2cError(Exception):
    pass


# === Helper Functions ===
def get_readable_file_size(size_in_bytes: int) -> str:
    if not isinstance(size_in_bytes, (int, float)) or size_in_bytes < 0: return "N/A"
    if size_in_bytes == 0: return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_in_bytes)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"

def format_timedelta_to_eta(td: timedelta) -> str:
    if td is None: return "N/A"
    total_seconds = int(td.total_seconds())
    if total_seconds < 0: return "N/A" # Should not happen for ETA
    
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    if seconds > 0 or not parts : parts.append(f"{seconds}s") # Show seconds if it's the only unit or non-zero
    
    return " ".join(parts) if parts else "0s"


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global FORCE_SUB_CHANNEL_ID
    if not FORCE_SUB_CHANNEL_ID: return True
    if not update.effective_user: return False 
    user_id = update.effective_user.id
    try:
        member_status = await context.bot.get_chat_member(chat_id=FORCE_SUB_CHANNEL_ID, user_id=user_id)
        if member_status.status not in ['member', 'administrator', 'creator']:
            fsub_display = f"@{FORCE_SUB_CHANNEL_ID}" if isinstance(FORCE_SUB_CHANNEL_ID, str) and not FORCE_SUB_CHANNEL_ID.startswith('@') else FORCE_SUB_CHANNEL_ID
            await update.message.reply_text(
                f"Hello {update.effective_user.first_name}!\n"
                f"To use this bot, you need to subscribe to our channel: {fsub_display}\n"
                "After subscribing, please try sending the link again.",
                disable_web_page_preview=True
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking subscription for {user_id} in {FORCE_SUB_CHANNEL_ID}: {e}")
        if "user not found" in str(e).lower() or "chat not found" in str(e).lower() or "member not found" in str(e).lower():
             await update.message.reply_text(
                f"Could not verify channel subscription. Please ensure the channel is correctly set by the admin and the bot is an admin there if it's private."
             )
        else:
            await update.message.reply_text("Could not verify channel subscription at the moment. Please try again later.")
        return False

# === Admin Commands ===
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if not log_buffer:
        await update.message.reply_text("Log buffer is empty.")
        return
    log_content = "\n".join(log_buffer)
    if len(log_content) > 4000: 
        try:
            with open("bot_logs.txt", "w", encoding="utf-8") as f: f.write(log_content)
            await update.message.reply_document(document=open("bot_logs.txt", "rb"), filename="bot_logs.txt")
            os.remove("bot_logs.txt")
        except Exception as e:
            logger.error(f"Failed to send logs as file: {e}")
            await update.message.reply_text("Failed to send logs as a file. Try again later.")
    else:
        await update.message.reply_text(f"<pre>{log_content}</pre>", parse_mode=ParseMode.HTML)

async def set_dump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DUMP_CHANNEL_ID
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setdump <channel_id> (e.g., -100xxxxxxxxxx) or `none` to clear.")
        return
    new_dump_id_str = context.args[0]
    if new_dump_id_str.lower() in ['none', 'clear']:
        DUMP_CHANNEL_ID = None
        await update.message.reply_text("Dump channel has been cleared. Files will be sent to the user directly.")
        logger.info(f"Admin {update.effective_user.id} cleared DUMP_CHANNEL_ID.")
        return
    try:
        new_dump_id = int(new_dump_id_str)
        if not (new_dump_id < -1000000000000):
             await update.message.reply_text("Invalid channel ID format. It should be a large negative number (e.g., -100xxxxxxxxxx).")
             return
        DUMP_CHANNEL_ID = new_dump_id
        await update.message.reply_text(f"Dump channel ID set to: {DUMP_CHANNEL_ID}")
        logger.info(f"Admin {update.effective_user.id} set DUMP_CHANNEL_ID to {DUMP_CHANNEL_ID}.")
    except ValueError:
        await update.message.reply_text("Invalid channel ID. Please provide a valid integer ID (e.g., -100xxxxxxxxxx).")

async def set_fsub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global FORCE_SUB_CHANNEL_ID
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setfsub <@channel_username or channel_id> or `none` to disable.")
        return
    new_fsub_target = context.args[0]
    if new_fsub_target.lower() in ['none', 'clear']:
        FORCE_SUB_CHANNEL_ID = None
        await update.message.reply_text("Force subscription has been disabled.")
        logger.info(f"Admin {update.effective_user.id} disabled FORCE_SUB_CHANNEL_ID.")
    else:
        try:
            FORCE_SUB_CHANNEL_ID = int(new_fsub_target)
            logger.info(f"Admin {update.effective_user.id} set FORCE_SUB_CHANNEL_ID to ID: {FORCE_SUB_CHANNEL_ID}.")
        except ValueError:
            FORCE_SUB_CHANNEL_ID = new_fsub_target if new_fsub_target.startswith('@') else "@" + new_fsub_target
            logger.info(f"Admin {update.effective_user.id} set FORCE_SUB_CHANNEL_ID to Username: {FORCE_SUB_CHANNEL_ID}.")
        await update.message.reply_text(f"Force subscribe channel set to: {FORCE_SUB_CHANNEL_ID}")

async def view_config_command_logic(update_or_query):
    dump_display = DUMP_CHANNEL_ID if DUMP_CHANNEL_ID else "Not Set (files sent to user)"
    fsub_display = FORCE_SUB_CHANNEL_ID if FORCE_SUB_CHANNEL_ID else "Disabled"
    config_text = (
        f"**Current Bot Configuration:**\n\n"
        f"**Dump Channel ID:** `{dump_display}`\n"
        f"**Force Subscribe Channel:** `{fsub_display}`\n"
        f"**Admin User IDs:** `{ADMIN_USER_IDS}`"
    )
    if isinstance(update_or_query, Update) and update_or_query.message: 
        await update_or_query.message.reply_text(config_text, parse_mode=ParseMode.MARKDOWN)
    elif hasattr(update_or_query, 'message') and update_or_query.message: 
        try:
            await update_or_query.edit_message_text(config_text, parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard()) 
        except Exception as e: 
            logger.warning(f"Failed to edit settings message: {e}. Sending new one.")
            await update_or_query.message.reply_text(config_text, parse_mode=ParseMode.MARKDOWN)

async def view_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    await view_config_command_logic(update)

def settings_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 View Configuration", callback_data='settings_view_config')],
        [InlineKeyboardButton("📦 Set Dump Channel", callback_data='settings_set_dump_info')],
        [InlineKeyboardButton("📢 Set ForceSub Channel", callback_data='settings_set_fsub_info')],
        [InlineKeyboardButton("❌ Close Settings", callback_data='settings_close')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    await update.message.reply_text("⚙️ **Bot Settings Panel** ⚙️\nChoose an option:", reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)

async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    if not await is_admin(query.from_user.id):
        await query.message.reply_text("You are not authorized to use this action.")
        return
    data = query.data
    if data == 'settings_view_config': await view_config_command_logic(query) 
    elif data == 'settings_set_dump_info': await query.message.reply_text("To set the Dump Channel, use the command:\n`/setdump <channel_id>` (e.g., -100xxxxxxxxxx)\nOr use `/setdump none` to clear.", parse_mode=ParseMode.MARKDOWN)
    elif data == 'settings_set_fsub_info': await query.message.reply_text("To set the Force Subscribe Channel, use the command:\n`/setfsub <@channel_username_or_id>`\nOr use `/setfsub none` to disable.", parse_mode=ParseMode.MARKDOWN)
    elif data == 'settings_close':
        try: await query.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete settings message: {e}")
            await query.edit_message_text("Settings panel closed.", reply_markup=None) 

# === User Command Handlers ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context): return
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hello {user_name}!\n"
        "I can help you download files from Terabox links.\n"
        "Just send me a Terabox link, and I'll process it for you.\n\n"
        "Type /help for more information."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context): return
    help_text = (
        "**How to use this bot:**\n\n"
        "1. Send any valid Terabox link directly to me.\n"
        "2. I will attempt to fetch the direct download link(s).\n"
        "3. The file(s) will be downloaded and then processed.\n\n" 
        "**Features:**\n"
        "- Shows download progress.\n"
        "- Uses Aria2c for robust downloads.\n\n"
        "If you encounter any issues, please ensure your link is correct and publicly accessible."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# === Message Handler for Terabox Links ===
async def handle_terabox_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DUMP_CHANNEL_ID 
    if not aria2:
        await update.message.reply_text("Aria2c client is not available. Please contact the admin.")
        logger.error("handle_terabox_link called but aria2 client is None.")
        return

    if not update.message or not update.message.text: return
    if not await check_subscription(update, context): return

    message_text = update.message.text
    terabox_link_pattern = r"https?://(?:www\.)?(?:terabox|freeterabox|teraboxapp|1024tera|nephobox|mirrobox|4funbox|momerybox|terabox\.app|gibibox|goaibox|terasharelink|1024terabox|teraboxshare)\.(?:com|app|link|me|xyz|cloud|fun|online|store|shop|top|pw|org|net|info|mobi|asia|vip|pro|life|live|world|space|tech|site|icu|cyou|buzz|gallery|website|press|services|show|run|gold|plus|guru|center|group|company|directory|today|digital|network|solutions|systems|technology|software|click|store|shop|ninja|money|pics|lol|tube|pictures|cam|vin|art|blog|best|fans|media|game|video|stream|movie|film|music|audio|cloud|drive|share|storage|file|data|download|backup|upload|box|disk)\S+"
    match = re.search(terabox_link_pattern, message_text, re.IGNORECASE)
    
    if not match:
        if not (message_text.startswith('/') or len(message_text.split()) > 10): 
            await update.message.reply_text("Please send a valid Terabox link. If you need help, type /help.")
        return

    terabox_url_from_user = match.group(0)
    status_msg = await update.message.reply_text(f"🔄 Processing Terabox link: {terabox_url_from_user[:50]}...")
    target_chat_id_for_files = DUMP_CHANNEL_ID if DUMP_CHANNEL_ID else update.message.chat_id
    user_id_for_log = update.effective_user.id
    user_mention_for_log = f"<a href='tg://user?id={user_id_for_log}'>{update.effective_user.first_name}</a>"

    # Construct the API URL for aria2c
    # Using the cheemsbackup API as it's known to redirect, which aria2c can handle.
    encoded_url = quote(terabox_url_from_user)
    api_dl_url = f"https://teradlrobot.cheemsbackup.workers.dev/?url={encoded_url}"
    
    download: Aria2Download = None
    file_path = None
    original_filename_from_aria = "Unknown File"

    try:
        logger.info(f"Adding URI to aria2c: {api_dl_url}")
        try:
            download = aria2.add_uris([api_dl_url]) # This returns a list of Download objects
            if not download: # Should not happen if add_uris is successful
                raise Aria2cError("Failed to add URI to aria2c, returned None.")
            download = download[0] # Get the first (and likely only) download object
            logger.info(f"Aria2c download added with GID: {download.gid}")
        except Exception as e:
            logger.error(f"Error adding URI to aria2c: {e}")
            await status_msg.edit_text(f"❌ Error initiating download with aria2c: {str(e)[:100]}")
            return

        await status_msg.edit_text(f"⏳ Download initiated with GID: {download.gid}. Waiting for metadata...")

        download_start_time = datetime.now()
        last_update_time = time.time()

        while not download.is_complete:
            await asyncio.sleep(2.5) # Update interval
            try:
                download.update()
            except Exception as e:
                logger.error(f"Error updating aria2c download status for GID {download.gid}: {e}")
                # Optionally break or continue, depending on desired robustness
                await status_msg.edit_text(f"⚠️ Error updating download status. Will keep trying.\nError: {str(e)[:50]}")
                continue # Try to update again in the next loop

            if download.status == 'error':
                error_msg = download.error_message
                logger.error(f"Aria2c download error for GID {download.gid}: {error_msg}")
                await status_msg.edit_text(f"❌ Aria2c download failed: {error_msg[:200]}")
                return
            
            if not download.name and not download.files: # Still waiting for metadata
                if (datetime.now() - download_start_time).total_seconds() > 60: # Timeout for metadata
                    logger.warning(f"Timeout waiting for metadata for GID {download.gid}")
                    await status_msg.edit_text("⌛ Timeout waiting for file metadata from the link. Please try another link or API.")
                    try: download.remove(force=True, clean=True)
                    except Exception: pass
                    return
                # Update status message to show it's still fetching info
                try:
                    await status_msg.edit_text(f"⏳ GID: {download.gid}. Fetching file info...", parse_mode=ParseMode.MARKDOWN)
                except BadRequest: pass # Message not modified
                continue


            original_filename_from_aria = download.name if download.name else (os.path.basename(download.files[0].path) if download.files else "Unknown File")
            
            # Progress calculation
            progress_percent = download.progress
            completed_length_hr = get_readable_file_size(download.completed_length)
            total_length_hr = get_readable_file_size(download.total_length)
            download_speed_hr = get_readable_file_size(download.download_speed)
            eta_hr = download.eta_string(human_readable=True) if download.eta else "N/A"
            
            elapsed_time_delta = datetime.now() - download_start_time
            elapsed_minutes, elapsed_seconds = divmod(int(elapsed_time_delta.total_seconds()), 60)

            progress_bar_filled = int(progress_percent / 5) # 20 stars/dots
            progress_bar = "★" * progress_bar_filled + "☆" * (20 - progress_bar_filled)

            status_text = (
                f"┏ ғɪʟᴇɴᴀᴍᴇ: {original_filename_from_aria}\n"
                f"┠ [{progress_bar}] {progress_percent:.2f}%\n"
                f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {completed_length_hr} ᴏғ {total_length_hr}\n"
                f"┠ sᴛᴀᴛᴜs: 📥 Downloading ({download.status})\n"
                f"┠ ᴇɴɢɪɴᴇ: Aria2c\n" 
                f"┠ sᴘᴇᴇᴅ: {download_speed_hr}/s\n"
                f"┠ ᴇᴛᴀ: {eta_hr} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
                f"┖ ᴜsᴇʀ: {user_mention_for_log} | ɪᴅ: {user_id_for_log}\n"
            )
            
            current_time_for_edit = time.time()
            if current_time_for_edit - last_update_time > 2.0: # Control edit frequency
                try:
                    await status_msg.edit_text(status_text, parse_mode=ParseMode.HTML)
                    last_update_time = current_time_for_edit
                except BadRequest: # Message not modified or other issue
                    pass 
                except Exception as e_edit:
                    logger.warning(f"Could not edit status message: {e_edit}")


        if download.is_complete:
            if not download.files:
                logger.error(f"Aria2c download GID {download.gid} completed but no files found.")
                await status_msg.edit_text("❌ Download completed but no file information found from aria2c.")
                return
            
            file_path = download.files[0].path
            original_filename_from_aria = download.name if download.name else os.path.basename(file_path)
            logger.info(f"Aria2c download complete for GID {download.gid}. File: {file_path}")
            await status_msg.edit_text(f"✅ Downloaded **{original_filename_from_aria}** via Aria2c.\nNow preparing for upload...", parse_mode=ParseMode.MARKDOWN)
        else: # Should not be reached if loop exits due to error or completion
            logger.warning(f"Aria2c download GID {download.gid} loop exited, status: {download.status}")
            await status_msg.edit_text(f"⚠️ Download process ended with status: {download.status}")
            return

        # --- Upload Logic (adapted from previous version) ---
        if not file_path or not os.path.exists(file_path):
            logger.error(f"File path {file_path} not found after aria2c download.")
            await status_msg.edit_text("❌ Error: Downloaded file not found on server.")
            return

        final_file_size = os.path.getsize(file_path)
        # Sanitize filename for Telegram (using the name Aria2c determined)
        filename_for_upload = re.sub(r'[<>:"/\\|?*]', '_', original_filename_from_aria)[:200]
        if not filename_for_upload: filename_for_upload = f"file_from_aria2_{download.gid}"


        if final_file_size > 2 * 1024 * 1024 * 1024: 
            error_large_file = f"❌ File **{filename_for_upload}** is too large ({get_readable_file_size(final_file_size)}) to upload. Max is 2GB."
            await status_msg.edit_text(error_large_file, parse_mode=ParseMode.MARKDOWN)
            if DUMP_CHANNEL_ID:
                 await context.bot.send_message(DUMP_CHANNEL_ID, f"Failed to upload: {filename_for_upload} (too large: {get_readable_file_size(final_file_size)}) from user {user_id_for_log} ({update.effective_user.username or 'no_username'}). Link: {terabox_url_from_user}")
            return # Skip upload for this file

        await status_msg.edit_text(f"⬆️ Uploading **{filename_for_upload}** ({get_readable_file_size(final_file_size)})...", parse_mode=ParseMode.MARKDOWN)

        caption_text = f"✨ **{filename_for_upload}**\n\n"
        caption_text += f"👤 **Leeched by:** {user_mention_for_log}\n"
        caption_text += f"📥 **User Link:** `tg://user?id={user_id_for_log}`\n\n"
        # caption_text += f"🗂️ **Folder:** {folder_title}\n" # Folder title not easily available with this aria2c method
        caption_text += f"⚙️ **Size:** {get_readable_file_size(final_file_size)}\n\n"
        caption_text += f"🤖 Processed by @{context.bot.username}"

        file_ext = os.path.splitext(filename_for_upload)[1].lower()
        sent_message = None
        upload_timeout_seconds = max(120, min(3000, int(final_file_size / (25 * 1024 * 1024 / 60) ) ))

        with open(file_path, "rb") as doc_to_send:
            send_kwargs = {
                "chat_id": target_chat_id_for_files, "caption": caption_text, "filename": filename_for_upload,
                "parse_mode": ParseMode.HTML, "request_timeout": upload_timeout_seconds + 60, 
                "connect_timeout": 30, "read_timeout": upload_timeout_seconds
            }
            if file_ext in ['.mp4', '.mkv', '.mov', '.avi', '.webm'] and final_file_size < 2 * 1024 * 1024 * 1024: 
                sent_message = await context.bot.send_video(video=doc_to_send, supports_streaming=True, **send_kwargs)
            elif file_ext in ['.mp3', '.ogg', '.wav', '.flac', '.m4a'] and final_file_size < 2 * 1024 * 1024 * 1024: 
                sent_message = await context.bot.send_audio(audio=doc_to_send, **send_kwargs)
            elif final_file_size < 2 * 1024 * 1024 * 1024 : 
                sent_message = await context.bot.send_document(document=doc_to_send, **send_kwargs)
        
        if sent_message:
            if target_chat_id_for_files == update.message.chat_id:
                upload_success_text = f"✅ Successfully uploaded **{filename_for_upload}**!"
                await status_msg.edit_text(upload_success_text, parse_mode=ParseMode.MARKDOWN)
            else: 
                upload_success_text_user = f"✅ Successfully processed **{filename_for_upload}**." 
                await update.message.reply_text(upload_success_text_user, parse_mode=ParseMode.MARKDOWN) 
                if status_msg.chat_id == update.message.chat_id : 
                   await status_msg.edit_text(upload_success_text_user, parse_mode=ParseMode.MARKDOWN)
        else:
            await status_msg.edit_text(f"⚠️ Could not upload **{filename_for_upload}**. The bot might lack permissions or an unknown error occurred.", parse_mode=ParseMode.MARKDOWN)

    except Aria2cError as e: # Catch custom Aria2c specific errors
        logger.error(f"Aria2cError processing link {terabox_url_from_user}: {e}")
        await status_msg.edit_text(f"❌ Aria2c processing error: {e}")
    except DirectDownloadLinkException as e: # Should not be hit if fetch_terabox_links is removed
        logger.warning(f"DirectDownloadLinkException for {terabox_url_from_user}: {e}")
        await status_msg.edit_text(f"❌ Error processing link: {e}")
    except Exception as e:
        logger.error(f"Unhandled error processing link {terabox_url_from_user}: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ An unexpected error occurred. Please try again later or check the link.\nError: {str(e)[:100]}")
    finally:
        # Clean up the download from aria2c and the file from disk
        if download:
            try:
                if file_path and os.path.exists(file_path): # Ensure file_path is defined
                    logger.info(f"Removing downloaded file: {file_path}")
                    os.remove(file_path)
                logger.info(f"Removing download GID {download.gid} from aria2c (force=True, clean=True)")
                download.remove(force=True, clean=True) # Remove from aria2 queue and delete files if not already
            except Exception as e_clean:
                logger.error(f"Error during aria2c cleanup for GID {download.gid}: {e_clean}")
        elif file_path and os.path.exists(file_path): # If download object was not created but file_path somehow exists
             try:
                logger.info(f"Removing orphaned downloaded file: {file_path}")
                os.remove(file_path)
             except Exception as e_rm_orphan:
                logger.error(f"Error removing orphaned file {file_path}: {e_rm_orphan}")


# === Main Application Setup ===
def run_bot():
    _initialize_config() 
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. Exiting.")
        return
    if not aria2: # Check if aria2 client was initialized
        logger.critical("Aria2c client is not initialized. Bot cannot function for downloads. Exiting.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("setdump", set_dump_command))
    application.add_handler(CommandHandler("setfsub", set_fsub_command))
    application.add_handler(CommandHandler("viewconfig", view_config_command))
    application.add_handler(CommandHandler("settings", settings_command)) 
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_terabox_link))

    logger.info("Bot started and polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    temp_dir = "./temp_downloads" # This directory is for httpx, aria2c will use its own configured dir or default.
                                 # However, we might still use it if we decide to move files from aria2's dir.
                                 # For now, aria2c downloads to its own dir, and we use the absolute path.
    if not os.path.exists(temp_dir): # This is less critical now with aria2c
        try:
            os.makedirs(temp_dir)
            logger.info(f"Created directory (for potential future use): {temp_dir}")
        except OSError as e:
            logger.warning(f"Could not create directory {temp_dir}: {e}.")
    
    # Optional: Create aria2c download directory if specified in options and doesn't exist
    # aria2_download_dir = aria2_options_dict.get("dir")
    # if aria2_download_dir and not os.path.exists(aria2_download_dir):
    #     try:
    #         os.makedirs(aria2_download_dir)
    #         logger.info(f"Created aria2c download directory: {aria2_download_dir}")
    #     except OSError as e:
    #         logger.error(f"Could not create aria2c download directory {aria2_download_dir}: {e}")

    run_bot()
