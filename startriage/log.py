import logging


def log_setup(setting: int, default: int = 1):
    """
    Perform setup for the logger.
    Run before any logging.log thingy is called.

    if setting is 0: the default is used, which is WARNING.
    else: setting + default is used.
    """

    levels = (logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG, logging.NOTSET)

    factor = clamp(default + setting, 0, len(levels) - 1)
    level = levels[factor]

    logging.basicConfig(level=level, format="[%(asctime)s] [%(name)s] %(message)s")
    # "%(levelname) -10s %(asctime)s %(name) -20s %(funcName) " "-25s : %(message)s"
    logging.captureWarnings(True)

    logging.getLogger("asyncio").setLevel(level=logging.WARNING)


def clamp(number: int, smallest: int, largest: int) -> int:
    """return number but limit it to the inclusive given value range"""
    return max(smallest, min(number, largest))
