"""Guarded real migration child, usable with either exact candidate runtime."""

from network import install_guard, self_check

install_guard([("127.0.0.1", 55438)])
self_check()
from alembic import command
from alembic.config import Config

command.upgrade(Config("alembic.ini"), "head")
