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
from telegram.error import RetryAfter # For FloodWait equivalent

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
ARIA2_GLOBAL_OPTIONS = { # Options from the example
    "max-tries": "50",
    "retry-wait": "3",
    "continue": "true",
    "allow-overwrite": "true",
    "min-split-size": "4M",
    "split": "10",
    # "max-connection-per-server": "16", # Default is 1, can be increased
    # "max-concurrent-downloads": "5",   # Default is 5
}


# Runtime configuration variables
DUMP_CHANNEL_ID = None
FORCE_SUB_CHANNEL_ID = None 
aria2_client = None
ARIA2_VERSION_STR = "N/A" # To store Aria2c version

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
        current_aria2_client = aria2p.API(
            aria2p.Client(
                host=ARIA2_RPC_HOST,
                port=ARIA2_RPC_PORT,
                secret=ARIA2_RPC_SECRET
            )
        )
        version_info = current_aria2_client.get_version()
        stats = current_aria2_client.get_stats()
        ARIA2_VERSION_STR = version_info.version if version_info else "Unknown"
        logger.info(f"Successfully connected to Aria2 RPC server. Version: {ARIA2_VERSION_STR}, "
                    f"Features: {version_info.enabled_features if version_info else 'N/A'}, "
                    f"Stats: {stats.num_active} active / {stats.num_waiting} waiting / {stats.num_stopped} stopped.")
        
        logger.info(f"Setting Aria2c global options: {ARIA2_GLOBAL_OPTIONS}")
        current_aria2_client.set_global_options(ARIA2_GLOBAL_OPTIONS)
        logger.info("Aria2c global options set successfully.")
        aria2_client = current_aria2_client # Assign to global only on full success
    except Exception as e:
        logger.error(f"Could not connect to Aria2 RPC server or set options at {ARIA2_RPC_HOST}:{ARIA2_RPC_PORT}. "
                     f"Ensure aria2c is running in daemon mode with RPC enabled. Error: {e}")
        aria2_client = None
        ARIA2_VERSION_STR = "Error"

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

