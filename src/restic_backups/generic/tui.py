"""Shared TUI prompt behavior."""

from __future__ import annotations

from typing import Any, cast

import questionary
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from questionary.question import Question

TUI_STYLE = Style(
    [
        ("control", "fg:ansiwhite bold"),
        ("disabled", "fg:ansibrightblack"),
        ("pointer", "fg:ansiwhite bold"),
        ("separator", "fg:ansibrightblack"),
    ]
)
CONTROL_VALUES = {"back", "cancel", "exit", "help"}


def menu_choice(
    label: str,
    description: str,
    value: str,
    width: int,
    *,
    disabled: str | None = None,
) -> questionary.Choice:
    label_style = (
        "class:disabled"
        if disabled
        else "class:control"
        if value in CONTROL_VALUES
        else "fg:ansicyan bold"
    )
    description_style = "class:disabled" if disabled else ""
    return questionary.Choice(
        [
            (label_style, f"{label:<{width}}  "),
            (description_style, description),
        ],
        value,
        disabled=disabled,
    )


def checkbox(*args: Any, **kwargs: Any) -> Question:
    """Create a checkbox with concise, consistent navigation help."""
    kwargs.setdefault("instruction", "(Use arrow keys to move, <space> to select)")
    kwargs.setdefault("style", TUI_STYLE)
    return questionary.checkbox(*args, **kwargs)


def select(*args: Any, **kwargs: Any) -> Question:
    """Create a selection prompt with Escape bound to back."""
    kwargs.setdefault("instruction", " ")
    kwargs.setdefault("style", TUI_STYLE)
    question = questionary.select(*args, **kwargs)
    bindings = cast(KeyBindings, question.application.key_bindings)

    @bindings.add(Keys.Escape, eager=True)
    def go_back(event: Any) -> None:
        event.app.exit(result=None)

    return question
