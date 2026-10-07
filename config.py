import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Telegram Bot Token
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Allowed Telegram user IDs (comma-separated, e.g. "12345678,98765432")
_raw_allowed = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS: set[int] = set()
if _raw_allowed:
    for item in _raw_allowed.replace(";", ",").split(","):
        item = item.strip()
        if item.isdigit():
            ALLOWED_USERS.add(int(item))

# Auto-detect agy executable path
def find_agy_path() -> str:
    env_path = os.getenv("AGY_PATH", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    
    which_path = shutil.which("agy")
    if which_path:
        return which_path

    # Common Windows installation path
    local_app_data = os.getenv("LOCALAPPDATA", "")
    if local_app_data:
        default_win_path = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.exists(default_win_path):
            return default_win_path

    user_profile = os.getenv("USERPROFILE", "")
    if user_profile:
        fallback_win_path = os.path.join(user_profile, "AppData", "Local", "agy", "bin", "agy.exe")
        if os.path.exists(fallback_win_path):
            return fallback_win_path

    return "agy"

AGY_PATH = find_agy_path()

# Default workspace directory where agy can inspect files and execute commands
DEFAULT_WORKING_DIR = os.getenv("DEFAULT_WORKING_DIR", str(BASE_DIR)).strip()
if not os.path.exists(DEFAULT_WORKING_DIR):
    DEFAULT_WORKING_DIR = str(BASE_DIR)

# Default model
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-3.8-flash-high").strip()

# Auto-approve tool permissions (e.g. file editing, running commands)
AUTO_APPROVE = os.getenv("AUTO_APPROVE", "true").lower() in ("true", "1", "yes")

# Database path for user sessions
DB_PATH = DATA_DIR / "sessions.db"