# === Terabox Link Fetching Logic (Remains the same) ===
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

    api_endpoints = [
        {
            "name": "tellycloud",
            "api_call_url": f"https://teraboxdl.tellycloudapi.workers.dev/?url={url_for_tellycloud_like_apis}",
            "method": "GET"
        },
        {
            "name": "cheems_robot_dl",
            "api_call_url": f"https://teradlrobot.cheemsbackup.workers.dev/?url={quoted_input_url}",
            "method": "GET"
        },
        {
            "name": "teraboxdownloader_in",
            "api_call_url": f"https://teraboxdownloader.in/api/?url={quoted_input_url}",
            "method": "GET"
        },
        {
            "name": "terabox_app_s_workers",
            "api_call_url": f"https://terabox.app-s.workers.dev/?url={quoted_input_url}",
            "method": "GET"
        },
        {
            "name": "terabox_dl_onrender",
            "api_call_url": f"https://terabox-dl.onrender.com/api/get-info?url={quoted_input_url}&pwd=",
            "method": "GET"
        },
    ]

    common_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
    }

    response_json = None
    successful_api_name = None

    for api_config in api_endpoints:
        api_url_to_call = api_config["api_call_url"]
        current_headers = common_headers.copy()
        logger.info(f"Trying API: {api_config['name']} ({api_url_to_call})")

        try:
            api_response = None
            if api_config["method"] == "GET":
                current_headers["Sec-Fetch-Site"] = "cross-site" 
                api_response = get(api_url_to_call, headers=current_headers, timeout=30, allow_redirects=True)
            else: # POST
                payload_dict = {"url": api_config.get("payload_url", input_url)}
                if api_config.get("needs_json_payload"):
                    current_headers["Content-Type"] = "application/json"
                    api_response = post(api_url_to_call, headers=current_headers, json=payload_dict, timeout=30)
                else:
                    api_response = post(api_url_to_call, headers=current_headers, data=payload_dict, timeout=30)

            api_response.raise_for_status() 
            
            if api_response.url != api_url_to_call and "application/json" not in api_response.headers.get("content-type", "").lower():
                logger.info(f"API {api_config['name']} seems to have redirected to a non-JSON URL: {api_response.url}. Assuming direct link.")
                parsed_final_url = urlparse(api_response.url)
                filename_from_path = os.path.basename(parsed_final_url.path) or f"File_from_{api_config['name']}"
                response_json = { 
                    "direct_link": api_response.url, 
                    "file_name": filename_from_path,
                    "size": api_response.headers.get('content-length', 0)
                }
                successful_api_name = api_config['name'] + " (via redirect)"
                logger.info(f"Successfully processed redirect as direct link from API: {successful_api_name}")
                break

            current_response_json = api_response.json() 

            if (current_response_json.get("Success") and "Data" in current_response_json) or \
               ("response" in current_response_json and isinstance(current_response_json["response"], list) and current_response_json["response"]) or \
               (isinstance(current_response_json, list) and current_response_json and ("downloadLink" in current_response_json[0] or "link" in current_response_json[0])) or \
               (current_response_json.get("list") and isinstance(current_response_json.get("list"), list)) or \
               (current_response_json.get("direct_link")) or \
               (current_response_json.get("url")): 
                response_json = current_response_json
                successful_api_name = api_config['name']
                logger.info(f"Successfully fetched and parsed JSON from API: {successful_api_name}")
                break
            else:
                logger.warning(f"API {api_config['name']} gave OK status but unexpected JSON structure: {str(current_response_json)[:200]}")
                response_json = None

        except RequestException as e:
            logger.error(f"RequestException with API {api_config['name']} ({api_url_to_call}): {e}")
        except ValueError as e: 
            logger.error(f"JSONDecodeError with API {api_config['name']} ({api_url_to_call}): {e}. Response: {api_response.text[:200] if 'api_response' in locals() and api_response else 'N/A'}")
        except Exception as e:
            logger.error(f"Generic error with API {api_config['name']} ({api_url_to_call}): {e}", exc_info=True)

    if not response_json:
        raise DirectDownloadLinkException("ERROR: Unable to fetch valid JSON data or direct link from any API endpoint.")

    logger.info(f"Processing data from successful API: {successful_api_name}")
    details = {"contents": [], "title": "Untitled Terabox Content", "total_size": 0, "is_folder": False}

    if response_json.get("Success") and "Data" in response_json: 
        logger.info(f"Parsing as Structure 1 (Success:True, Data:{{...}}) from API: {successful_api_name}")
        item_data = response_json["Data"]
        title = item_data.get("FileName", item_data.get("title", "Untitled_File"))
        details["title"] = title
        details["total_size"] = item_data.get("FileSizebytes", 0) 
        if isinstance(details["total_size"], str) and details["total_size"].isdigit(): details["total_size"] = int(details["total_size"])
        elif not isinstance(details["total_size"], int):
            file_size_str = item_data.get("FileSize", item_data.get("size"))
            if file_size_str:
                match_mb = re.match(r"([\d.]+)\s*MB", str(file_size_str), re.IGNORECASE)
                match_gb = re.match(r"([\d.]+)\s*GB", str(file_size_str), re.IGNORECASE)
                if match_mb: details["total_size"] = int(float(match_mb.group(1)) * 1024 * 1024)
                elif match_gb: details["total_size"] = int(float(match_gb.group(1)) * 1024 * 1024 * 1024)
                else: details["total_size"] = 0
            else: details["total_size"] = 0
        direct_link = item_data.get("DirectLink") or item_data.get("DirectLink2") or item_data.get("url") or item_data.get("link")
        if direct_link: details["contents"].append({"url": direct_link, "filename": title})
        else: 
            resolutions = item_data.get("resolutions", {})
            if resolutions:
                chosen_link = resolutions.get("HD Video") or resolutions.get("SD Video") or next(iter(resolutions.values()), None)
                if chosen_link: details["contents"].append({"url": chosen_link, "filename": title})
        if not details["contents"]: logger.warning(f"API {successful_api_name} (Struct 1): No direct link or resolution found in Data.")

    elif "response" in response_json and isinstance(response_json["response"], list): 
        logger.info(f"Parsing as Structure 2 (response:[...]) from API: {successful_api_name}")
        response_list = response_json["response"]
        if not response_list: logger.warning(f"API {successful_api_name} (Struct 2): 'response' list is empty.")
        else:
            details["is_folder"] = len(response_list) > 1
            if details["is_folder"]: details["title"] = response_list[0].get("title", "Terabox_Folder") 
            for i_idx, item in enumerate(response_list): # Renamed i to i_idx to avoid conflict
                file_title = item.get("title", f"file_{i_idx+1}")
                if not details["is_folder"] and i_idx==0 : details["title"] = file_title
                direct_link = None
                resolutions = item.get("resolutions", {})
                if resolutions: direct_link = resolutions.get("HD Video") or resolutions.get("Fast Download") or resolutions.get("SD Video") or next(iter(resolutions.values()), None)
                else: direct_link = item.get("url") or item.get("downloadLink") or item.get("link")
                if direct_link:
                    file_size_str = item.get("size", "0") 
                    try:
                        if isinstance(file_size_str, (int,float)): details["total_size"] += int(file_size_str)
                    except ValueError: pass
                    details["contents"].append({"url": direct_link, "filename": file_title})
            if not details["contents"]: logger.warning(f"API {successful_api_name} (Struct 2): No usable links found in 'response' list.")
    
    elif response_json.get("direct_link") and response_json.get("file_name"): 
        logger.info(f"Parsing as Structure 3 (direct_link, file_name) from API: {successful_api_name}")
        details["title"] = response_json.get("file_name")
        details["contents"].append({"url": response_json["direct_link"], "filename": response_json.get("file_name")})
        try: details["total_size"] = int(response_json.get("file_size_bytes", response_json.get("size", 0)))
        except (ValueError, TypeError): logger.warning(f"Could not parse file_size_bytes for API {successful_api_name} (Struct 3)")

    elif isinstance(response_json, list) and response_json: 
        logger.info(f"Parsing as Structure 4 (list of objects) from API: {successful_api_name}")
        details["is_folder"] = len(response_json) > 1
        details["title"] = "Terabox_Folder" if details["is_folder"] else response_json[0].get("name", "Terabox_File")
        for i_idx, item in enumerate(response_json): # Renamed i to i_idx
            direct_link = item.get("downloadLink") or item.get("url") or item.get("link")
            filename_val = item.get("name") or item.get("filename", f"file_{i_idx+1}") # Renamed filename to filename_val
            if direct_link:
                details["contents"].append({"url": direct_link, "filename": filename_val})
                try: details["total_size"] += int(item.get("size", 0))
                except (ValueError, TypeError): pass 
        if not details["contents"]: logger.warning(f"API {successful_api_name} (Struct 4): No usable links found in list.")
    
    elif response_json.get("url") and response_json.get("filename"):
        logger.info(f"Parsing as simple {{'url': ..., 'filename': ...}} from API: {successful_api_name}")
        details["title"] = response_json.get("filename")
        details["contents"].append({"url": response_json["url"], "filename": response_json.get("filename")})
        try: details["total_size"] = int(response_json.get("size", 0))
        except (ValueError, TypeError): logger.warning(f"Could not parse size for simple API {successful_api_name}")

    else: 
        logger.warning(f"Unhandled JSON structure from API {successful_api_name}. Attempting most generic parse: {str(response_json)[:300]}")
        if isinstance(response_json, dict):
            dl_url = response_json.get("url") or response_json.get("direct_link") or response_json.get("downloadLink")
            dl_name = response_json.get("filename") or response_json.get("name") or response_json.get("title", "Untitled_File_Generic")
            if dl_url:
                details["title"] = dl_name
                details["contents"].append({"url": dl_url, "filename": dl_name})
                logger.info("Most generic fallback parse: Found a potential link.")
            else:
                 logger.error(f"Most generic fallback parse failed for API {successful_api_name}. No common link keys found.")
                 raise DirectDownloadLinkException("ERROR: Unhandled or invalid API response structure after all fallbacks.")
        else:
            logger.error(f"Most generic fallback parse failed for API {successful_api_name}. Response is not a dictionary.")
            raise DirectDownloadLinkException("ERROR: Unhandled API response type (not a dict) after all fallbacks.")

    if not details["contents"]:
        logger.error(f"No valid download links found after processing JSON from {successful_api_name}. JSON: {str(response_json)[:300]}")
        raise DirectDownloadLinkException("ERROR: No valid download links extracted.")

    logger.info(f"Successfully processed. Found {len(details['contents'])} items. Title: {details['title']}")
    return details

