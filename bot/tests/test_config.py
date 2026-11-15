"""Tests for env parsing."""

import pathlib

from signalbooks import config


def test_everything_defaults_to_empty_so_the_cli_can_pass_the_checks_it_needs():
    cfg = config.load({})

    assert cfg.torbox_api_key == ""
    assert cfg.abb_cookie is None
    assert cfg.signal_account == ""
    assert cfg.signal_socket == pathlib.Path("/run/signal-cli/socket")
    assert cfg.allowed_numbers == frozenset()
    assert cfg.signal_group_id is None


def test_the_signal_allowlist_is_split_on_commas_and_trimmed():
    cfg = config.load({"SIGNAL_ALLOWED_NUMBERS": " +15550000001, +15550000002 ,,"})

    assert cfg.allowed_numbers == frozenset({"+15550000001", "+15550000002"})


def test_the_socket_path_can_be_overridden():
    cfg = config.load({"SIGNAL_SOCKET": "/tmp/signal.sock"})

    assert cfg.signal_socket == pathlib.Path("/tmp/signal.sock")


def test_an_empty_cookie_counts_as_unset():
    assert config.load({"ABB_COOKIE": "  "}).abb_cookie is None


def test_the_group_id_is_optional_and_empty_counts_as_unset():
    assert config.load({"SIGNAL_GROUP_ID": "r2V4=="}).signal_group_id == "r2V4=="
    assert config.load({"SIGNAL_GROUP_ID": "  "}).signal_group_id is None
