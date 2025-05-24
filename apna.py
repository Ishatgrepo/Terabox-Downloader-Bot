#!/usr/bin/env python
# pylint: disable=logging-fstring-interpolation, disable=broad-except, disable=invalid-name
import asyncio
import httpx
import os
import re
import logging
import time
from urllib.parse import urlparse, quote
from requests import post, get, RequestException # For the synchronous terabox link fetching part
from collections import deque
from datetime import datetime # Added for elapsed time calculation

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import RetryAfter 

try:
    import aria2p # For aria2c RPC
except ImportError:
    aria2p = None # Handle missing library gracefully

# === Configuration ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "7893919705:AAE9b6jpHFdxzQQIucrNMEvje2u7N8uL15o")
DUMP_CHANNEL_ID_STR = os.getenv("DUMP_CHANNEL_ID", "-1002281669966")
FORCE_SUB_CHANNEL_ID_STR = os.getenv("FORCE_SUB_CHANNEL_ID", None)
ADMIN_USER_IDS_STR = os.getenv("ADMIN_USER_IDS", "6469067345")
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in ADMIN_USER_IDS_STR.split(',') if admin_id.strip()]


# === Aria2c Configuration ===
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "http://localhost")
ARIA2_RPC_PORT = int(os.getenv("ARIA2_RPC_PORT", 6800))
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "") 
ARIA2_ENABLED = os.getenv("ARIA2_ENABLED", "true").lower() == "true"
ARIA2_GLOBAL_OPTIONS = { 
    "max-tries": "50",
    "retry-wait": "3",
    "continue": "true",
    "allow-overwrite": "true",
    "min-split-size": "4M",
    "split": "10",
    "max-connection-per-server": "16", # Increased from default 1
    "max-concurrent-downloads": "10",  # Increased from default 5
    "optimize-concurrent-downloads": "true",
}


# Runtime configuration variables
DUMP_CHANNEL_ID = None
FORCE_SUB_CHANNEL_ID = None 
aria2_client = None
ARIA2_VERSION_STR = "N/A" 

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

def initialize_aria2():
    global aria2_client, ARIA2_VERSION_STR
    if not ARIA2_ENABLED:
        logger.info("Aria2 integration is disabled by configuration.")
        aria2_client = None
        return

    if aria2p is None:
        logger.warning("aria2p library is not installed. Aria2c integration will be disabled. pip install aria2p")
        aria2_client = None
        return

    try:
        logger.info(f"Attempting to connect to Aria2 RPC server at {ARIA2_RPC_HOST}:{ARIA2_RPC_PORT}")
        low_level_client = aria2p.Client(
            host=ARIA2_RPC_HOST,
            port=ARIA2_RPC_PORT,
            secret=ARIA2_RPC_SECRET
        )
        # The API wrapper is still useful for managing downloads (add_uris, etc.)
        current_aria2_api_wrapper = aria2p.API(low_level_client)
        
        # Use direct RPC calls for version and stats as a more robust method
        logger.info("Attempting direct RPC call for aria2.getVersion")
        version_data = low_level_client.call("aria2.getVersion")
        logger.info(f"aria2.getVersion response: {version_data}")
        
        logger.info("Attempting direct RPC call for aria2.getGlobalStat")
        stats_data = low_level_client.call("aria2.getGlobalStat")
        logger.info(f"aria2.getGlobalStat response: {stats_data}")

        ARIA2_VERSION_STR = version_data.get("version", "Unknown")
        enabled_features = version_data.get("enabledFeatures", [])
        
        active_downloads = stats_data.get("numActive", "N/A")
        waiting_downloads = stats_data.get("numWaiting", "N/A")
        stopped_downloads = stats_data.get("numStopped", "N/A") # numStoppedTotal might also be available

        logger.info(f"Successfully connected to Aria2 RPC server. Version: {ARIA2_VERSION_STR}, "
                    f"Features: {enabled_features}, "
                    f"Stats: {active_downloads} active / {waiting_downloads} waiting / {stopped_downloads} stopped.")
        
        logger.info(f"Setting Aria2c global options: {ARIA2_GLOBAL_OPTIONS}")
        current_aria2_api_wrapper.set_global_options(ARIA2_GLOBAL_OPTIONS)
        logger.info("Aria2c global options set successfully.")
        aria2_client = current_aria2_api_wrapper
    except aria2p.client.ClientException as ce:
        logger.error(f"Aria2 ClientException during initialization: {ce}. This often indicates a connection or authentication issue with the Aria2 RPC server.")
        aria2_client = None
        ARIA2_VERSION_STR = "Error (ClientEx)"
    except Exception as e:
        logger.error(f"Could not connect to Aria2 RPC server or set options at {ARIA2_RPC_HOST}:{ARIA2_RPC_PORT}. "
                     f"Ensure aria2c is running in daemon mode with RPC enabled. Error: {e}", exc_info=True)
        aria2_client = None
        ARIA2_VERSION_STR = "Error (Conn/Other)"


