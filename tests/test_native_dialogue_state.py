"""Durable reconciliation for uncertain app-server turn/start requests."""
import unittest

from modport.native_dialogue_state import reconcile_pending_turn


class FakeTransport:
    def __init__(self, pages=None, failure=None):
        self.pages = pages or {}
        self.failure = failure
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if self.failure == method:
            raise RuntimeError("server pagination failed")
        if callable(self.pages[method]):
            return self.pages[method](params)
        cursor = params.get("cursor")
        return self.pages[method][cursor]


def page(data, next_cursor=None, backwards_cursor=None):
    return {"data": data, "nextCursor": next_cursor,
            "backwardsCursor": backwards_cursor}


def user_entry(turn_id, item_id, client_id):
    return {"turnId": turn_id, "item": {"type": "userMessage", "id": item_id,
        "clientId": client_id, "content": [{"type": "text", "text": "prompt"}]}}


def agent_entry(turn_id, item_id, text):
    return {"turnId": turn_id, "item": {"type": "agentMessage", "id": item_id,
        "text": text, "phase": "final_answer", "memoryCitation": None,
        "delivery": None, "questions": None}}


class NativeDialogueStateTests(unittest.TestCase):
    pending = {"client_user_message_id": "modport:command:plan:0"}

    def test_pages_protocol_history_and_returns_matching_turn_messages(self):
        unfiltered_items = {
            None: page([user_entry("older", "user-older", "another-client")],
                       "item-page-2"),
            "item-page-2": page([
                user_entry("wanted", "user-wanted", "modport:command:plan:0"),
                agent_entry("wanted", "agent-1", "Plan response"),
                {"turnId": "wanted", "item": {"type": "reasoning", "id": "private"}},
                agent_entry("older", "agent-old", "Unrelated response"),
            ], backwards_cursor="item-page-1"),
        }

        def item_pages(params):
            if params.get("turnId") == "wanted":
                return page([agent_entry("wanted", "agent-1", "Plan response")])
            return unfiltered_items[params.get("cursor")]

        transport = FakeTransport({
            "thread/turns/list": {
                None: page([{"id": "older", "status": "completed"}], "turn-page-2"),
                "turn-page-2": page([{"id": "wanted", "status": "completed"}],
                                    backwards_cursor="turn-page-1"),
            },
            "thread/items/list": item_pages,
        })

        result = reconcile_pending_turn(transport, "thread-1", self.pending)

        self.assertEqual({"id": "wanted", "status": "completed", "items": [
            agent_entry("wanted", "agent-1", "Plan response")["item"]]}, result)
        self.assertEqual(("thread/turns/list", {"threadId": "thread-1",
            "sortDirection": "asc", "itemsView": "notLoaded"}), transport.calls[0])
        self.assertEqual("turn-page-2", transport.calls[1][1]["cursor"])
        self.assertEqual(("thread/items/list", {"threadId": "thread-1",
            "sortDirection": "asc"}), transport.calls[2])
        self.assertEqual("item-page-2", transport.calls[3][1]["cursor"])

    def test_no_correlated_user_message_fails_closed(self):
        transport = FakeTransport({
            "thread/turns/list": {None: page([])},
            "thread/items/list": {None: page([
                user_entry("other", "user-other", "different")])},
        })
        with self.assertRaisesRegex(RuntimeError, "outcome is uncertain"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_multiple_correlated_messages_are_ambiguous(self):
        transport = FakeTransport({
            "thread/turns/list": {None: page([
                {"id": "one", "status": "completed"},
                {"id": "two", "status": "completed"}])},
            "thread/items/list": {None: page([
                user_entry("one", "user-1", "modport:command:plan:0"),
                user_entry("two", "user-2", "modport:command:plan:0")])},
        })
        with self.assertRaisesRegex(RuntimeError, "multiple persisted items"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_missing_matching_turn_is_a_protocol_error(self):
        transport = FakeTransport({
            "thread/turns/list": {None: page([])},
            "thread/items/list": {None: page([
                user_entry("missing", "user", "modport:command:plan:0")])},
        })
        with self.assertRaisesRegex(RuntimeError, "no unique persisted turn"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_persisted_turn_id_must_match_correlated_history(self):
        transport = FakeTransport({
            "thread/turns/list": {None: page([
                {"id": "observed", "status": "completed"}])},
            "thread/items/list": {None: page([
                user_entry("observed", "user", "modport:command:plan:0")])},
        })
        pending = {**self.pending, "turn_id": "acknowledged-by-server"}
        with self.assertRaisesRegex(RuntimeError, "turn id conflicts"):
            reconcile_pending_turn(transport, "thread-1", pending)

    def test_repeated_turn_cursor_is_a_protocol_error(self):
        transport = FakeTransport({
            "thread/turns/list": {
                None: page([], "same"),
                "same": page([], "same"),
            },
        })
        with self.assertRaisesRegex(RuntimeError, "repeated a pagination cursor"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_repeated_item_cursor_is_a_protocol_error(self):
        transport = FakeTransport({
            "thread/turns/list": {None: page([])},
            "thread/items/list": {
                None: page([], "same"),
                "same": page([], "same"),
            },
        })
        with self.assertRaisesRegex(RuntimeError, "repeated a pagination cursor"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_server_pagination_failure_is_not_treated_as_no_match(self):
        transport = FakeTransport(failure="thread/turns/list")
        with self.assertRaisesRegex(RuntimeError, "server pagination failed"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_missing_required_next_cursor_is_a_protocol_error(self):
        transport = FakeTransport({
            "thread/turns/list": {None: {"data": [], "backwardsCursor": None}},
        })
        with self.assertRaisesRegex(RuntimeError, "omitted nextCursor"):
            reconcile_pending_turn(transport, "thread-1", self.pending)

    def test_in_progress_turn_makes_absence_uncertain(self):
        item_reads = 0

        def items(_params):
            nonlocal item_reads
            item_reads += 1
            if item_reads == 1:
                return page([])
            return page([user_entry("active", "user-active",
                                    "modport:command:plan:0")])

        transport = FakeTransport({
            "thread/turns/list": {None: page([
                {"id": "active", "status": "inProgress"}])},
            "thread/items/list": items,
        })
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            reconcile_pending_turn(transport, "thread-1", self.pending)
        self.assertEqual(1, item_reads)

    def test_status_and_messages_are_refreshed_after_correlation(self):
        turn_reads = 0
        filtered_item_reads = 0

        def turns(_params):
            nonlocal turn_reads
            turn_reads += 1
            status = "inProgress" if turn_reads < 3 else "completed"
            return page([{"id": "wanted", "status": status}])

        def items(params):
            nonlocal filtered_item_reads
            if "turnId" not in params:
                return page([user_entry("wanted", "user-wanted",
                    "modport:command:plan:0")])
            filtered_item_reads += 1
            rows = [user_entry("wanted", "user-wanted", "modport:command:plan:0")]
            if filtered_item_reads > 1:
                rows.append(agent_entry("wanted", "agent-final", "Finished"))
            return page(rows)

        transport = FakeTransport({"thread/turns/list": turns,
                                   "thread/items/list": items})
        result = reconcile_pending_turn(transport, "thread-1", self.pending)
        self.assertEqual("completed", result["status"])
        self.assertEqual(["agent-final"], [item["id"] for item in result["items"]])
        self.assertEqual(2, filtered_item_reads)

    def test_pending_client_id_is_required(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(ValueError, "client_user_message_id"):
            reconcile_pending_turn(transport, "thread-1", {})


if __name__ == "__main__":
    unittest.main()
