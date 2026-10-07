"""Behavioral tests for incremental continuation-catalog parsing."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.json_catalog import iter_catalog


class TrackingTextIO(io.StringIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class JsonCatalogTests(unittest.TestCase):
    def parse(self, text, *, chunk_size=7):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(text, encoding="utf-8")
            return list(iter_catalog(path, chunk_size=chunk_size))

    def test_streams_nested_rows_across_string_and_unicode_boundaries(self):
        document = {
            "previous_run_id": "run-一",
            "sources": [
                {"text": "quote: \" and slash: \\",
                 "nested": [{"value": "雪"}, [1.25e10, True, None]]},
                {"emoji": "\U0001f680", "object": {"deep": {"answer": 42}}},
            ],
            "schema_version": 1,
        }
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))

        events = self.parse(encoded, chunk_size=3)

        self.assertEqual(("field", "previous_run_id", "run-一"), events[0])
        self.assertEqual(("field", "sources", None), events[1])
        self.assertEqual(document["sources"],
                         [value for kind, _key, value in events if kind == "source"])
        self.assertEqual(("field", "schema_version", 1), events[-1])

    def test_empty_sources_is_distinct_from_a_missing_sources_field(self):
        self.assertEqual(
            [("field", "schema_version", 1), ("field", "sources", None)],
            self.parse('{"schema_version":1,"sources":[]}'),
        )
        self.assertEqual(
            [("field", "schema_version", 1)],
            self.parse('{"schema_version":1}'),
        )

    def test_rejects_duplicate_top_level_keys(self):
        for encoded in (
                '{"schema_version":1,"schema_version":1,"sources":[]}',
                '{"sources":[],"sources":[]}'):
            with self.subTest(encoded=encoded):
                with self.assertRaisesRegex(ValueError, "duplicate top-level"):
                    self.parse(encoded)

    def test_rejects_truncation_invalid_numbers_and_trailing_data(self):
        invalid = (
            '{"sources":[{"unfinished":true}',
            '{"sources":[1e,2]}',
            '{"sources":[]} garbage',
            '["sources"]',
        )
        for encoded in invalid:
            with self.subTest(encoded=encoded):
                with self.assertRaises(ValueError):
                    self.parse(encoded, chunk_size=1)

    def test_yields_first_row_before_reading_the_complete_file(self):
        rows = [{"id": index, "payload": "x" * 40} for index in range(2000)]
        encoded = json.dumps({"sources": rows, "after": "done"},
                             separators=(",", ":"))
        stream = TrackingTextIO(encoded)

        with patch("modport.json_catalog.Path.open", return_value=stream):
            events = iter_catalog("unused", chunk_size=32)
            self.assertEqual(("field", "sources", None), next(events))
            self.assertEqual(("source", None, rows[0]), next(events))
            consumed = stream.tell()
            events.close()

        self.assertLess(consumed, len(encoded))
        self.assertTrue(stream.read_sizes)
        self.assertTrue(all(size == 32 for size in stream.read_sizes))

    def test_number_token_may_span_a_chunk_boundary(self):
        self.assertEqual(
            [("field", "sources", None), ("source", None, 1250.0)],
            self.parse('{"sources":[1.25e3]}', chunk_size=1),
        )


if __name__ == "__main__":
    unittest.main()
