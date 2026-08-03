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


def group_disabled_choices(
    available: list[questionary.Choice | questionary.Separator],
    disabled: list[tuple[str, str]],
    *,
    heading: str,
    label_width: int,
) -> list[questionary.Choice | questionary.Separator]:
    """Put unavailable choices in one clearly delimited grey section."""
    if not disabled:
        return available
    disabled_width = max(len(label) for label, _ in disabled)
    rendered = [f"{label:<{disabled_width}}  ({reason})" for label, reason in disabled]
    rule_width = max(32, label_width + 18, *(len(label) + 2 for label in rendered))
    return [
        *available,
        questionary.Separator(f" {heading} ".center(rule_width, "─")),
        *(questionary.Separator(f"  {label}") for label in rendered),
        questionary.Separator("─" * rule_width),
    ]


def menu_choice(
    label: str,
    description: str,
    value: str,
    width: int,
) -> questionary.Choice:
    label_style = "class:control" if value in CONTROL_VALUES else "fg:ansicyan bold"
    return questionary.Choice(
        [
            (label_style, f"{label:<{width}}  "),
            ("", description),
        ],
        value,
    )


def checkbox(*args: Any, required: bool = False, **kwargs: Any) -> Question:
    """Create a checkbox with concise help and Escape bound to back."""
    kwargs.setdefault("instruction", "(Use arrow keys to move, <space> to select)")
    kwargs.setdefault("style", TUI_STYLE)
    if required:
        kwargs.setdefault(
            "validate",
            lambda selected: (
                bool(selected) or "Select at least one option to continue."
            ),
        )
    question = questionary.checkbox(*args, **kwargs)
    bindings = cast(KeyBindings, question.application.key_bindings)

    @bindings.add(Keys.Escape, eager=True)
    def go_back(event: Any) -> None:
        event.app.exit(result=None)

    return question


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
