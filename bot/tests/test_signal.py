"""Tests for the signal-cli JSON-RPC client, against a fake Unix socket."""

import asyncio
import json

import pytest

from signalbooks import signal

ACCOUNT = "+15550000001"
OTHER = "+15550000002"


def receive(*envelopes: dict) -> str:
    return "".join(
        json.dumps({"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope}}) + "\n"
        for envelope in envelopes
    )


def data_message(sender: str = OTHER, text: str = "hello", group: str | None = None) -> dict:
    data: dict = {"message": text}
    if group:
        data["groupInfo"] = {"groupId": group}
    return {"sourceNumber": sender, "dataMessage": data}


def sync_message() -> dict:
    return {"sourceNumber": ACCOUNT, "syncMessage": {"sentMessage": {"message": "from my phone"}}}


def feed(lines: str):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(lines.encode())
        await writer.drain()
        await reader.read()  # hold the connection until the client closes it
        writer.close()

    return handler


def close_at_once():
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    return handler


def answer(result: dict, seen: list | None = None):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        if seen is not None:
            seen.append(request)
        writer.write((json.dumps({"jsonrpc": "2.0", "id": request["id"], **result}) + "\n").encode())
        await writer.drain()
        await reader.read()
        writer.close()

    return handler


def run(tmp_path, handler, action):
    """Start a one-shot fake daemon on a Unix socket and run ``action(path)``."""

    async def main():
        server = await asyncio.start_unix_server(handler, path=str(tmp_path / "signal.sock"))
        try:
            return await action(tmp_path / "signal.sock")
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def test_a_direct_message_arrives_with_its_sender_and_text(tmp_path):
    with_result = run(tmp_path, feed(receive(data_message(text="hi there"))), _first)
    assert with_result == signal.Incoming(OTHER, "hi there")


def test_a_group_message_carries_its_group_id(tmp_path):
    message = run(tmp_path, feed(receive(data_message(group="group=="))), _first)
    assert message == signal.Incoming(OTHER, "hello", "group==")


def test_sync_traffic_is_ignored(tmp_path):
    message = run(
        tmp_path,
        feed(receive(sync_message(), data_message(text="real"))),
        _first,
    )
    assert message.text == "real"


def test_this_accounts_own_message_is_ignored(tmp_path):
    message = run(
        tmp_path,
        feed(receive(data_message(sender=ACCOUNT, text="echo"), data_message(text="real"))),
        _first,
    )
    assert message.text == "real"


def test_a_message_without_text_is_ignored(tmp_path):
    typing = {"sourceNumber": OTHER, "typingMessage": {"action": "STARTED"}}
    message = run(tmp_path, feed(receive(typing, data_message(text="real"))), _first)
    assert message.text == "real"


def test_a_junk_line_does_not_kill_the_client(tmp_path):
    message = run(tmp_path, feed("[]\nnot json\n" + receive(data_message())), _first)
    assert message.text == "hello"


def test_a_malformed_notification_does_not_kill_the_reader(tmp_path):
    junk = json.dumps({"jsonrpc": "2.0", "method": "receive", "params": []}) + "\n"
    message = run(tmp_path, feed(junk + receive(data_message())), _first)
    assert message.text == "hello"


def test_a_malformed_error_response_is_still_a_one_line_error(tmp_path):
    async def action(socket_path):
        client = signal.Client(socket_path, ACCOUNT)
        await client.connect()
        try:
            with pytest.raises(signal.SignalError, match="boom"):
                await asyncio.wait_for(client.send(OTHER, "pong"), 5)
        finally:
            await client.close()

    run(tmp_path, answer({"error": "boom"}), action)


def test_send_writes_a_json_rpc_request_and_waits_for_the_answer(tmp_path):
    seen: list = []

    async def action(socket_path):
        client = signal.Client(socket_path, ACCOUNT)
        await client.connect()
        try:
            await asyncio.wait_for(client.send(OTHER, "pong"), 5)
        finally:
            await client.close()

    run(tmp_path, answer({"result": {"timestamp": 1}}, seen), action)

    assert seen[0]["method"] == "send"
    assert seen[0]["params"] == {"recipient": [OTHER], "message": "pong"}


def test_a_rejected_request_raises_a_one_line_error(tmp_path):
    async def action(socket_path):
        client = signal.Client(socket_path, ACCOUNT)
        await client.connect()
        try:
            with pytest.raises(signal.SignalError, match="not registered"):
                await asyncio.wait_for(client.send(OTHER, "pong"), 5)
        finally:
            await client.close()

    run(
        tmp_path,
        answer({"error": {"code": -1, "message": "not registered"}}),
        action,
    )


def test_a_closed_socket_ends_the_message_stream(tmp_path):
    async def action(socket_path):
        client = signal.Client(socket_path, ACCOUNT)
        await client.connect()
        try:
            return await asyncio.wait_for(client.next_message(), 5)
        finally:
            await client.close()

    assert run(tmp_path, close_at_once(), action) is None


def test_connect_fails_cleanly_when_the_daemon_is_not_there(tmp_path):
    async def action(socket_path):
        client = signal.Client(socket_path, ACCOUNT)
        with pytest.raises(signal.SignalError, match="cannot reach signal-cli"):
            await client.connect()

    asyncio.run(action(tmp_path / "missing.sock"))


async def _first(socket_path):
    client = signal.Client(socket_path, ACCOUNT)
    await client.connect()
    try:
        return await asyncio.wait_for(client.next_message(), 5)
    finally:
        await client.close()
