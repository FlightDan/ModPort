from dataclasses import replace
import unittest

from modport.characterization import (
    CharacterizationContract,
    FrozenContractError,
    generate_contract,
    freeze_contract,
    review_contract,
)


class CharacterizationTests(unittest.TestCase):
    def test_entries_and_behaviors_cannot_disagree(self):
        with self.assertRaises(ValueError):
            CharacterizationContract.from_mapping({
                "entries": [],
                "behaviors": [{
                    "id": "one", "source_evidence": "source", "action": ["act"],
                    "assertions": ["assert"], "test_mapping": ["test"],
                }],
            })

    def _contract(self):
        return generate_contract(
            {
                "entries": [
                    {
                        "id": "health_change",
                        "source": "source/behavior",
                        "preconditions": ["entity is alive"],
                        "actions": ["apply change"],
                        "assertions": ["observable value is preserved"],
                        "side": "server",
                        "tests": ["game_test.health_change"],
                    }
                ]
            },
            generator_id="characterizer",
        )

    def test_schema_contains_observable_behavior_fields(self):
        contract = self._contract()
        entry = contract.entries[0].to_dict()
        self.assertEqual(
            set(("behavior_source", "preconditions", "operations", "assertions", "side", "test_mapping")),
            set(entry) & {"behavior_source", "preconditions", "operations", "assertions", "side", "test_mapping"},
        )
        self.assertEqual(len(contract.sha256()), 64)
        self.assertEqual(contract.sha256(), generate_contract(contract).sha256())

    def test_only_independent_approved_review_can_freeze(self):
        contract = self._contract()
        with self.assertRaises(Exception):
            review_contract(contract, "characterizer")
        review = review_contract(contract, "reviewer")
        frozen = freeze_contract(contract, review)
        self.assertTrue(frozen.verify())
        self.assertEqual(frozen.frozen_sha256, contract.sha256())

    def test_frozen_contract_rejects_weakening_and_waiver(self):
        contract = self._contract()
        frozen = freeze_contract(contract, review_contract(contract, "reviewer"))
        weakened = generate_contract(
            {"entries": [{"id": "health_change", "source": "source/behavior", "assertions": ["weaker"]}]},
            generator_id="characterizer",
        )
        with self.assertRaises(FrozenContractError):
            frozen.assert_not_weakened(weakened)
        with self.assertRaises(FrozenContractError):
            frozen.enforce(waiver="temporary exemption")
        with self.assertRaises(FrozenContractError):
            frozen.waive("skip assertion")

    def test_stale_review_hash_is_not_a_freeze_gate(self):
        contract = self._contract()
        review = review_contract(contract, "reviewer")
        changed = generate_contract(
            {"entries": [{"id": "other", "source": "source/other"}]}, generator_id="characterizer"
        )
        self.assertTrue(freeze_contract(changed, review).verify())

    def test_behavior_guard_allows_metadata_updates_without_hash_matching(self):
        contract = self._contract()
        frozen = freeze_contract(contract, review_contract(contract, "reviewer"))
        updated = replace(contract, metadata={"notes": "additional source research"})
        self.assertNotEqual(updated.sha256(), contract.sha256())
        self.assertTrue(frozen.assert_not_weakened(updated))


if __name__ == "__main__":
    unittest.main()
