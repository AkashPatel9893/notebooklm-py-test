"""Private Live protobuf bindings; repeated fields and unknowns remain intact."""

from __future__ import annotations

from ..exceptions import DecodingError


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return varint(number * 8 + 2) + varint(len(value)) + value


def setup(notebook_id: str, source_ids: tuple[str, ...] | None, transcribe: bool) -> bytes:
    value = field(1, "projects/" + notebook_id)
    if transcribe:
        value += b"\x10\x01\x18\x01"
    for source_id in source_ids or ():
        value += field(4, field(1, source_id))
    return field(1, value)


def audio(data: bytes, mime_type: str) -> bytes:
    return field(2, field(1, field(1, mime_type) + field(2, data)))


class Message:
    """Minimal protobuf reader, retaining all fields without guessing unknown meanings."""

    def __init__(self, data: bytes) -> None:
        self.values: dict[int, list[int | bytes]] = {}
        pos = 0

        def read() -> int:
            nonlocal pos
            value = 0
            for shift in range(0, 70, 7):
                if pos >= len(data):
                    raise ValueError
                byte = data[pos]
                pos += 1
                value |= (byte & 127) << shift
                if not byte & 128:
                    if shift == 63 and byte > 1:
                        raise ValueError
                    return value
            raise ValueError

        try:
            while pos < len(data):
                key = read()
                number, wire = key >> 3, key & 7
                if not number or number > 536870911:
                    raise ValueError
                value: int | bytes
                if wire == 0:
                    value = read()
                else:
                    if wire == 2:
                        size = read()
                    elif wire in (1, 5):
                        size = 8 if wire == 1 else 4
                    else:
                        raise ValueError
                    if pos + size > len(data):
                        raise ValueError
                    value = data[pos : pos + size]
                    pos += size
                self.values.setdefault(number, []).append(value)
        except ValueError:
            raise DecodingError("Invalid Live Voice protobuf message") from None

    def has(self, number: int) -> bool:
        return number in self.values

    def blob(self, number: int) -> bytes:
        value = self.values.get(number, [b""])[-1]
        if not isinstance(value, bytes):
            raise DecodingError("Invalid Live Voice protobuf field")
        return value

    def integer(self, number: int) -> int:
        value = self.values.get(number, [0])[-1]
        if not isinstance(value, int):
            raise DecodingError("Invalid Live Voice protobuf field")
        return value

    def text(self, number: int) -> str:
        try:
            return self.blob(number).decode()
        except UnicodeError:
            raise DecodingError("Invalid Live Voice text encoding") from None

    def message(self, number: int) -> Message:
        return Message(self.blob(number))

    def repeated(self, number: int) -> list[Message]:
        result = []
        for value in self.values.get(number, []):
            if not isinstance(value, bytes):
                raise DecodingError("Invalid Live Voice repeated field")
            result.append(Message(value))
        return result
