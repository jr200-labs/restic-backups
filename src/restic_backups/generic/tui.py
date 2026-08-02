"""Shared TUI prompt behavior."""

from __future__ import annotations

from typing import Any, cast

import questionary
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from questionary.question import Question


def select(*args: Any, **kwargs: Any) -> Question:
    """Create a selection prompt with Escape bound to back."""
    question = questionary.select(*args, **kwargs)
    bindings = cast(KeyBindings, question.application.key_bindings)

    @bindings.add(Keys.Escape, eager=True)
    def go_back(event: Any) -> None:
        event.app.exit(result=None)

    return question