# === Helper Functions ===
def format_size(size_in_bytes: int) -> str: # Replaced get_readable_file_size
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

# === Admin Commands (Mostly unchanged, view_config updated for Aria2 version) ===
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

# === User Command Handlers (Unchanged) ===
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
    """Safely updates a Telegram message, handling RetryAfter."""
    if status_msg_obj.text == text_to_send: # Avoid editing if text is the same
        return
    while True:
        try:
            await status_msg_obj.edit_text(text_to_send, parse_mode=parse_mode_val)
            break 
        except RetryAfter as e:
            logger.warning(f"Flood control: waiting for {e.retry_after} seconds before retrying status update.")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error(f"Failed to update status message: {e}", exc_info=True)
            break # Break on other errors

# === Message Handler for Terabox Links (Updated for Aria2 Status) ===
async def handle_terabox_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DUMP_CHANNEL_ID, aria2_client, ARIA2_VERSION_STR
    if not update.message or not update.message.text: return
    if not await check_subscription(update, context): return

    message_text = update.message.text
    terabox_link_pattern = r"https?://(?:www\.)?(?:terabox|freeterabox|teraboxapp|1024tera|nephobox|mirrobox|4funbox|momerybox|terabox\.app|gibibox|goaibox|terasharelink|1024terabox|teraboxshare)\.(?:com|app|link|me|xyz|cloud|fun|online|store|shop|top|pw|org|net|info|mobi|asia|vip|pro|life|live|world|space|tech|site|icu|cyou|buzz|gallery|website|press|services|show|run|gold|plus|guru|center|group|company|directory|today|digital|network|solutions|systems|technology|software|click|store|shop|ninja|money|pics|lol|tube|pictures|cam|vin|art|blog|best|fans|media|game|video|stream|movie|film|music|audio|cloud|drive|share|storage|file|data|download|backup|upload|box|disk)\S+"
    match = re.search(terabox_link_pattern, message_text, re.IGNORECASE)
    
    if not match:
        if not (message_text.startswith('/') or len(message_text.split()) > 10): 
            await update.message.reply_text("Please send a valid Terabox link. If you need help, type /help.")
        return

    url_to_process = match.group(0)
    status_msg = await update.message.reply_text(f"🔄 Processing Terabox link: {url_to_process[:50]}...")
    target_chat_id_for_files = DUMP_CHANNEL_ID if DUMP_CHANNEL_ID else update.message.chat_id
    user_info_for_status = f"<a href='tg://user?id={update.effective_user.id}'>{update.effective_user.first_name}</a> | ID: {update.effective_user.id}"


    try:
        loop = asyncio.get_event_loop()
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

        for i_loop, file_info in enumerate(terabox_data["contents"]): # Renamed i to i_loop
            direct_url = file_info["url"]
            original_filename = file_info["filename"]
            filename = re.sub(r'[<>:"/\\|?*]', '_', original_filename)[:200]
            if '.' not in filename and '.' in direct_url:
                try:
                    path_part = urlparse(direct_url).path
                    potential_ext = os.path.splitext(path_part)[1]
                    if potential_ext and 1 < len(potential_ext) < 7: filename += potential_ext
                except Exception: pass
            if not filename: filename = f"file_{i_loop+1}"

            temp_file_path = None 
            downloaded_size = 0
            download_method_used = ""
            download_start_time = datetime.now() # For elapsed time calculation

            try:
                if ARIA2_ENABLED and aria2_client:
                    download_method_used = "Aria2"
                    initial_aria_status_text = f"⏳ Preparing download for **{filename}** ({i_loop+1}/{num_files}) via Aria2..."
                    await update_tg_status_message(status_msg, initial_aria_status_text)
                    
                    logger.info(f"Adding download to aria2: {filename} from {direct_url}")
                    # Pass sanitized filename to 'out' option for Aria2
                    aria2_download = aria2_client.add_uris([direct_url], options={'dir': temp_dir, 'out': filename})
                    
                    last_status_update_time = time.time()
                    while not aria2_download.is_complete and not aria2_download.has_error:
                        aria2_download.update() 
                        current_time_loop = time.time() # Renamed current_time
                        if current_time_loop - last_status_update_time > 2.5: # Update status message every 2.5s
                            prog_percent = aria2_download.progress
                            completed_len_bytes = aria2_download.completed_length
                            total_len_bytes = aria2_download.total_length
                            dl_speed_str = aria2_download.download_speed_string()
                            eta_str = aria2_download.eta_string()
                            aria2_dl_name = aria2_download.name # Use name from Aria2

                            elapsed_time_delta = datetime.now() - download_start_time
                            elapsed_minutes, elapsed_seconds = divmod(elapsed_time_delta.seconds, 60)
                            
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
                            await update_tg_status_message(status_msg, status_text_aria, parse_mode_val=ParseMode.HTML) # Use HTML for user link
                            last_status_update_time = current_time_loop
                        await asyncio.sleep(1) # Check every second

                    aria2_download.update() 
                    if aria2_download.is_complete:
                        if aria2_download.files:
                            temp_file_path = aria2_download.files[0].path
                            downloaded_size = aria2_download.completed_length
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
                    
                    current_file_progress_text = f"Downloading **{filename}** ({i_loop+1}/{num_files}) via HTTPX..."
                    await update_tg_status_message(status_msg, current_file_progress_text)
                    
                    temp_file_path = os.path.join(temp_dir, filename) 
                    last_status_update_time = time.time() # Renamed last_update_time
                    
                    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client: 
                        async with client.stream("GET", direct_url, timeout=httpx.Timeout(60.0, connect=30.0)) as response: 
                            response.raise_for_status()
                            total_size_bytes = int(response.headers.get('content-length', 0))
                            with open(temp_file_path, "wb") as f:
                                async for chunk in response.aiter_bytes(chunk_size=131072): 
                                    if not chunk: continue
                                    f.write(chunk)
                                    downloaded_size += len(chunk)
                                    current_time_loop = time.time() # Renamed current_time
                                    if current_time_loop - last_status_update_time > 2.5: 
                                        percentage = (downloaded_size / total_size_bytes * 100) if total_size_bytes > 0 else 0
                                        progress_bar_str = "█" * int(percentage / 5) + "░" * (20 - int(percentage / 5))
                                        status_text_httpx = (
                                            f"{current_file_progress_text}\n"
                                            f"`{progress_bar_str}`\n"
                                            f"{format_size(downloaded_size)} / {format_size(total_size_bytes)} ({percentage:.1f}%)"
                                        )
                                        await update_tg_status_message(status_msg, status_text_httpx)
                                        last_status_update_time = current_time_loop
                    logger.info(f"HTTPX download complete for {filename}. Size: {downloaded_size}")

                if not temp_file_path or not os.path.exists(temp_file_path):
                    logger.error(f"File {filename} was not found at expected path after download ({download_method_used}). Path: {temp_file_path}")
                    await update_tg_status_message(status_msg, f"❌ Error: Downloaded file **{filename}** not found.")
                    continue

                final_file_size_on_disk = os.path.getsize(temp_file_path)
                final_progress_text = f"✅ Downloaded **{filename}** ({format_size(final_file_size_on_disk)} via {download_method_used}).\nNow uploading..."
                await update_tg_status_message(status_msg, final_progress_text)

                if final_file_size_on_disk > 2 * 1024 * 1024 * 1024: 
                    error_large_file = f"❌ File **{filename}** is too large ({format_size(final_file_size_on_disk)}) to upload. Max is 2GB."
                    await update_tg_status_message(status_msg, error_large_file)
                    if DUMP_CHANNEL_ID:
                         await context.bot.send_message(DUMP_CHANNEL_ID, f"Failed to upload: {filename} (too large: {format_size(final_file_size_on_disk)}) from user {update.effective_user.id} ({update.effective_user.username or 'no_username'}). Link: {url_to_process}")
                    continue

                caption_text = f"**{filename}**\n\n**Size:** {format_size(final_file_size_on_disk)}\n\n"
                if terabox_data.get("is_folder") and num_files > 1: caption_text += f"**Folder:** {folder_title}\n"
                caption_text += f"Processed by @{context.bot.username}"
                file_ext = os.path.splitext(filename)[1].lower()
                sent_message = None
                upload_timeout_seconds = max(120, min(3000, int(final_file_size_on_disk / (25 * 1024 * 1024 / 60) ) )) 

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
                    if target_chat_id_for_files == update.message.chat_id:
                        await update_tg_status_message(status_msg, success_msg_text)
                    else: 
                        await update.message.reply_text(success_msg_text, parse_mode=ParseMode.MARKDOWN) 
                        if status_msg.chat_id == update.message.chat_id : 
                           await update_tg_status_message(status_msg, success_msg_text) 
                else:
                    await update_tg_status_message(status_msg, f"⚠️ Could not upload **{filename}**. The bot might lack permissions or an unknown error occurred.")

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
                        if not aria2_download.is_complete or aria2_download.has_error or not temp_file_path: 
                            logger.info(f"Attempting to remove GID {aria2_download.gid} from Aria2 due to error or incompletion.")
                            aria2_download.remove(force=True, files=True) 
                    except Exception as e_aria_clean:
                        logger.warning(f"Could not clean up GID {aria2_download.gid if 'aria2_download' in locals() and aria2_download else 'N/A'} from Aria2: {e_aria_clean}")

        final_completion_message = f"🏁 All {num_files} file(s) from '{folder_title}' processed."
        if status_msg.chat_id == update.message.chat_id:
            await update_tg_status_message(status_msg, final_completion_message)
        else: await update.message.reply_text(final_completion_message)

    except DirectDownloadLinkException as e:
        logger.warning(f"DirectDownloadLinkException for {url_to_process}: {e}")
        await update_tg_status_message(status_msg, f"❌ Error processing link: {e}")
    except Exception as e: 
        logger.error(f"Unhandled error processing link {url_to_process}: {e}", exc_info=True)
        await update_tg_status_message(status_msg, f"❌ An unexpected error occurred. Please try again later or check the link.\nError: {str(e)[:100]}")

# === Main Application Setup ===
def run_bot():
    _initialize_config() 
    initialize_aria2() 

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. Exiting.")
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
    temp_dir = "./temp_downloads"
    if not os.path.exists(temp_dir):
        try:
            os.makedirs(temp_dir)
            logger.info(f"Created temporary directory: {temp_dir}")
        except OSError as e:
            logger.error(f"Could not create temp_downloads directory: {e}. Files will be downloaded to current directory if writable.")
    run_bot()
