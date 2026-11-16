"""signal-cli JSON-RPC client over the daemon's Unix socket.

One JSON object per line. Requests carry an ``id`` that the matching response
echoes; incoming messages arrive as ``receive`` notifications. Sync traffic and
messages this account sent itself are dropped here: they are the phone's echo.
"""

import asyncio
import itertools
import json
import pathlib
from dataclasses import dataclass

TIMEOUT = 30.0
# Chat keys for groups carry this prefix so a reply knows to use ``groupId``
# instead of ``recipient``. Base64 group ids cannot contain a colon.
GROUP_PREFIX = "group:"


class SignalError(Exception):
    """Expected failure: daemon unreachable, socket closed, or request rejected."""


@dataclass(frozen=True)
class Incoming:
    sender: str
    text: str
    group: str | None = None

    @property
    def chat(self) -> str:
        """Where a reply goes: the group, or the sender's number."""
        return GROUP_PREFIX + self.group if self.group else self.sender


class Client:
    def __init__(self, socket_path: str | pathlib.Path, account: str = ""):
        self.path = pathlib.Path(socket_path)
        self.account = account
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self._inbox: asyncio.Queue[Incoming | None] = asyncio.Queue()

    async def connect(self) -> None:
        """Open the socket. Raises SignalError so the caller can retry."""
        try:
            reader, self._writer = await asyncio.open_unix_connection(self.path)
        except OSError as exc:
            raise SignalError(f"cannot reach signal-cli at {self.path}: {exc.strerror or exc}") from exc
        self._reader = asyncio.create_task(self._read(reader))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._writer is not None:
            self._writer.close()

    async def next_message(self) -> Incoming | None:
        """The next message worth answering, or None once the socket closes."""
        return await self._inbox.get()

    async def send(self, chat: str, text: str, *, urgent: bool = True) -> int:
        """Send, returning the message's timestamp so it can be edited later."""
        params = self._params(chat, text)
        if not urgent:
            params["noUrgent"] = True
        return _timestamp(await self._call("send", params))

    async def edit(self, chat: str, timestamp: int, text: str, *, urgent: bool = True) -> None:
        """Replace the text of a message this account sent. The bubble keeps the
        timestamp it was sent with, so every edit targets that same one."""
        params = self._params(chat, text)
        params["editTimestamp"] = timestamp
        if not urgent:
            params["noUrgent"] = True
        await self._call("send", params)

    @staticmethod
    def _params(chat: str, text: str) -> dict:
        if chat.startswith(GROUP_PREFIX):
            return {"groupId": chat[len(GROUP_PREFIX) :], "message": text}
        return {"recipient": [chat], "message": text}

    async def _call(self, method: str, params: dict) -> object:
        writer = self._writer
        if writer is None:
            raise SignalError("not connected to signal-cli")
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            request = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            return await asyncio.wait_for(future, TIMEOUT)
        except TimeoutError:
            raise SignalError(f"signal-cli did not answer {method}") from None
        except OSError as exc:
            raise SignalError(f"signal-cli connection failed: {type(exc).__name__}") from exc
        finally:
            self._waiting.pop(request_id, None)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except OSError:
            pass
        finally:
            self._closed()

    def _dispatch(self, message: dict) -> None:
        future = self._waiting.get(message.get("id"))
        if future is not None:
            if future.done():
                return
            error = message.get("error")
            if error:
                detail = error.get("message") if isinstance(error, dict) else error
                future.set_exception(SignalError(f"signal-cli: {detail}"))
            else:
                future.set_result(message.get("result"))
            return
        if message.get("method") == "receive":
            params = message.get("params")
            if not isinstance(params, dict):
                return
            incoming = _incoming(params, self.account or params.get("account") or "")
            if incoming is not None:
                self._inbox.put_nowait(incoming)

    def _closed(self) -> None:
        self._inbox.put_nowait(None)
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(SignalError("signal-cli closed the socket"))


def _timestamp(result: object) -> int:
    """signal-cli answers a send with ``{timestamp, results}``; the timestamp is
    what a later edit must target."""
    if isinstance(result, dict) and isinstance(result.get("timestamp"), int):
        return result["timestamp"]
    raise SignalError("signal-cli did not confirm the message")


def _incoming(params: dict, account: str) -> Incoming | None:
    # The single-account daemon nests the envelope; older builds put it inline.
    envelope = params.get("envelope") or params
    if not isinstance(envelope, dict) or envelope.get("syncMessage"):
        return None
    sender = envelope.get("sourceNumber") or ""
    data = envelope.get("dataMessage")
    if not isinstance(data, dict):
        return None
    text = data.get("message") or ""
    if not sender or not text or sender == account:
        return None
    group_info = data.get("groupInfo")
    group = group_info.get("groupId") if isinstance(group_info, dict) else None
    return Incoming(sender, text, group or None)
