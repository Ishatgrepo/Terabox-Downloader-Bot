from aria2p import API as Aria2API, Client as Aria2Client
import asyncio
from dotenv import load_dotenv
from datetime import datetime
import os
import logging
import math
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait
import time
import urllib.parse
from urllib.parse import urlparse
from flask import Flask, render_template
from threading import Thread

load_dotenv('config.env', override=True)
logging.basicConfig(
    level=logging.INFO,  
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s - %(filename)s:%(lineno)d"
)

logger = logging.getLogger(__name__)

logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.ERROR)

aria2 = Aria2API(
    Aria2Client(
        host="http://localhost",
        port=6800,
        secret=""
    )
)
options = {
    "max-tries": "50",
    "retry-wait": "3",
    "continue": "true",
    "allow-overwrite": "true",
    "min-split-size": "4M",
    "split": "10"
}

aria2.set_global_options(options)

API_ID = os.environ.get('TELEGRAM_API', '')
if len(API_ID) == 0:
    logging.error("TELEGRAM_API variable is missing! Exiting now")
    exit(1)

API_HASH = os.environ.get('TELEGRAM_HASH', '')
if len(API_HASH) == 0:
    logging.error("TELEGRAM_HASH variable is missing! Exiting now")
    exit(1)
    
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
if len(BOT_TOKEN) == 0:
    logging.error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

DUMP_CHAT_ID = os.environ.get('DUMP_CHAT_ID', '')
if len(DUMP_CHAT_ID) == 0:
    logging.error("DUMP_CHAT_ID variable is missing! Exiting now")
    exit(1)
else:
    DUMP_CHAT_ID = int(DUMP_CHAT_ID)

FSUB_ID = os.environ.get('FSUB_ID', '')
if len(FSUB_ID) == 0:
    logging.error("FSUB_ID variable is missing! Exiting now")
    exit(1)
else:
    FSUB_ID = int(FSUB_ID)

USER_SESSION_STRING = os.environ.get('USER_SESSION_STRING', '')
if len(USER_SESSION_STRING) == 0:
    logging.info("USER_SESSION_STRING variable is missing! Bot will split Files in 2Gb...")
    USER_SESSION_STRING = None

