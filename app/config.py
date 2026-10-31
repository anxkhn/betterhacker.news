import os

from pydantic import BaseModel


class LogConfig(BaseModel):
    """Logging configuration to be set for the server"""

    LOGGER_NAME: str = "betterhacker"
    LOG_FORMAT: str = "%(levelprefix)s | %(asctime)s | %(message)s"
    LOG_LEVEL: str = "DEBUG"
    LOG_PATH: str = os.environ.get("BETTERHACKER_LOG_PATH", "./bh.log")
    # LOG_PATH: str = os.environ.get("FORTUNE_LOG_PATH", "/code/logs/logfile.log")

    # Logging config
    version = 1
    disable_existing_loggers = False
    formatters = {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": LOG_FORMAT,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    }
    handlers = {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "file": {
            "formatter": "default",
            "class": "logging.FileHandler",
            "filename": LOG_PATH,
        },
    }
    loggers = {
        LOGGER_NAME: {"handlers": ["default", "file"], "level": LOG_LEVEL},
    }
