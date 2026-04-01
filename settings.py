import pathlib
import os
import logging

from dotenv import load_dotenv
from logging.config import dictConfig

load_dotenv()

DISCORD_API_TOKEN = os.getenv("DISCORD_API_TOKEN")

BASE_DIR = pathlib.Path(__file__).parent
COGS_DIR = BASE_DIR / "cogs"

SPECIAL_ROLES = {
    "Showdown 1": 1488795172480356473,
    "Showdown 2": 1488830429686665246,
    "BA1": 1488835134101655652,
    "BA2": 1488835215949434891,
}
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))

# Activity Tracking Settings
ACTIVITY_LEADER_ROLE_ID = 1488837755189461132  # The role ID for "Most Active" member
ACTIVITY_BLACKLIST_CHANNELS = [
    # Add channel IDs here where messages should NOT earn points
    # Example: 123456789012345678
    1443104374837608529,
    1459164230832885803,
    1458536899692990494,
    1470865841779249329,
    1443079705866797217,
    1442857104250634363,
    1442857208462311524,
    1482369748015513630,
    1482760154414842120,
    1463479585567150194,
]

class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',    # Blue
        'INFO': '\033[92m',     # Green
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'CRITICAL': '\033[95m', # Magenta
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"
    
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)-10s - %(asctime)s - %(module)-15s : %(message)s",
        },
        "standard": {
            "format": "%(levelname)-10s - %(name)-15s : %(message)s",
        },
        "colored": {
            "()": ColoredFormatter,
            "format": "%(levelname)-10s - %(name)-15s : %(message)s",
        }
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "colored",
            "stream": "ext://sys.stdout",
        },
        "console2": {
            "level": "WARNING",
            "class": "logging.StreamHandler",
            "formatter": "colored",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "level": "INFO",
            "class": "logging.FileHandler",
            "filename": "logs/infos.log",
            "formatter": "verbose",
            "mode": "w",
            "encoding": "utf-8",
        },        
    },
    "loggers": {
        "bot": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False
        },
        "discord": {
            "handlers": ["console2", "file"],
            "level": "INFO",
            "propagate": False
        }
    }
}

logger = logging.getLogger("bot")

dictConfig(LOGGING_CONFIG)