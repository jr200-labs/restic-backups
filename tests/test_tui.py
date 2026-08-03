from unittest.mock import Mock, patch

import pytest
from prompt_toolkit.keys import Keys

from restic_backups import cli
from restic_backups.generic.tui import TUI_STYLE, checkbox, menu_choice, select


def test_escape_goes_back_and_ctrl_c_exits_cleanly() -> None:
    question = select("Menu:", choices=["Item"])
    with patch.object(question, "unsafe_ask", return_value="Item"):
        assert question.unsafe_ask() == "Item"

    event = Mock()
    key_bindings = question.application.key_bindings
    assert key_bindings is not None
    binding = key_bindings.get_bindings_for_keys((Keys.Escape,))
    binding[-1].handler(event)
    event.app.exit.assert_called_once_with(result=None)

    with (
        patch.object(cli.audit, "record"),
        patch.object(cli, "app", side_effect=KeyboardInterrupt),
        pytest.raises(SystemExit) as stopped,
    ):
        cli.main()
    assert stopped.value.code == 0


def test_disabled_choices_are_grey() -> None:
    attrs = TUI_STYLE.get_attrs_for_style_str("class:disabled")

    assert attrs.color == "ansibrightblack"


def test_navigation_controls_and_pointer_are_white() -> None:
    control = menu_choice("Back", "Return", "back", 10)
    data = menu_choice("Repository", "Select data", "repository", 10)

    assert isinstance(control.title, list)
    assert isinstance(data.title, list)
    assert control.title[0][0] == "class:control"
    assert data.title[0][0] == "fg:ansicyan bold"
    assert TUI_STYLE.get_attrs_for_style_str("class:control").color == "ansiwhite"
    assert TUI_STYLE.get_attrs_for_style_str("class:pointer").color == "ansiwhite"


def test_checkbox_uses_concise_navigation_help() -> None:
    with patch("restic_backups.generic.tui.questionary.checkbox") as prompt:
        checkbox("Options:", choices=["Dry run"])

    assert prompt.call_args.kwargs["instruction"] == (
        "(Use arrow keys to move, <space> to select)"
    )
