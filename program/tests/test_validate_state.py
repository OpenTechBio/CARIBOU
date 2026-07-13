from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "caribou_validate_state", ROOT / "program" / "validate_state.py"
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
validator = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(validator)


class PolicySemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.safe_load(
            (ROOT / "program" / "template" / "policy.yaml").read_text(encoding="utf-8")
        )
        cls.schema = validator.load_json(ROOT / "program" / "schemas" / "policy.schema.json")

    def test_public_template_is_valid(self) -> None:
        self.assertEqual([], validator.validate_against_schema(self.template, self.schema, "policy"))
        self.assertEqual([], validator.validate_policy(self.template))

    def test_main_must_be_denied_for_remote_push(self) -> None:
        policy = copy.deepcopy(self.template)
        policy["repository"]["push_remote"] = True
        policy["repository"]["remote_write_policy"]["denied_branches"] = ["release"]
        self.assertTrue(any("main must be listed" in error for error in validator.validate_policy(policy)))

    def test_only_peerd_partition_is_authorized(self) -> None:
        policy = copy.deepcopy(self.template)
        policy["slurm"]["allowed"] = True
        policy["slurm"]["partitions"] = ["gpu"]
        policy["slurm"]["required_partition"] = "gpu"
        self.assertTrue(any("partition peerd exclusively" in error for error in validator.validate_policy(policy)))

    def test_download_root_must_stay_inside_caribou(self) -> None:
        policy = copy.deepcopy(self.template)
        policy["data"]["external_dataset_download_allowed"] = True
        policy["data"]["external_dataset_download_paths"] = ["/tmp/caribou-data"]
        self.assertTrue(any("download path must be inside CARIBOU" in error for error in validator.validate_policy(policy)))

    def test_explicit_unlimited_model_limits_are_schema_valid(self) -> None:
        policy = copy.deepcopy(self.template)
        policy["external_model_apis"].update(
            {
                "allowed": True,
                "allowed_models": ["*"],
                "maximum_calls": "unlimited",
                "maximum_tokens": "unlimited",
                "maximum_spend_usd": "unlimited",
            }
        )
        self.assertEqual([], validator.validate_against_schema(policy, self.schema, "policy"))


if __name__ == "__main__":
    unittest.main()
