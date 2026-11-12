"""Tests for chat command parsing."""

import pytest

from signalbooks import messages
from signalbooks.messages import HELP, USAGE, Command

URL = "https://audiobookbay.lu/abss/test-book-ada-example/"


def test_help_is_a_bare_command():
    assert messages.parse("/help") == Command("help")


def test_help_keeps_the_extra_words_as_its_argument():
    assert messages.parse("/help extra") == Command("help", "extra")


def test_a_command_is_case_insensitive():
    assert messages.parse("/HELP") == Command("help")
    assert messages.parse("/Relink 42") == Command("relink", "42")


def test_abb_takes_the_link_as_its_argument():
    assert messages.parse(f"/abb {URL}") == Command("abb", URL)


def test_a_bare_link_is_shorthand_for_abb():
    assert messages.parse(URL) == Command("abb", URL)


def test_surrounding_whitespace_is_dropped():
    assert messages.parse(f"  {URL}\n") == Command("abb", URL)


def test_relink_without_an_argument_uses_the_last_delivery():
    assert messages.parse("/relink") == Command("relink", "")


def test_relink_names_one_torrent():
    assert messages.parse("/relink 42") == Command("relink", "42")


def test_relink_rejects_a_torrent_id_that_is_not_a_number():
    assert messages.parse("/relink abc") == Command("usage")


def test_a_tab_separates_a_command_from_its_argument():
    assert messages.parse(f"/abb\t{URL}") == Command("abb", URL)


def test_an_unknown_command_gets_the_usage_reply():
    assert messages.parse("/foo") == Command("usage", "")


def test_a_plain_text_message_is_not_a_command():
    assert messages.parse("hello there") == Command("abb", "hello there")


@pytest.mark.parametrize("text", ["", "/", "  "])
def test_parse_never_raises_on_odd_input(text):
    assert messages.parse(text) in (Command("abb", ""), Command("usage"))


def test_the_templates_name_every_command():
    assert "/help" in HELP and "/abb" in HELP and "/relink" in HELP
    assert HELP in USAGE and "unknown command" in USAGE
