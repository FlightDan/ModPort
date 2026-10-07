"""Incremental reader for continuation source catalogs.

Catalog rows can contain large operation inputs, so callers must be able to
validate and discard one row without materializing the complete document.
"""
from json import JSONDecodeError, JSONDecoder
from pathlib import Path


_WHITESPACE = frozenset(" \t\r\n")
_NUMBER_CONTINUATION = frozenset("0123456789.eE+-")


def _invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


class _BufferedJSON:
    def __init__(self, stream, chunk_size):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self.stream = stream
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = JSONDecoder(parse_constant=_invalid_constant)

    def _compact(self):
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0

    def _read_more(self, chunks=1):
        self._compact()
        pieces = [self.buffer]
        for _ in range(chunks):
            if self.eof:
                break
            try:
                piece = self.stream.read(self.chunk_size)
            except UnicodeError as exc:
                raise ValueError("catalog is not valid UTF-8") from exc
            if not isinstance(piece, str):
                raise ValueError("catalog stream must return text")
            if not piece:
                self.eof = True
                break
            pieces.append(piece)
        if len(pieces) > 1:
            self.buffer = "".join(pieces)

    def _available(self):
        while self.position == len(self.buffer) and not self.eof:
            self._read_more()
        return self.position < len(self.buffer)

    def skip_whitespace(self):
        while True:
            while (self.position < len(self.buffer)
                   and self.buffer[self.position] in _WHITESPACE):
                self.position += 1
            if self.position < len(self.buffer) or self.eof:
                self._compact()
                return
            self._read_more()

    def peek(self):
        self.skip_whitespace()
        if not self._available():
            return None
        return self.buffer[self.position]

    def take(self, expected):
        actual = self.peek()
        if actual != expected:
            found = "end of file" if actual is None else repr(actual)
            raise ValueError(f"expected {expected!r}, found {found}")
        self.position += 1
        self._compact()

    def value(self):
        self.skip_whitespace()
        if not self._available():
            raise ValueError("unexpected end of catalog")
        reads = 1
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except JSONDecodeError as exc:
                if self.eof:
                    raise ValueError("invalid or truncated catalog JSON") from exc
                self._read_more(reads)
                reads = min(reads * 2, 1024)
                continue

            # raw_decode may accept the prefix of a number at a chunk boundary
            # (for example, ``1`` from ``1e3``). Read through the token before
            # committing the value. Structural values and strings cannot have
            # a valid continuation after raw_decode returns.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if end < len(self.buffer) and self.buffer[end] in _NUMBER_CONTINUATION:
                    tail = self.buffer[end:]
                    boundary_seen = any(char in _WHITESPACE or char in ",]}"
                                        for char in tail)
                    if boundary_seen or self.eof:
                        raise ValueError("invalid JSON number")
                    del value
                    self._read_more(reads)
                    reads = min(reads * 2, 1024)
                    continue

            # Ensure a token ending exactly at the buffer boundary is not a
            # scalar prefix whose remaining characters arrive in the next read.
            if end == len(self.buffer) and not self.eof:
                del value
                self._read_more()
                continue
            self.position = end
            self._compact()
            return value


def _iter_stream(stream, *, chunk_size):
    reader = _BufferedJSON(stream, chunk_size)
    reader.take("{")
    seen = set()
    if reader.peek() == "}":
        reader.take("}")
    else:
        while True:
            key = reader.value()
            if not isinstance(key, str):
                raise ValueError("catalog object keys must be strings")
            if key in seen:
                raise ValueError(f"duplicate top-level catalog key: {key}")
            seen.add(key)
            reader.take(":")
            if key == "sources":
                reader.take("[")
                yield "field", "sources", None
                if reader.peek() == "]":
                    reader.take("]")
                else:
                    while True:
                        row = reader.value()
                        yield "source", None, row
                        del row
                        separator = reader.peek()
                        if separator == "]":
                            reader.take("]")
                            break
                        reader.take(",")
            else:
                value = reader.value()
                yield "field", key, value
                del value

            separator = reader.peek()
            if separator == "}":
                reader.take("}")
                break
            reader.take(",")

    if reader.peek() is not None:
        raise ValueError("trailing data after catalog object")


def iter_catalog(path, *, chunk_size=64 * 1024):
    """Yield top-level fields and individual ``sources`` rows from *path*.

    The ``sources`` array itself is reported as ``('field', 'sources', None)``
    before its rows. Each row is then yielded as ``('source', None, row)``.
    Other top-level members are yielded as ``('field', key, value)``. A caller
    must exhaust the iterator before treating any yielded values as valid,
    because duplicate keys or trailing corruption may occur later in the file.

    Memory use is bounded by one decoded value plus the current read buffer.
    Consequently, a single unusually large source row can still require
    correspondingly large memory.
    """
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as stream:
            yield from _iter_stream(stream, chunk_size=chunk_size)
    except JSONDecodeError as exc:
        raise ValueError("invalid catalog JSON") from exc
