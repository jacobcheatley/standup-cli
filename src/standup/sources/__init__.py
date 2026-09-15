from typing import Any

from standup.sources import ado, github, linear, reminders
from standup.sources.base import Source

SOURCES: list[Source[Any]] = [reminders.source, github.source, ado.source, linear.source]
