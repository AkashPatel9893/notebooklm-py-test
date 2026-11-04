"""Dedicated first-party Live channel, sharing Android's bearer credential owner."""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import grpc_status, raise_grpc_status
from .session import _default_grpc_loader

TARGET = "labstailwind.pa.googleapis.com:443"
METHOD = "/google.internal.labs.tailwind.api.v1.LiveSessionService/StreamLiveSession"


def mapped_error(error: Exception) -> Exception:
    """Copy only safe status data; never retain the gRPC exception or details."""
    try:
        raise_grpc_status(grpc_status(error), method=METHOD, timeout_seconds=None)
    except Exception as safe:
        safe.__context__ = None
        return safe.with_traceback(None)


class LiveTransport:
    def __init__(self, runtime: Any, epoch: int, max_response_bytes: int) -> None:
        self._runtime = runtime
        self._epoch = epoch
        self._max_response_bytes = max_response_bytes
        self._channel: Any = None
        self.call: Any = None
        self._credential: Any = None
        self._closed = False

    async def open(self, deadline: float | None) -> None:
        credential = await self._runtime.bearer_provider.get(self._epoch)
        if self._closed:
            return
        timeout = (
            None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
        )
        grpc = _default_grpc_loader()
        channel = grpc.aio.secure_channel(
            TARGET,
            grpc.ssl_channel_credentials(),
            # Let ordinary oversized responses reach the SDK's size check,
            # without allowing unbounded transport allocation.
            options=(
                (
                    "grpc.max_receive_message_length",
                    max(2 * self._max_response_bytes, 4 * 1024 * 1024),
                ),
            ),
        )
        self._channel = channel
        self._credential = credential
        self.call = channel.stream_stream(
            METHOD, request_serializer=lambda b: b, response_deserializer=lambda b: b
        )(metadata=(("authorization", "Bearer " + credential.token),), timeout=timeout)

    async def read(self) -> bytes | None:
        data = await self.call.read()
        grpc = _default_grpc_loader()
        return None if data is grpc.aio.EOF else data

    async def write(self, data: bytes) -> None:
        await self.call.write(data)

    def cancel(self) -> None:
        if self.call is not None:
            self.call.cancel()

    def map_error(self, error: Exception) -> Exception:
        if grpc_status(error).name == "UNAUTHENTICATED" and self._credential is not None:
            self._runtime.bearer_provider.invalidate(self._credential.generation)
        return mapped_error(error)

    async def close(self) -> None:
        self._closed = True
        self.cancel()
        channel = self._channel
        self._channel = None
        try:
            if channel is not None:
                await channel.close()
        finally:
            self._credential = None
            self.call = None
