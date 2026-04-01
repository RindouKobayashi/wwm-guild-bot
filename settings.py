import pathlib
import os
import logging

from dotenv import load_dotenv
from logging.config import dictConfig

BASE_DIR = pathlib.Path(__file__).parent
COGS_DIR = BASE_DIR / "cogs"

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