app = Client("jetbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user = None
SPLIT_SIZE = 2093796556
if USER_SESSION_STRING:
    user = Client("jetu", api_id=API_ID, api_hash=API_HASH, session_string=USER_SESSION_STRING)
    SPLIT_SIZE = 4241280205

VALID_DOMAINS = [
    'terabox.com', 'nephobox.com', '4funbox.com', 'mirrobox.com', 
    'momerybox.com', 'teraboxapp.com', '1024tera.com', 
    'terabox.app', 'gibibox.com', 'goaibox.com', 'terasharelink.com', 
    'teraboxlink.com', 'terafileshare.com'
]

TERABOX_API_URL_TEMPLATES = [
    "https://teradlrobot.cheemsbackup.workers.dev/?url={}",
    "https://teraboxdl.tellycloudapi.workers.dev/?url={}"
]

last_update_time = 0

async def is_user_member(client, user_id):
    try:
        member = await client.get_chat_member(FSUB_ID, user_id)
        if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return True
        else:
            return False
    except Exception as e:
        logging.error(f"Error checking membership status for user {user_id}: {e}")
        return False
    
def is_valid_url(url):
    parsed_url = urlparse(url)
    return any(parsed_url.netloc.endswith(domain) for domain in VALID_DOMAINS)

def format_size(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
    developer_button = InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴘᴇʀ ⚡️", url="https://t.me/rtx5069")
    repo69 = InlineKeyboardButton("ʀᴇᴘᴏ 🌐", url="https://github.com/Hrishi2861/Terabox-Downloader-Bot")
    user_mention = message.from_user.mention
    reply_markup = InlineKeyboardMarkup([[join_button, developer_button], [repo69]])
    final_msg = f"ᴡᴇʟᴄᴏᴍᴇ, {user_mention}.\n\n🌟 ɪ ᴀᴍ ᴀ ᴛᴇʀᴀʙᴏx ᴅᴏᴡɴʟᴏᴀᴅᴇʀ ʙᴏᴛ. sᴇɴᴅ ᴍᴇ ᴀɴʏ ᴛᴇʀᴀʙᴏx ʟɪɴᴋ ɪ ᴡɪʟʟ ᴅᴏᴡɴʟᴏᴀᴅ ᴡɪᴛʜɪɴ ғᴇᴡ sᴇᴄᴏɴᴅs ᴀɴᴅ sᴇɴᴅ ɪᴛ ᴛᴏ ʏᴏᴜ ✨."
    video_file_id = "/app/Jet-Mirror.mp4"
    if os.path.exists(video_file_id):
        await client.send_video(
            chat_id=message.chat.id,
            video=video_file_id,
            caption=final_msg,
            reply_markup=reply_markup
            )
    else:
        await message.reply_text(final_msg, reply_markup=reply_markup)

async def update_status_message(status_message, text):
    try:
        await status_message.edit_text(text)
    except Exception as e:
        logger.error(f"Failed to update status message: {e}")

@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    if message.text.startswith('/'):
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    is_member = await is_user_member(client, user_id)

    if not is_member:
        join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
        reply_markup = InlineKeyboardMarkup([[join_button]])
        await message.reply_text("ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴍʏ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ ᴍᴇ.", reply_markup=reply_markup)
        return
    
    url = None
    for word in message.text.split():
        if is_valid_url(word):
            url = word
            break

    if not url:
        await message.reply_text("Please provide a valid Terabox link.")
        return

    encoded_url = urllib.parse.quote(url)
    
    download = None 
    status_message = await message.reply_text("🔎 ᴘʀᴇᴘᴀʀɪɴɢ ᴅᴏᴡɴʟᴏᴀᴅ...")

    for url_template in TERABOX_API_URL_TEMPLATES:
        final_url = url_template.format(encoded_url)
        logger.info(f"Attempting to add URI: {final_url}")
        
        current_download_attempt = None 
        try:
            # Add the URI to aria2
            current_download_attempt = aria2.add_uris([final_url])
            
            # Poll for a short duration to confirm successful initiation
            max_retries = 3  # Number of times to check
            retry_delay = 5  # Seconds to wait between checks (total ~15s)
            
            for i in range(max_retries):
                await asyncio.sleep(retry_delay)
                current_download_attempt.update() # Refresh download status
                
                logger.debug(
                    f"Polling attempt {i+1}/{max_retries} for {final_url}: "
                    f"Status='{current_download_attempt.status}', "
                    f"Name='{current_download_attempt.name}', "
                    f"Size='{current_download_attempt.total_length}', "
                    f"Error='{current_download_attempt.error_message}'"
                )

                if current_download_attempt.status in ['active', 'waiting'] and \
                   current_download_attempt.total_length > 0 and \
                   current_download_attempt.name:
                    logger.info(f"Successfully initiated download for '{current_download_attempt.name}' from {final_url}")
                    download = current_download_attempt 
                    break 
                elif current_download_attempt.is_complete and not current_download_attempt.has_failed:
                    logger.info(f"Download from {final_url} completed during initial check.")
                    download = current_download_attempt
                    break 
                elif current_download_attempt.status == 'error':
                    logger.warning(
                        f"Download from {final_url} failed. "
                        f"Error Code: {current_download_attempt.error_code}, "
                        f"Message: {current_download_attempt.error_message}"
                    )
                    aria2.remove([current_download_attempt], force=True, clean=True)
                    current_download_attempt = None 
                    break 
            
            if download: 
                break 
            
            if current_download_attempt: # If polling finished, not an error, but no success
                 logger.warning(f"Download from {final_url} did not properly initiate after polling. Status: {current_download_attempt.status}. Removing.")
                 aria2.remove([current_download_attempt], force=True, clean=True)
        except Exception as e:
            logger.error(f"Exception while processing URI {final_url}: {e}", exc_info=True)
            if current_download_attempt:
                try:
                    aria2.remove([current_download_attempt], force=True, clean=True)
                except Exception as re:
                    logger.error(f"Nested error removing download attempt for {final_url}: {re}")
            continue 
            
    if not download:
        await status_message.edit_text(
            "❌ ᴄᴏᴜʟᴅ ɴᴏᴛ ɪɴɪᴛɪᴀᴛᴇ ᴅᴏᴡɴʟᴏᴀᴅ ғʀᴏᴍ ᴀᴠᴀɪʟᴀʙʟᴇ sᴏᴜʀᴄᴇs. "
            "ᴛʜᴇ ʟɪɴᴋ ᴍɪɢʜᴛ ʙᴇ ɪɴᴠᴀʟɪᴅ ᴏʀ ᴀʟʟ sᴇʀᴠᴇʀs ᴀʀᴇ ᴅᴏᴡɴ."
        )
        return

    if not download or not download.files:
        logger.error(f"Download failed or did not return file information. Download object: {download}")
        await status_message.edit_text(
            "❌ ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴏᴍᴘʟᴇᴛᴇᴅ ʙᴜᴛ ғɪʟᴇ ɪɴғᴏʀᴍᴀᴛɪᴏɴ ɪs ᴍɪssɪɴɢ."
        )
        if download: # Attempt to clean up if download object exists
            try:
                aria2.remove([download], force=True, clean=True)
            except Exception as e_rem:
                logger.error(f"Error removing problematic download {download.gid if hasattr(download, 'gid') else 'N/A'}: {e_rem}")
        return

    await status_message.edit_text("sᴇɴᴅɪɴɢ ʏᴏᴜ ᴛʜᴇ ᴍᴇᴅɪᴀ...🤤")
    start_time = datetime.now() # Set start time now that download is confirmed
    
    while not download.is_complete and not download.has_failed: # Add check for download failure
        await asyncio.sleep(15) # Consider reducing sleep if updates are needed faster and FloodWait is handled
        download.update()
        progress = download.progress

        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name}\n"
            f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(download.completed_length)} ᴏғ {format_size(download.total_length)}\n"
            f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>Aria2c v1.37.0</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(download.download_speed)}/s\n"
            f"┠ ᴇᴛᴀ: {download.eta} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
            )
        while True:
            try:
                await update_status_message(status_message, status_text)
                break
            except FloodWait as e:
                logger.error(f"Flood wait detected! Sleeping for {e.value} seconds")
                await asyncio.sleep(e.value)

    if download.has_failed:
        logger.error(f"Download failed: {download.name}, Error: {download.error_message}")
        await update_status_message(status_message, f"❌ Download failed: {download.name}")
        try:
            aria2.remove([download], force=True, clean=True)
        except Exception as e_rem:
            logger.error(f"Error removing failed download {download.gid}: {e_rem}")
        return
    file_path = download.files[0].path
    caption = (
        f"✨ {download.name}\n"
        f"👤 ʟᴇᴇᴄʜᴇᴅ ʙʏ : <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>\n"
        f"📥 ᴜsᴇʀ ʟɪɴᴋ: tg://user?id={user_id}\n\n"
        "[ᴘᴏᴡᴇʀᴇᴅ ʙʏ ᴊᴇᴛ-ᴍɪʀʀᴏʀ ❤️🚀](https://t.me/JetMirror)"
    )

    last_update_time = time.time()
    UPDATE_INTERVAL = 15

    async def update_status(message, text):
        nonlocal last_update_time
        current_time = time.time()
        if current_time - last_update_time >= UPDATE_INTERVAL:
            try:
                await message.edit_text(text)
                last_update_time = current_time
            except FloodWait as e:
                logger.warning(f"FloodWait: Sleeping for {e.value}s")
                await asyncio.sleep(e.value)
                await update_status(message, text)
            except Exception as e:
                logger.error(f"Error updating status: {e}")

    async def upload_progress(current, total):
        progress = (current / total) * 100
        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name}\n"
            f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(current)} ᴏғ {format_size(total)}\n"
            f"┠ sᴛᴀᴛᴜs: 📤 Uploading to Telegram\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>PyroFork v2.2.11</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(current / elapsed_time.seconds if elapsed_time.seconds > 0 else 0)}/s\n"
            f"┠ ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )
        await update_status(status_message, status_text)

    async def split_video_with_ffmpeg(input_path, output_prefix, split_size):
        try:
            original_ext = os.path.splitext(input_path)[1].lower() or '.mp4'
            start_time = datetime.now()
            last_progress_update = time.time()
            
            proc = await asyncio.create_subprocess_exec(
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', input_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            total_duration = float(stdout.decode().strip())
            
            file_size = os.path.getsize(input_path)
            parts = math.ceil(file_size / split_size)
            
            if parts == 1:
                return [input_path]
            
            duration_per_part = total_duration / parts
            split_files = []
            
            for i in range(parts):
                current_time = time.time()
                if current_time - last_progress_update >= UPDATE_INTERVAL:
                    elapsed = datetime.now() - start_time
                    status_text = (
                        f"✂️ Splitting {os.path.basename(input_path)}\n"
                        f"Part {i+1}/{parts}\n"
                        f"Elapsed: {elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                    )
                    await update_status(status_message, status_text)
                    last_progress_update = current_time
                
                output_path = f"{output_prefix}.{i+1:03d}{original_ext}"
                cmd = [
                    'xtra', '-y', '-ss', str(i * duration_per_part),
                    '-i', input_path, '-t', str(duration_per_part),
                    '-c', 'copy', '-map', '0',
                    '-avoid_negative_ts', 'make_zero',
                    output_path
                ]
                
                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()
                split_files.append(output_path)
            
            return split_files
        except Exception as e:
            logger.error(f"Split error: {e}")
            raise

    async def handle_upload():
        nonlocal file_path # Ensure file_path is accessible
        if not os.path.exists(file_path):
            logger.error(f"File not found for upload: {file_path}. This might happen if download failed or file was removed prematurely.")
            await update_status_message(status_message, f"❌ Error: Downloaded file missing: {os.path.basename(file_path if file_path else 'Unknown File')}")
            return

        file_size = os.path.getsize(file_path)
        
        if file_size > SPLIT_SIZE:
            await update_status(
                status_message,
                f"✂️ Splitting {download.name} ({format_size(file_size)})"
            )
            
            split_files = await split_video_with_ffmpeg(
                file_path,
                os.path.splitext(file_path)[0],
                SPLIT_SIZE
            )
            
            try:
                for i, part in enumerate(split_files):
                    part_caption = f"{caption}\n\nPart {i+1}/{len(split_files)}"
                    await update_status(
                        status_message,
                        f"📤 Uploading part {i+1}/{len(split_files)}\n"
                        f"{os.path.basename(part)}"
                    )
                    
                    if USER_SESSION_STRING:
                        sent_to_dump = await user.send_video(
                            DUMP_CHAT_ID, part, 
                            caption=part_caption,
                            progress=upload_progress
                        )
                        if sent_to_dump:
                            await app.copy_message(
                                message.chat.id, DUMP_CHAT_ID, sent_to_dump.id
                            )
                        else:
                            logger.error(f"Failed to send split part {part} to DUMP_CHAT_ID using user account. send_video returned None.")
                            await update_status_message(status_message, f"❌ Error uploading part {os.path.basename(part)}.")
                            # Consider whether to stop all uploads or just skip this part
                    else:
                        sent_to_dump = await client.send_video(
                            DUMP_CHAT_ID, part,
                            caption=part_caption,
                            progress=upload_progress
                        )
                        if sent_to_dump:
                            if sent_to_dump.video:
                                await client.send_video(
                                    message.chat.id, sent_to_dump.video.file_id,
                                    caption=part_caption
                                )
                            else:
                                logger.error(f"Message sent to DUMP_CHAT_ID for part {part} is not a video. Message: {sent_to_dump}")
                                await update_status_message(status_message, f"❌ Error: Failed to confirm video upload for part {os.path.basename(part)}.")
                        else:
                            logger.error(f"Failed to send split part {part} to DUMP_CHAT_ID using bot account. send_video returned None.")
                            await update_status_message(status_message, f"❌ Error uploading part {os.path.basename(part)}.")
                    os.remove(part)
            finally:
                for part in split_files:
                    if os.path.exists(part):
                        try: os.remove(part)
                        except OSError as e: logger.error(f"Error removing split part {part}: {e}")

        else:
            await update_status(
                status_message,
                f"📤 Uploading {download.name}\n"
                f"Size: {format_size(file_size)}"
            )
            
            if USER_SESSION_STRING:
                sent_to_dump = await user.send_video(
                    DUMP_CHAT_ID, file_path,
                    caption=caption,
                    progress=upload_progress
                )
                if sent_to_dump:
                    await app.copy_message(
                        message.chat.id, DUMP_CHAT_ID, sent_to_dump.id
                    )
                else:
                    logger.error(f"Failed to send file {file_path} to DUMP_CHAT_ID using user account. send_video returned None.")
                    await update_status_message(status_message, f"❌ Error uploading file {os.path.basename(file_path)}.")
            else:
                sent_to_dump = await client.send_video(
                    DUMP_CHAT_ID, file_path,
                    caption=caption,
                    progress=upload_progress
                )
                if sent_to_dump:
                    if sent_to_dump.video :
                        await client.send_video(
                            message.chat.id, sent_to_dump.video.file_id,
                            caption=caption
                        )
                    else:
                        logger.error(f"Message sent to DUMP_CHAT_ID for file {file_path} is not a video. Message: {sent_to_dump}")
                        await update_status_message(status_message, f"❌ Error: Failed to confirm video upload for {os.path.basename(file_path)}.")
                else:
                    logger.error(f"Failed to send file {file_path} to DUMP_CHAT_ID using bot account. send_video returned None.")
                    await update_status_message(status_message, f"❌ Error uploading file {os.path.basename(file_path)}.")

        if os.path.exists(file_path):
            try: os.remove(file_path)
            except OSError as e: logger.error(f"Error removing original file {file_path}: {e}")

    start_time = datetime.now()
    await handle_upload()

    try:
        await status_message.delete()
        await message.delete()
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return render_template("index.html")

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

def keep_alive():
    Thread(target=run_flask).start()

async def start_user_client():
    if user:
        await user.start()
        logger.info("User client started.")

def run_user():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_user_client())

if __name__ == "__main__":
    keep_alive()

    if user:
        logger.info("Starting user client...")
        Thread(target=run_user).start()

    logger.info("Starting bot client...")
    app.run()
