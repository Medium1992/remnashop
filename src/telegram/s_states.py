"""Dialog states of the "s" edition, apart from upstream's ``states.py``."""

from aiogram.fsm.state import State, StatesGroup


class SDashboard(StatesGroup):
    MAIN = State()
    SLOTS = State()
    VERSIONS = State()
    CHANNEL = State()
    STALE = State()
    INACTIVE = State()
    INACTIVE_LIST = State()
    BLOCKED = State()
    ROUTERS = State()
    NUMBER = State()
    TARGET = State()
    CONFIRM_SEND = State()


class SBlock(StatesGroup):
    MAIN = State()
