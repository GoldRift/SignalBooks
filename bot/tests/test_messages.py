"""Tests for chat command parsing and the reply templates."""

import pytest

from signalbooks import jobs, messages
from signalbooks.messages import HELP, Command

URL = "https://audiobookbay.lu/abss/test-book-ada-example/"


def delivery(**overrides) -> jobs.Delivery:
    fields = {
        "torrent_id": 42,
        "chat": "cli",
        "title": "Test Book — Ada Example",
        "url": "https://cdn.example/test-book",
        "files": 1,
        "size": 438_400_000,
        "zipped": False,
    }
    fields.update(overrides)
    return jobs.Delivery(**fields)


def test_help_is_a_bare_command():
    assert messages.parse("/help") == Command("help")


def test_help_keeps_the_extra_words_as_its_argument():
    assert messages.parse("/help extra") == Command("help", "extra")


def test_a_command_is_case_insensitive():
    assert messages.parse("/HELP") == Command("help")
    assert messages.parse("/Relink 42") == Command("relink", "42")


def test_abb_takes_the_link_as_its_argument():
    assert messages.parse(f"/abb {URL}") == Command("abb", URL)


def test_abb_without_a_link_is_a_usage_error_not_a_fetch():
    assert messages.parse("/abb") == Command("usage", "abb")
    assert messages.parse("/abb   ") == Command("usage", "abb")


def test_a_bare_link_is_ignored():
    assert messages.parse(URL) == Command("ignore")


def test_surrounding_whitespace_is_dropped():
    assert messages.parse(f"  /abb {URL}\n") == Command("abb", URL)


def test_relink_without_an_argument_uses_the_last_delivery():
    assert messages.parse("/relink") == Command("relink", "")


def test_relink_names_one_torrent():
    assert messages.parse("/relink 42") == Command("relink", "42")


def test_relink_rejects_a_torrent_id_that_is_not_a_number():
    assert messages.parse("/relink abc") == Command("usage", "relink")


def test_a_tab_separates_a_command_from_its_argument():
    assert messages.parse(f"/abb\t{URL}") == Command("abb", URL)


def test_an_unknown_command_gets_the_usage_reply():
    assert messages.parse("/foo") == Command("usage", "")


def test_a_plain_text_message_is_not_a_command():
    assert messages.parse("hello there") == Command("ignore")


@pytest.mark.parametrize("text", ["", "/", "  "])
def test_parse_never_raises_on_odd_input(text):
    assert messages.parse(text) in (Command("ignore"), Command("usage"))


def test_the_help_text_names_every_command_and_the_expiry_rule():
    for command in ("/abb", "/relink", "/help"):
        assert command in HELP
    assert "audiobookbay.lu" in HELP
    assert "Start the download within about 3 hours" in HELP
    assert "bare link is ignored" in HELP
    assert "**" not in HELP and "__" not in HELP  # Signal renders no Markdown


def test_usage_names_the_recovery_for_each_context():
    assert messages.usage("abb") == "Usage: /abb <link>"
    assert "last book in this chat" in messages.usage("relink")
    assert messages.usage("") == "Unknown command. /help lists what I can do."


def test_a_new_add_says_what_happens_next():
    assert messages.added("Test Book — Ada Example") == (
        "Added: Test Book — Ada Example\nLink coming when it's ready."
    )


def test_a_duplicate_add_that_is_still_pending_waits_for_the_link():
    assert messages.added("Test Book — Ada Example", duplicate=True, torrent_id=42) == (
        "Already added: Test Book — Ada Example\nThe link is still coming."
    )


def test_a_duplicate_add_that_was_delivered_points_at_relink():
    assert messages.added("Test Book — Ada Example", duplicate=True, torrent_id=42, sent=True) == (
        "Already added: Test Book — Ada Example\n/relink 42 for a fresh one."
    )


def test_a_single_file_delivery_names_the_book_link_and_size():
    assert messages.delivery(delivery()) == (
        "Test Book — Ada Example\n"
        "https://cdn.example/test-book\n"
        "1 file · 438 MB\n"
        "Start the download within about 3 hours · /relink 42 for a new one"
    )


def test_a_zip_delivery_reports_the_file_count():
    assert messages.delivery(delivery(files=7, size=1_200_000_000, zipped=True)) == (
        "Test Book — Ada Example\n"
        "https://cdn.example/test-book\n"
        "Zip · 7 files · 1.2 GB\n"
        "Start the download within about 3 hours · /relink 42 for a new one"
    )


def test_a_delivery_without_a_size_still_reads_cleanly():
    assert "1 file\n" in messages.delivery(delivery(size=0))
    assert "1 file ·" not in messages.delivery(delivery(size=0))


def test_a_failed_delivery_is_one_line_with_the_reason():
    text = messages.delivery(
        delivery(
            url="",
            error="TorBox reported a download error. Send /abb with the book link again in 10 minutes.",
        )
    )
    assert text == (
        "Couldn't get Test Book — Ada Example. TorBox reported a download error. "
        "Send /abb with the book link again in 10 minutes."
    )
    assert "\n" not in text


def test_a_delivery_reason_without_a_period_gains_one():
    assert messages.delivery(delivery(url="", error="TorBox: no slots")) == (
        "Couldn't get Test Book — Ada Example. TorBox: no slots."
    )


def test_relink_names_the_book_when_it_is_known():
    assert messages.relink("https://cdn.example/fresh", "Test Book — Ada Example") == (
        "Fresh link for Test Book — Ada Example\n"
        "https://cdn.example/fresh\n"
        "Start the download within about 3 hours."
    )


def test_relink_falls_back_to_the_torrent_id():
    assert messages.relink("https://cdn.example/fresh", torrent_id=42) == (
        "Fresh link for torrent 42\n"
        "https://cdn.example/fresh\n"
        "Start the download within about 3 hours."
    )


def test_failure_turns_an_exception_into_a_sentence():
    assert messages.failure(Exception("not an AudiobookBay link")) == (
        "Not an AudiobookBay link."
    )
    assert messages.failure(Exception("TorBox: no slots.")) == "TorBox: no slots."
    assert messages.failure(Exception()) == "Something went wrong. Try again."


def test_size_text_uses_readable_units():
    assert messages.size_text(0) == ""
    assert messages.size_text(999) == "999 B"
    assert messages.size_text(3_400) == "3 KB"
    assert messages.size_text(438_400_000) == "438 MB"
    assert messages.size_text(999_500) == "1 MB"
    assert messages.size_text(1_000_000_000) == "1 GB"
    assert messages.size_text(999_500_000) == "1 GB"
    assert messages.size_text(1_200_000_000) == "1.2 GB"


def test_only_a_direct_message_from_an_allowlisted_number_is_answered():
    allowed = frozenset({"+15550000001"})

    assert messages.permitted("+15550000001", None, allowed)
    assert not messages.permitted("+15559999999", None, allowed)
    assert not messages.permitted("+15550000001", "group==", allowed)


def test_a_group_message_is_answered_only_in_the_configured_group():
    allowed = frozenset({"+15550000001"})

    assert messages.permitted("+15550000001", "group==", allowed, "group==")
    assert not messages.permitted("+15550000001", "group==", allowed, "other==")
    assert not messages.permitted("+15550000001", "group==", allowed, None)


def test_every_member_of_the_configured_group_is_trusted():
    allowed = frozenset({"+15550000001"})

    assert messages.permitted("+15559999999", "group==", allowed, "group==")
    assert not messages.permitted("+15559999999", None, allowed, "group==")


def test_a_group_alone_leaves_direct_messages_closed():
    assert messages.permitted("+15559999999", None, frozenset(), "group==") is False