# === Logging Setup ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING) 
if aria2p:
    logging.getLogger("aria2p").setLevel(logging.WARNING)

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

# === Custom Exception ===
class DirectDownloadLinkException(Exception):
    """Custom exception for direct download link errors."""
    pass

# === Terabox Link Fetching Logic ===
def fetch_terabox_links(input_url: str):
    """
    Fetches direct download links from a Terabox URL.
    """
    logger.info(f"Attempting to fetch links for URL: {input_url}")

    terabox_domain_pattern = r"terabox\.com|teraboxapp\.com|1024tera\.com|freeterabox\.com|teraboxlink\.com|mirrobox\.com|nephobox\.com|4funbox\.com|momerybox\.com|terabox\.app|gibibox\.com|goaibox\.com|terasharelink\.com|1024terabox\.com|teraboxshare\.com"
    if not re.search(terabox_domain_pattern, input_url, re.IGNORECASE):
        raise DirectDownloadLinkException("ERROR: Invalid Terabox URL pattern.")
    
    shortlink_pattern = r"/s/(\w+)|surl=(\w+)"
    if not re.search(shortlink_pattern, input_url, re.IGNORECASE):
        logger.warning(f"URL {input_url} does not match typical /s/ or surl= pattern, but proceeding.")

    parsed_input_url = urlparse(input_url)
    netloc = parsed_input_url.netloc

    url_for_tellycloud_like_apis = input_url.replace(netloc, "1024tera.com") if "terabox.com" in netloc or "teraboxapp.com" in netloc or "freeterabox.com" in netloc else input_url
    quoted_input_url = quote(input_url)

    # Using the specific API from the user's example for direct link fetching
    # This simplifies the link fetching part significantly if this one API is reliable
    api_to_use = {
        "name": "cheems_robot_dl_direct", # Name it for clarity
        "api_call_url": f"https://teradlrobot.cheemsbackup.workers.dev/?url={quoted_input_url}",
        "method": "GET"
    }
    
    common_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Accept": "application/json, text/plain, */*", # Expecting JSON
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site" # Often needed for worker APIs
    }

    response_json = None
    logger.info(f"Trying API: {api_to_use['name']} ({api_to_use['api_call_url']})")

    try:
        api_response = get(api_to_use["api_call_url"], headers=common_headers, timeout=30, allow_redirects=True)
        api_response.raise_for_status()
        
        # The example implies this API directly returns the JSON structure we need
        current_response_json = api_response.json()

        # Check for a structure similar to what the example's aria2.add_uris would receive
        # Example structure: {"response": [{"url": "...", "title": "...", "size": "..."}]}
        # Or a simpler direct link structure if the API provides it directly.
        if ("response" in current_response_json and isinstance(current_response_json["response"], list) and current_response_json["response"] and "url" in current_response_json["response"][0]) or \
           ("direct_link" in current_response_json and "file_name" in current_response_json): # For other common direct link APIs
            response_json = current_response_json
            logger.info(f"Successfully fetched and parsed JSON from API: {api_to_use['name']}")
        else:
            logger.warning(f"API {api_to_use['name']} gave OK status but unexpected JSON structure: {str(current_response_json)[:300]}")
            raise DirectDownloadLinkException("ERROR: API response JSON structure not recognized.")

    except RequestException as e:
        logger.error(f"RequestException with API {api_to_use['name']}: {e}")
        raise DirectDownloadLinkException(f"ERROR: Network error with API: {e}")
    except ValueError as e: 
        logger.error(f"JSONDecodeError with API {api_to_use['name']}: {e}. Response: {api_response.text[:200] if 'api_response' in locals() and api_response else 'N/A'}")
        raise DirectDownloadLinkException(f"ERROR: Could not decode API JSON response: {e}")
    except Exception as e:
        logger.error(f"Generic error with API {api_to_use['name']}: {e}", exc_info=True)
        raise DirectDownloadLinkException(f"ERROR: Generic error with API: {e}")

    if not response_json: # Should be caught by exceptions above, but as a safeguard
        raise DirectDownloadLinkException("ERROR: Unable to fetch valid JSON data from the API endpoint.")

    details = {"contents": [], "title": "Terabox Content", "total_size": 0, "is_folder": False}

    # Adapting parser for the structure implied by the example:
    # {"response": [{"url": "...", "title": "...", "size": "..."}]}
    if "response" in response_json and isinstance(response_json["response"], list):
        logger.info(f"Parsing as 'response list' structure from API: {api_to_use['name']}")
        response_list = response_json["response"]
        if not response_list:
            logger.warning(f"API {api_to_use['name']}: 'response' list is empty.")
            raise DirectDownloadLinkException("ERROR: API 'response' list is empty.")
        
        details["is_folder"] = len(response_list) > 1
        # If it's a folder, the title might be from the first item or a general one.
        # If single file, use its title.
        details["title"] = response_list[0].get("title", "Terabox Folder" if details["is_folder"] else "Terabox File")

        for i_idx, item in enumerate(response_list):
            file_url = item.get("url")
            file_title = item.get("title", f"file_{i_idx+1}")
            
            if file_url:
                details["contents"].append({"url": file_url, "filename": file_title})
                # Size parsing can be added here if the API provides it reliably per item
                # file_size_str = item.get("size", "0")
                # try:
                #     if isinstance(file_size_str, (int,float)): details["total_size"] += int(file_size_str)
                # except ValueError: pass
            else:
                logger.warning(f"Item {i_idx} in response list from {api_to_use['name']} missing 'url'. Item: {item}")
        
        if not details["contents"]:
            logger.warning(f"API {api_to_use['name']}: No usable URLs found in 'response' list.")
            raise DirectDownloadLinkException("ERROR: No usable download URLs extracted from API response.")

    # Fallback for other direct link structures (like the ones previously handled)
    elif response_json.get("direct_link") and response_json.get("file_name"): 
        logger.info(f"Parsing as 'direct_link, file_name' structure from API: {api_to_use['name']}")
        details["title"] = response_json.get("file_name")
        details["contents"].append({"url": response_json["direct_link"], "filename": response_json.get("file_name")})
        try: 
            size_val = response_json.get("file_size_bytes", response_json.get("size", 0))
            if isinstance(size_val, str) and size_val.isdigit():
                details["total_size"] = int(size_val)
            elif isinstance(size_val, (int, float)):
                 details["total_size"] = int(size_val)
        except (ValueError, TypeError): 
            logger.warning(f"Could not parse file_size for API {api_to_use['name']}")
    else:
        logger.error(f"Unhandled JSON structure from API {api_to_use['name']}. JSON: {str(response_json)[:300]}")
        raise DirectDownloadLinkException("ERROR: Unhandled or invalid API response structure after all fallbacks.")

    if not details["contents"]: # Should be caught by earlier checks
        logger.error(f"No valid download links found after processing JSON from {api_to_use['name']}.")
        raise DirectDownloadLinkException("ERROR: No valid download links extracted.")

    logger.info(f"Successfully processed. Found {len(details['contents'])} items. Title: {details['title']}")
    return details


# === Helper Functions ===
def format_size(size_in_bytes: int) -> str: 
    if not isinstance(size_in_bytes, (int, float)) or size_in_bytes < 0: return "N/A"
    if size_in_bytes == 0: return "0 B"
    
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.2f} KB"
    elif size_in_bytes < 1024 * 1024 * 1024:
        return f"{size_in_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_in_bytes / (1024 * 1024 * 1024):.2f} GB"

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
    global ARIA2_VERSION_STR
    dump_display = DUMP_CHANNEL_ID if DUMP_CHANNEL_ID else "Not Set (files sent to user)"
    fsub_display = FORCE_SUB_CHANNEL_ID if FORCE_SUB_CHANNEL_ID else "Disabled"
    aria2_status_msg = "Enabled" if ARIA2_ENABLED and aria2_client else ("Disabled by config" if not ARIA2_ENABLED else "Not Connected/aria2p missing")
    
    config_text = (
        f"**Current Bot Configuration:**\n\n"
        f"**Dump Channel ID:** `{dump_display}`\n"
        f"**Force Subscribe Channel:** `{fsub_display}`\n"
        f"**Admin User IDs:** `{ADMIN_USER_IDS}`\n"
        f"**Aria2c Integration:** `{aria2_status_msg}`\n"
    )
    if ARIA2_ENABLED and aria2_client:
        config_text += f"  - RPC: `{ARIA2_RPC_HOST}:{ARIA2_RPC_PORT}`\n"
        config_text += f"  - Aria2c Version: `{ARIA2_VERSION_STR}`\n"


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
    if data == 'settings_view_config':
        await view_config_command_logic(query) 
    elif data == 'settings_set_dump_info':
        await query.message.reply_text("To set the Dump Channel, use the command:\n`/setdump <channel_id>` (e.g., -100xxxxxxxxxx)\nOr use `/setdump none` to clear.", parse_mode=ParseMode.MARKDOWN)
    elif data == 'settings_set_fsub_info':
        await query.message.reply_text("To set the Force Subscribe Channel, use the command:\n`/setfsub <@channel_username_or_id>`\nOr use `/setfsub none` to disable.", parse_mode=ParseMode.MARKDOWN)
    elif data == 'settings_close':
        try:
            await query.message.delete()
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
        "- Attempts to handle single files and folders from Terabox.\n"
        "- Uses Aria2c for faster downloads if configured and available.\n\n"
        "If you encounter any issues, please ensure your link is correct and publicly accessible."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# === Robust Status Message Updater ===
async def update_tg_status_message(status_msg_obj, text_to_send, parse_mode_val=ParseMode.MARKDOWN):
    if status_msg_obj is None: # Safety check
        logger.warning("Attempted to update a None status message.")
        return
    if status_msg_obj.text == text_to_send: 
        return
    
    # Limit update frequency to avoid hitting rate limits too aggressively
    # This is a simple way; a more sophisticated approach might use a per-chat lock or queue
    context = ContextTypes.DEFAULT_TYPE(application=status_msg_obj.get_bot().application) # Get context
    now = time.time()
    last_edit_time = context.chat_data.get(f"last_edit_time_{status_msg_obj.message_id}", 0)
    
    if now - last_edit_time < 1.5: # Edit at most every 1.5 seconds
        await asyncio.sleep(1.5 - (now - last_edit_time)) # Wait for the remainder of the interval

    while True:
        try:
            await status_msg_obj.edit_text(text_to_send, parse_mode=parse_mode_val)
            context.chat_data[f"last_edit_time_{status_msg_obj.message_id}"] = time.time()
            break 
        except RetryAfter as e: 
            logger.warning(f"RetryAfter: waiting for {e.retry_after} seconds before retrying status update for message {status_msg_obj.message_id}.")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error(f"Failed to update status message {status_msg_obj.message_id}: {e}", exc_info=True)
            break 

# === Message Handler for Terabox Links ===
async def handle_terabox_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DUMP_CHANNEL_ID, aria2_client, ARIA2_VERSION_STR
    if not update.message or not update.message.text: return
    if not await check_subscription(update, context): return

    message_text = update.message.text
    # More comprehensive Terabox domain matching
    terabox_link_pattern = r"https?://(?:www\.)?(?:[a-zA-Z0-9-]+\.)?(?:terabox|freeterabox|teraboxapp|1024tera|nephobox|mirrobox|4funbox|momerybox|terabox\.app|gibibox|goaibox|terasharelink|1024terabox|teraboxshare|terafileshare)\.(?:com|app|link|me|xyz|cloud|fun|online|store|shop|top|pw|org|net|info|mobi|asia|vip|pro|life|live|world|space|tech|site|icu|cyou|buzz|gallery|website|press|services|show|run|gold|plus|guru|center|group|company|directory|today|digital|network|solutions|systems|technology|software|click|store|shop|ninja|money|pics|lol|tube|pictures|cam|vin|art|blog|best|fans|media|game|video|stream|movie|film|music|audio|cloud|drive|share|storage|file|data|download|backup|upload|box|disk)\S+"
    match = re.search(terabox_link_pattern, message_text, re.IGNORECASE)
    
    if not match:
        if not (message_text.startswith('/') or len(message_text.split()) > 10): 
            # Avoid replying to potential commands or very long non-link messages
            await update.message.reply_text("Please send a valid Terabox link. If you need help, type /help.")
        return

    url_to_process = match.group(0)
    status_msg = await update.message.reply_text(f"🔄 Processing Terabox link: {url_to_process[:50]}...")
    target_chat_id_for_files = DUMP_CHANNEL_ID if DUMP_CHANNEL_ID else update.message.chat_id
    user_id_for_status = update.effective_user.id
    user_first_name_for_status = update.effective_user.first_name.replace("<","&lt;").replace(">","&gt;") # Basic HTML escape
    user_info_for_status = f"<a href='tg://user?id={user_id_for_status}'>{user_first_name_for_status}</a> | ɪᴅ: {user_id_for_status}"


    try:
        loop = asyncio.get_event_loop()
        # fetch_terabox_links is synchronous, run in executor
        terabox_data = await loop.run_in_executor(None, fetch_terabox_links, url_to_process)

        if not terabox_data or not terabox_data.get("contents"):
            await update_tg_status_message(status_msg, "❌ Could not retrieve download information. The link might be invalid, private, or the API failed.")
            return

        num_files = len(terabox_data['contents'])
        folder_title = terabox_data.get('title', 'Terabox Content')
        await update_tg_status_message(status_msg,
            f"✅ Link processed!\n"
            f"**Title:** {folder_title}\n"
            f"**Files Found:** {num_files}\n"
            f"Starting downloads..." 
        )

        temp_dir = "./temp_downloads" 

        for i_loop, file_info in enumerate(terabox_data["contents"]): 
            direct_url = file_info["url"]
            original_filename = file_info["filename"]
            filename = re.sub(r'[<>:"/\\|?*]', '_', original_filename)[:200] # Sanitize and shorten
            if '.' not in filename and '.' in direct_url: # Try to get extension
                try:
                    path_part = urlparse(direct_url).path
                    potential_ext = os.path.splitext(path_part)[1]
                    if potential_ext and 1 < len(potential_ext) < 7: filename += potential_ext
                except Exception: pass
            if not filename: filename = f"file_{i_loop+1}" # Fallback filename

            temp_file_path = None 
            downloaded_size_bytes = 0
            download_method_used = ""
            download_start_time = datetime.now() 

            try:
                if ARIA2_ENABLED and aria2_client:
                    download_method_used = "Aria2"
                    initial_aria_status_text = f"⏳ Preparing download for **{filename}** ({i_loop+1}/{num_files}) via Aria2..."
                    await update_tg_status_message(status_msg, initial_aria_status_text)
                    
                    logger.info(f"Adding download to aria2: {filename} from {direct_url}")
                    aria2_download = aria2_client.add_uris([direct_url], options={'dir': temp_dir, 'out': filename})
                    
                    last_status_update_time_loop = time.time() # Renamed
                    while not aria2_download.is_complete and not aria2_download.has_error:
                        aria2_download.update() 
                        current_time_loop_inner = time.time() # Renamed
                        if current_time_loop_inner - last_status_update_time_loop > 2.0: # Update status message every 2s for Aria2
                            prog_percent = aria2_download.progress
                            completed_len_bytes = aria2_download.completed_length
                            total_len_bytes = aria2_download.total_length
                            dl_speed_str = aria2_download.download_speed_string() # This is a method call
                            eta_str = aria2_download.eta_string() # This is a method call
                            aria2_dl_name = filename # Use our sanitized filename for consistency

                            elapsed_time_delta = datetime.now() - download_start_time
                            elapsed_minutes, elapsed_seconds = divmod(int(elapsed_time_delta.total_seconds()), 60)
                            
                            progress_bar_filled = "★" * int(prog_percent / 10)
                            progress_bar_empty = "☆" * (10 - int(prog_percent / 10))

                            status_text_aria = (
                                f"┏ ғɪʟᴇɴᴀᴍᴇ: {aria2_dl_name}\n"
                                f"┠ [{progress_bar_filled}{progress_bar_empty}] {prog_percent:.2f}%\n"
                                f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(completed_len_bytes)} ᴏғ {format_size(total_len_bytes)}\n"
                                f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
                                f"┠ ᴇɴɢɪɴᴇ: <b><u>Aria2c v{ARIA2_VERSION_STR}</u></b>\n"
                                f"┠ sᴘᴇᴇᴅ: {dl_speed_str}\n"
                                f"┠ ᴇᴛᴀ: {eta_str} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
                                f"┖ ᴜsᴇʀ: {user_info_for_status}\n"
                            )
                            await update_tg_status_message(status_msg, status_text_aria, parse_mode_val=ParseMode.HTML)
                            last_status_update_time_loop = current_time_loop_inner
                        await asyncio.sleep(0.5) # Check more frequently for faster updates if needed

                    aria2_download.update() 
                    if aria2_download.is_complete:
                        if aria2_download.files:
                            temp_file_path = aria2_download.files[0].path
                            downloaded_size_bytes = aria2_download.completed_length
                            logger.info(f"Aria2 download complete for {aria2_download.name}. Path: {temp_file_path}")
                        else:
                            logger.error(f"Aria2 download for {aria2_download.name} complete but no file path found. GID: {aria2_download.gid}")
                            await update_tg_status_message(status_msg, f"❌ Aria2 download for **{aria2_download.name}** completed but file path is missing.")
                            try: aria2_download.remove(force=True, files=True)
                            except Exception as e_rm_aria: logger.error(f"Error removing problematic aria2 download {aria2_download.gid}: {e_rm_aria}")
                            continue 
                    elif aria2_download.has_error:
                        logger.error(f"Aria2 download error for {aria2_download.name}: {aria2_download.error_message} (Code: {aria2_download.error_code}) GID: {aria2_download.gid}")
                        await update_tg_status_message(status_msg, f"❌ Aria2 download error for **{aria2_download.name}**: {aria2_download.error_message}")
                        try: aria2_download.remove(force=True, files=True)
                        except Exception as e_rm_aria: logger.error(f"Error removing failed aria2 download {aria2_download.gid}: {e_rm_aria}")
                        continue 
                    else: 
                        logger.warning(f"Aria2 download for {aria2_download.name} exited loop unexpectedly. Status: {aria2_download.status}")
                        await update_tg_status_message(status_msg, f"⚠️ Unknown Aria2 download issue for **{aria2_download.name}**.")
                        try: aria2_download.remove(force=True, files=True)
                        except Exception as e_rm_aria: logger.error(f"Error removing unknown-state aria2 download {aria2_download.gid}: {e_rm_aria}")
                        continue
                
                else: 
                    download_method_used = "HTTPX"
                    if not ARIA2_ENABLED: logger.info(f"Aria2 disabled, using HTTPX for {filename}")
                    elif aria2_client is None: logger.info(f"Aria2 client not connected or aria2p missing, using HTTPX for {filename}")
                    
                    initial_httpx_status_text = f"Downloading **{filename}** ({i_loop+1}/{num_files}) via HTTPX..."
                    await update_tg_status_message(status_msg, initial_httpx_status_text)
                    
                    temp_file_path = os.path.join(temp_dir, filename) 
                    last_status_update_time_loop = time.time()
                    
                    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client: 
                        async with client.stream("GET", direct_url, timeout=httpx.Timeout(60.0, connect=30.0)) as response: 
                            response.raise_for_status()
                            total_size_bytes = int(response.headers.get('content-length', 0))
                            with open(temp_file_path, "wb") as f_httpx: # Renamed f to f_httpx
                                async for chunk in response.aiter_bytes(chunk_size=131072): 
                                    if not chunk: continue
                                    f_httpx.write(chunk)
                                    downloaded_size_bytes += len(chunk)
                                    current_time_loop_inner = time.time()
                                    if current_time_loop_inner - last_status_update_time_loop > 2.0: # Update status message every 2s for HTTPX
                                        percentage = (downloaded_size_bytes / total_size_bytes * 100) if total_size_bytes > 0 else 0
                                        
                                        elapsed_time_delta = datetime.now() - download_start_time
                                        elapsed_minutes, elapsed_seconds = divmod(int(elapsed_time_delta.total_seconds()), 60)
                                        
                                        # Simplified progress bar for HTTPX as it doesn't have rich ETA/speed like Aria2
                                        progress_bar_filled = "★" * int(percentage / 10)
                                        progress_bar_empty = "☆" * (10 - int(percentage / 10))

                                        status_text_httpx = (
                                            f"┏ ғɪʟᴇɴᴀᴍᴇ: {filename}\n"
                                            f"┠ [{progress_bar_filled}{progress_bar_empty}] {percentage:.2f}%\n"
                                            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(downloaded_size_bytes)} ᴏғ {format_size(total_size_bytes)}\n"
                                            f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
                                            f"┠ ᴇɴɢɪɴᴇ: <b><u>HTTPX Fallback</u></b>\n"
                                            # Speed and ETA are harder to calculate accurately here without more complex logic
                                            f"┠ ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
                                            f"┖ ᴜsᴇʀ: {user_info_for_status}\n"
                                        )
                                        await update_tg_status_message(status_msg, status_text_httpx, parse_mode_val=ParseMode.HTML)
                                        last_status_update_time_loop = current_time_loop_inner
                    logger.info(f"HTTPX download complete for {filename}. Size: {downloaded_size_bytes}")

                if not temp_file_path or not os.path.exists(temp_file_path):
                    logger.error(f"File {filename} was not found at expected path after download ({download_method_used}). Path: {temp_file_path}")
                    await update_tg_status_message(status_msg, f"❌ Error: Downloaded file **{filename}** not found.")
                    continue

                final_file_size_on_disk = os.path.getsize(temp_file_path)
                upload_prep_text = f"✅ Downloaded **{filename}** ({format_size(final_file_size_on_disk)} via {download_method_used}).\nNow preparing to upload..."
                await update_tg_status_message(status_msg, upload_prep_text)

                if final_file_size_on_disk > 2 * 1024 * 1024 * 1024: 
                    error_large_file = f"❌ File **{filename}** is too large ({format_size(final_file_size_on_disk)}) to upload. Max is 2GB for bot uploads."
                    await update_tg_status_message(status_msg, error_large_file)
                    if DUMP_CHANNEL_ID:
                         await context.bot.send_message(DUMP_CHANNEL_ID, f"Failed to upload: {filename} (too large: {format_size(final_file_size_on_disk)}) from user {user_id_for_status}. Link: {url_to_process}")
                    continue

                caption_text = f"**{filename}**\n\n**Size:** {format_size(final_file_size_on_disk)}\n\n"
                if terabox_data.get("is_folder") and num_files > 1: caption_text += f"**Folder:** {folder_title}\n"
                caption_text += f"Processed by @{context.bot.username}"
                file_ext = os.path.splitext(filename)[1].lower()
                sent_message = None
                upload_timeout_seconds = max(120, min(3600, int(final_file_size_on_disk / (10 * 1024 * 1024 / 60) ) )) # Adjusted for potentially slower uploads, max 1hr

                # Upload status (simplified, as PTB doesn't have a direct progress callback for sending like Pyrogram)
                upload_status_text = (
                    f"┏ ғɪʟᴇɴᴀᴍᴇ: {filename}\n"
                    f"┠ sᴛᴀᴛᴜs: 📤 Uploading to Telegram...\n"
                    f"┠ sɪᴢᴇ: {format_size(final_file_size_on_disk)}\n"
                    f"┖ ᴜsᴇʀ: {user_info_for_status}\n"
                )
                await update_tg_status_message(status_msg, upload_status_text, parse_mode_val=ParseMode.HTML)


                with open(temp_file_path, "rb") as doc_to_send:
                    send_kwargs = {
                        "chat_id": target_chat_id_for_files, "caption": caption_text, "filename": filename,
                        "parse_mode": ParseMode.MARKDOWN, "request_timeout": upload_timeout_seconds + 60, 
                        "connect_timeout": 30, "read_timeout": upload_timeout_seconds
                    }
                    if file_ext in ['.mp4', '.mkv', '.mov', '.avi', '.webm'] and final_file_size_on_disk < 2 * 1024 * 1024 * 1024: 
                        sent_message = await context.bot.send_video(video=doc_to_send, supports_streaming=True, **send_kwargs)
                    elif file_ext in ['.mp3', '.ogg', '.wav', '.flac', '.m4a'] and final_file_size_on_disk < 2 * 1024 * 1024 * 1024: 
                        sent_message = await context.bot.send_audio(audio=doc_to_send, **send_kwargs)
                    elif final_file_size_on_disk < 2 * 1024 * 1024 * 1024 : 
                        sent_message = await context.bot.send_document(document=doc_to_send, **send_kwargs)
                
                if sent_message:
                    success_msg_text = f"✅ Successfully uploaded **{filename}**!"
                    if target_chat_id_for_files == update.message.chat_id: # If sending to user directly
                        await update_tg_status_message(status_msg, success_msg_text)
                    else: # If sending to dump channel
                        await update.message.reply_text(success_msg_text, parse_mode=ParseMode.MARKDOWN) 
                        # Optionally update status_msg in user chat if it's different
                        if status_msg.chat_id == update.message.chat_id : 
                           await update_tg_status_message(status_msg, success_msg_text) 
                else:
                    await update_tg_status_message(status_msg, f"⚠️ Could not upload **{filename}**. The bot might lack permissions or an unknown error occurred during upload.")

            except httpx.HTTPStatusError as e: 
                logger.error(f"HTTP error downloading {filename} from {direct_url} (HTTPX): {e.response.status_code} - {e.response.text[:100]}", exc_info=True)
                await update_tg_status_message(status_msg, f"❌ HTTP error downloading **{filename}** (HTTPX): Status {e.response.status_code}. Link might have expired or is invalid.")
            except httpx.ReadTimeout as e: 
                logger.error(f"Read timeout downloading {filename} from {direct_url} (HTTPX): {e}", exc_info=True)
                await update_tg_status_message(status_msg, f"❌ Read timeout downloading **{filename}** (HTTPX). The connection was too slow or interrupted.")
            except httpx.RequestError as e: 
                logger.error(f"Network error downloading {filename} (HTTPX): {e}", exc_info=True)
                await update_tg_status_message(status_msg, f"❌ Network error downloading **{filename}** (HTTPX): {str(e)[:100]}")
            except aria2p.client.ClientException as e_aria_client: 
                 logger.error(f"Aria2 ClientException for {filename}: {e_aria_client}", exc_info=True)
                 await update_tg_status_message(status_msg, f"❌ Aria2 Client error for **{filename}**: {str(e_aria_client)[:150]}. Ensure Aria2c is running and configured.")
            except Exception as e: 
                logger.error(f"Error with file {filename} (URL: {direct_url}, Method: {download_method_used}): {e}", exc_info=True)
                await update_tg_status_message(status_msg, f"❌ An error occurred with **{filename}**: {str(e)[:100]}")
            finally:
                if temp_file_path and os.path.exists(temp_file_path):
                    try: 
                        os.remove(temp_file_path)
                        logger.info(f"Removed temp file: {temp_file_path}")
                    except Exception as e_rm: 
                        logger.error(f"Failed to remove temp file {temp_file_path}: {e_rm}")
                if download_method_used == "Aria2" and 'aria2_download' in locals() and aria2_download:
                    try:
                        # Attempt to remove the download from Aria2, especially if it failed or wasn't completed.
                        if not aria2_download.is_complete or aria2_download.has_error or not temp_file_path: 
                            logger.info(f"Attempting to remove GID {aria2_download.gid} from Aria2 due to error or incompletion.")
                            aria2_download.remove(force=True, files=True) 
                    except Exception as e_aria_clean:
                        logger.warning(f"Could not clean up GID {aria2_download.gid if 'aria2_download' in locals() and aria2_download else 'N/A'} from Aria2: {e_aria_clean}")

        final_completion_message = f"🏁 All {num_files} file(s) from '{folder_title}' processed."
        if status_msg and status_msg.chat_id == update.message.chat_id: # Check if status_msg is valid
            await update_tg_status_message(status_msg, final_completion_message)
        else: # If status_msg was deleted or is not in user chat, send a new reply
            await update.message.reply_text(final_completion_message)

    except DirectDownloadLinkException as e:
        logger.warning(f"DirectDownloadLinkException for {url_to_process}: {e}")
        if status_msg: await update_tg_status_message(status_msg, f"❌ Error processing link: {e}")
    except Exception as e: 
        logger.error(f"Unhandled error processing link {url_to_process}: {e}", exc_info=True)
        if status_msg: await update_tg_status_message(status_msg, f"❌ An unexpected error occurred. Please try again later or check the link.\nError: {str(e)[:100]}")
    finally:
        # Clean up last edit time from chat_data if status_msg exists
        if status_msg and context:
            context.chat_data.pop(f"last_edit_time_{status_msg.message_id}", None)


# === Main Application Setup ===
def run_bot():
    _initialize_config() 
    initialize_aria2() 

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. Exiting.")
        return

    # Consider increasing default pool size for httpx if it's used extensively for link fetching
    # and if fetch_terabox_links becomes async.
    # For now, PTB's concurrency is the main focus.
    application_builder = Application.builder().token(BOT_TOKEN)
    
    # Configure concurrent_updates and connection_pool_size for better handling of multiple users
    # These values can be tuned based on server resources and expected load.
    application_builder.concurrent_updates(10) # Handle up to 10 updates concurrently
    application_builder.connection_pool_size(512) # Default is 512, usually fine.

    application = application_builder.build()


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
    # Removed concurrent_updates from run_polling as it's set in builder now
    application.run_polling(allowed_updates=Update.ALL_TYPES) 

if __name__ == "__main__":
    temp_dir = "./temp_downloads"
    if not os.path.exists(temp_dir):
        try:
            os.makedirs(temp_dir)
            logger.info(f"Created temporary directory: {temp_dir}")
        except OSError as e:
            logger.error(f"Could not create temp_downloads directory: {e}. Files will be downloaded to current directory if writable.")
    run_bot()
