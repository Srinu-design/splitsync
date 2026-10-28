import logging
import sys

# DECISION: Dedicated logger for JSON request access logs honoring LOG_LEVEL.
logger = logging.getLogger("splitsync.access")


def setup_logging(log_level: str = "INFO") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
