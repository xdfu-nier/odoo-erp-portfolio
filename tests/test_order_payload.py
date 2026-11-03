"""Pure Python contract for bounded, deterministic external-order payloads."""

import builtins
import copy
import dataclasses
import hashlib
import importlib.util
import json
import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "addons" / "portfolio_order_bridge" / "payload.py"
TOP_FIELDS = (
    "company_code", "source_code", "external_order_id", "customer_ref", "currency", "lines",
)
LINE_FIELDS = ("external_line_id", "sku", "quantity", "unit_price", "discount")


def make_payload():
    return {
        "company_code": "PARTS-CN",
        "source_code": "B2B",
        "external_order_id": "PO-2026-001",
        "customer_ref": "BUYER-1",
        "currency": "CNY",
        "lines": [
            {
                "external_line_id": "20",
                "sku": "轴承-6205",
                "quantity": "0002.5000000",
                "unit_price": "00100.0000",
                "discount": "05.500000",
            },
            {
                "external_line_id": "10",
                "sku": "SEAL-8",
                "quantity": "0001.0000",
                "unit_price": "0.000",
                "discount": "100.0000000",
            },
        ],
        "request_id": "request-001",
        "sent_at": "2026-11-03T10:20:30+08:00",
    }


def compact_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


class OrderPayloadTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE_PATH.is_file(), "External-order payload module is missing")
        spec = importlib.util.spec_from_file_location("order_payload_under_test", MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        original_import = builtins.__import__

        def without_odoo(name, *args, **kwargs):
            if name == "odoo" or name.startswith("odoo."):
                raise AssertionError("The payload contract must load without importing Odoo")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", without_odoo):
            spec.loader.exec_module(self.module)

    def normalize(self, payload):
        return self.module.normalize_order_payload(payload)

    def assert_invalid(self, payload, path=None, secret=None):
        with self.assertRaises(self.module.PayloadValidationError) as raised:
            self.normalize(payload)
        error = raised.exception
        self.assertIsInstance(error, ValueError)
        self.assertIsInstance(error.code, str)
        self.assertIsInstance(error.path, str)
        self.assertTrue(error.code)
        self.assertTrue(error.path)
        self.assertRegex(error.code, r"\A[A-Za-z][A-Za-z0-9_.-]*\Z")
        self.assertRegex(error.path, r"\A(?:payload|[a-z_]+(?:\[[0-9]+\])?(?:\.[a-z_]+)?)\Z")
        self.assertIn(error.code, str(error))
        self.assertIn(error.path, str(error))
        if path is not None:
            self.assertEqual(error.path, path)
        if secret is not None:
            for exposed in (str(error), repr(error), error.code, error.path, repr(error.args)):
                self.assertNotIn(secret, exposed)
        return error

    def test_exact_canonical_object_json_and_sha256(self):
        expected = {
            "company_code": "PARTS-CN",
            "source_code": "B2B",
            "external_order_id": "PO-2026-001",
            "customer_ref": "BUYER-1",
            "currency": "CNY",
            "lines": [
                {"external_line_id": "10", "sku": "SEAL-8", "quantity": "1",
                 "unit_price": "0", "discount": "100"},
                {"external_line_id": "20", "sku": "轴承-6205", "quantity": "2.5",
                 "unit_price": "100", "discount": "5.5"},
            ],
        }
        normalized = self.normalize(make_payload())
        self.assertIsInstance(normalized, self.module.NormalizedOrder)
        self.assertEqual(normalized.as_dict(), expected)
        self.assertEqual(normalized.canonical_json, compact_json(expected))
        self.assertIn("轴承", normalized.canonical_json)
        self.assertEqual(normalized.payload_hash,
                         hashlib.sha256(normalized.canonical_json.encode("utf-8")).hexdigest())

    def test_equivalent_numbers_line_order_and_transport_have_the_same_hash(self):
        original = make_payload()
        equivalent = copy.deepcopy(original)
        equivalent["request_id"] = "retry-99"
        equivalent["sent_at"] = "different-audit-time"
        equivalent["lines"].reverse()
        equivalent["lines"][0].update(quantity="1", unit_price="0000", discount="100")
        equivalent["lines"][1].update(quantity="2.5", unit_price="100", discount="5.5")
        left, right = self.normalize(original), self.normalize(equivalent)
        self.assertEqual(left.canonical_json, right.canonical_json)
        self.assertEqual(left.payload_hash, right.payload_hash)
        self.assertNotEqual(left.raw_json, right.raw_json)
        del equivalent["request_id"]
        del equivalent["sent_at"]
        self.assertEqual(left.payload_hash, self.normalize(equivalent).payload_hash)

    def test_business_field_changes_change_the_hash(self):
        baseline = self.normalize(make_payload()).payload_hash
        for key, value in (("company_code", "PARTS-EU"), ("source_code", "EDI"),
                           ("external_order_id", "PO-OTHER"), ("customer_ref", "BUYER-2"),
                           ("currency", "EUR")):
            with self.subTest(top_field=key):
                payload = make_payload()
                payload[key] = value
                self.assertNotEqual(self.normalize(payload).payload_hash, baseline)
        for key, value in (("external_line_id", "21"), ("sku", "OTHER-SKU"),
                           ("quantity", "3"), ("unit_price", "101"), ("discount", "6")):
            with self.subTest(line_field=key):
                payload = make_payload()
                payload["lines"][0][key] = value
                self.assertNotEqual(self.normalize(payload).payload_hash, baseline)

    def test_raw_json_preserves_transport_number_strings_and_input_line_order(self):
        payload = make_payload()
        original = copy.deepcopy(payload)
        normalized = self.normalize(payload)
        self.assertEqual(normalized.raw_json, compact_json(original))
        self.assertEqual(json.loads(normalized.raw_json), original)
        self.assertNotIn("request_id", normalized.as_dict())
        self.assertNotIn("sent_at", normalized.as_dict())
        self.assertEqual(payload, original)

    def test_result_is_a_frozen_dataclass(self):
        normalized = self.normalize(make_payload())
        self.assertTrue(dataclasses.is_dataclass(normalized))
        for key in ("raw_json", "canonical_json", "payload_hash"):
            with self.subTest(field=key), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(normalized, key, "changed")

    def test_caller_and_decoded_results_have_no_mutable_aliases(self):
        payload = make_payload()
        normalized = self.normalize(payload)
        original_json = normalized.canonical_json
        original_raw = normalized.raw_json
        original_hash = normalized.payload_hash
        first, second = normalized.as_dict(), normalized.as_dict()
        self.assertIsNot(first, second)
        self.assertIsNot(first["lines"], second["lines"])
        self.assertIsNot(first["lines"][0], second["lines"][0])
        first["lines"][0]["quantity"] = "999"
        first["lines"].clear()
        payload["lines"][0]["sku"] = "CALLER-MUTATED"
        payload["lines"].clear()
        payload["customer_ref"] = "CALLER-MUTATED"
        self.assertEqual(normalized.as_dict(), second)
        self.assertEqual(normalized.canonical_json, original_json)
        self.assertEqual(normalized.raw_json, original_raw)
        self.assertEqual(normalized.payload_hash, original_hash)

    def test_every_required_top_field_is_required(self):
        for key in TOP_FIELDS:
            with self.subTest(field=key):
                payload = make_payload()
                del payload[key]
                self.assert_invalid(payload, key)

    def test_every_required_line_field_is_required(self):
        for key in LINE_FIELDS:
            with self.subTest(field=key):
                payload = make_payload()
                del payload["lines"][0][key]
                self.assert_invalid(payload, f"lines[0].{key}")

    def test_unknown_keys_are_rejected_without_exposing_the_key(self):
        secret = "SECRET-CUSTOMER-TOKEN-9e41"
        for level in ("top", "line"):
            with self.subTest(level=level):
                payload = make_payload()
                target = payload if level == "top" else payload["lines"][0]
                target[secret] = "ignored-value"
                self.assert_invalid(payload, "payload" if level == "top" else "lines[0]",
                                    secret=secret)

    def test_root_must_be_a_dict(self):
        for payload in (None, True, 1, "secret-root", [], (), set()):
            with self.subTest(kind=type(payload).__name__):
                self.assert_invalid(payload, "payload", secret="secret-root")

    def test_lines_must_be_a_list(self):
        for value in (None, True, 1, "secret-lines", {}, (), set()):
            with self.subTest(kind=type(value).__name__):
                payload = make_payload()
                payload["lines"] = value
                self.assert_invalid(payload, "lines", secret="secret-lines")

    def test_line_count_is_between_one_and_one_hundred(self):
        for count in (0, 101):
            with self.subTest(count=count):
                payload = make_payload()
                payload["lines"] = [dict(payload["lines"][0], external_line_id=str(index))
                                    for index in range(count)]
                self.assert_invalid(payload, "lines")
        payload = make_payload()
        payload["lines"] = [dict(payload["lines"][0], external_line_id=str(index))
                            for index in range(100)]
        self.assertEqual(len(self.normalize(payload).as_dict()["lines"]), 100)
        payload["lines"] = payload["lines"][:1]
        self.assertEqual(len(self.normalize(payload).as_dict()["lines"]), 1)

    def test_each_line_must_be_a_dict(self):
        for value in (None, True, 1, "secret-line", [], ()):
            with self.subTest(kind=type(value).__name__):
                payload = make_payload()
                payload["lines"][0] = value
                self.assert_invalid(payload, "lines[0]", secret="secret-line")

    def test_duplicate_external_line_ids_are_rejected(self):
        payload = make_payload()
        payload["lines"][1]["external_line_id"] = payload["lines"][0]["external_line_id"]
        self.assert_invalid(payload, "lines[1].external_line_id")

    def test_line_sort_is_case_sensitive_lexicographic_and_identifiers_are_preserved(self):
        payload = make_payload()
        payload["lines"] = [dict(payload["lines"][0], external_line_id=value)
                            for value in ("2", "a", "10", "A")]
        actual = self.normalize(payload).as_dict()
        self.assertEqual([line["external_line_id"] for line in actual["lines"]],
                         ["10", "2", "A", "a"])
        payload["company_code"] = "parts-cn"
        self.assertEqual(self.normalize(payload).as_dict()["company_code"], "parts-cn")
        self.assertNotEqual(self.normalize(payload).payload_hash,
                            self.normalize(dict(payload, company_code="PARTS-CN")).payload_hash)

    def test_identifiers_reject_wrong_types_empty_whitespace_controls_and_surrogates(self):
        fields = [(key, 64) for key in ("company_code", "source_code", "customer_ref", "currency")]
        fields.extend((("external_order_id", 128), ("external_line_id", 128), ("sku", 64)))
        invalid = (None, False, 7, [], {}, "", " value", "value ", "\u3000value", "value\u3000",
                   "v\x00alue", "v\talue", "v\nalue", "v\ralue", "v\x1falue", "v\x7falue",
                   "v\x85alue", "v\ud800alue", "v\udfffalue")
        for key, limit in fields:
            for value in (*invalid, "x" * (limit + 1)):
                with self.subTest(field=key, value=repr(value)):
                    payload = make_payload()
                    target = payload["lines"][0] if key in ("sku", "external_line_id") else payload
                    target[key] = value
                    path = f"lines[0].{key}" if target is not payload else key
                    self.assert_invalid(payload, path)

    def test_identifiers_accept_exact_length_unicode_and_non_cny_currency(self):
        payload = make_payload()
        for key in ("company_code", "source_code", "customer_ref", "currency"):
            payload[key] = "界" * 64
        payload["external_order_id"] = "订" * 128
        payload["lines"][0]["external_line_id"] = "行" * 128
        payload["lines"][0]["sku"] = "件" * 64
        actual = self.normalize(payload).as_dict()
        self.assertEqual(actual["currency"], "界" * 64)
        payload["currency"] = "EUR"
        self.assertEqual(self.normalize(payload).as_dict()["currency"], "EUR")

    def test_transport_accepts_empty_and_maximum_length_strings(self):
        for value in ("", "审" * 128, "  audit text  "):
            with self.subTest(value=value):
                payload = make_payload()
                payload.update(request_id=value, sent_at=value)
                raw = json.loads(self.normalize(payload).raw_json)
                self.assertEqual(raw["request_id"], value)
                self.assertEqual(raw["sent_at"], value)

    def test_transport_rejects_wrong_types_length_controls_and_surrogates(self):
        for key in ("request_id", "sent_at"):
            for value in (None, False, 7, [], {}, "x" * 129, "audit\x00secret", "audit\x1fsecret",
                          "audit\x7fsecret", "audit\x85secret", "audit\ud800secret"):
                with self.subTest(field=key, value=repr(value)):
                    payload = make_payload()
                    payload[key] = value
                    self.assert_invalid(payload, key)

    def test_numbers_are_ordinary_ascii_decimal_strings_only(self):
        invalid = (None, False, True, 1, 1.25, Decimal("1.25"), [], {},
                   "", " ", " 1", "1 ", "-1", "+1", "-0", "1e2", "1E2", "NaN", "sNaN",
                   "Infinity", "-Infinity", ".5", "1.", ".", "1.2.3", "1,000", "1_000",
                   "١", "１２", "1\n", "1\x00", "0" * 65)
        for key in ("quantity", "unit_price", "discount"):
            for value in invalid:
                with self.subTest(field=key, value=repr(value)):
                    payload = make_payload()
                    payload["lines"][0][key] = value
                    self.assert_invalid(payload, f"lines[0].{key}")

    def test_effective_fractional_precision_is_at_most_six_without_rounding(self):
        for key in ("quantity", "unit_price", "discount"):
            for value in ("1.0000001", "0.0000001", "1.1234567", "0.123456700000"):
                with self.subTest(field=key, value=value):
                    payload = make_payload()
                    payload["lines"][0][key] = value
                    self.assert_invalid(payload, f"lines[0].{key}")
        payload = make_payload()
        payload["lines"][0].update(quantity="001.123456" + "0" * 40,
                                   unit_price="0.000001" + "0" * 40,
                                   discount="99.999999" + "0" * 40)
        line = self.normalize(payload).as_dict()["lines"][1]
        self.assertEqual((line["quantity"], line["unit_price"], line["discount"]),
                         ("1.123456", "0.000001", "99.999999"))

    def test_numeric_ranges_are_checked_exactly(self):
        cases = {"quantity": ("0", "000.000000", "1000000000.000001"),
                 "unit_price": ("1000000000.000001",),
                 "discount": ("100.000001",)}
        for key, values in cases.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    payload = make_payload()
                    payload["lines"][0][key] = value
                    self.assert_invalid(payload, f"lines[0].{key}")

    def test_exact_numeric_edges_and_integer_trailing_zeros_are_preserved(self):
        payload = make_payload()
        payload["lines"][0].update(quantity="1000000000.000000", unit_price="1000000000",
                                   discount="100")
        payload["lines"][1].update(quantity="0.000001", unit_price="0000.000000", discount="0")
        lines = self.normalize(payload).as_dict()["lines"]
        self.assertEqual((lines[0]["quantity"], lines[0]["unit_price"], lines[0]["discount"]),
                         ("0.000001", "0", "0"))
        self.assertEqual((lines[1]["quantity"], lines[1]["unit_price"], lines[1]["discount"]),
                         ("1000000000", "1000000000", "100"))

    def test_decimal_context_precision_cannot_round_normalization_or_range_checks(self):
        payload = make_payload()
        payload["lines"][0].update(quantity="123456789.123456", unit_price="999999999.999999",
                                   discount="99.123456")
        with localcontext() as context:
            context.prec = 2
            line = self.normalize(payload).as_dict()["lines"][1]
            self.assertEqual(line["quantity"], "123456789.123456")
            self.assertEqual(line["unit_price"], "999999999.999999")
            self.assertEqual(line["discount"], "99.123456")
            payload["lines"][0]["unit_price"] = "1000000000.000001"
            self.assert_invalid(payload, "lines[0].unit_price")

    def test_sixty_four_character_numeric_strings_are_accepted(self):
        payload = make_payload()
        payload["lines"][0].update(quantity="0" * 63 + "1", unit_price="0" * 64,
                                   discount="0" * 61 + "100")
        line = self.normalize(payload).as_dict()["lines"][1]
        self.assertEqual((line["quantity"], line["unit_price"], line["discount"]),
                         ("1", "0", "100"))

    def test_utf8_byte_limit_rejects_otherwise_valid_bounded_unicode_payload(self):
        payload = make_payload()
        payload["lines"] = [
            {"external_line_id": f"{index:03d}" + "订" * 125, "sku": "件" * 64,
             "quantity": "0" * 63 + "1", "unit_price": "0" * 64, "discount": "0" * 64}
            for index in range(100)
        ]
        self.assertLess(len(compact_json(payload)), 65536)
        self.assertGreater(len(compact_json(payload).encode("utf-8")), 65536)
        self.assert_invalid(payload, "payload")

    def test_shape_and_field_bounds_are_checked_before_serializing_untrusted_input(self):
        cycle = []
        cycle.append(cycle)
        cases = []
        payload = make_payload()
        payload["lines"][0]["sku"] = "secret" * 20000
        cases.append((payload, "lines[0].sku"))
        payload = make_payload()
        payload["lines"] = [cycle]
        cases.append((payload, "lines[0]"))
        payload = make_payload()
        payload["secret-unknown-key"] = cycle
        cases.append((payload, "payload"))
        payload = make_payload()
        payload["customer_ref"] = cycle
        cases.append((payload, "customer_ref"))
        for payload, path in cases:
            with self.subTest(path=path), patch("json.dumps", side_effect=AssertionError(
                    "Invalid shape or field bounds must be rejected before JSON serialization")):
                self.assert_invalid(payload, path, secret="secret")

    def test_error_api_is_safe_and_consistent_for_sensitive_values(self):
        secret = "SECRET-PAYMENT-CUSTOMER-83ab"
        for key, value in (("quantity", secret), ("sku", " " + secret),
                           ("external_line_id", secret + "\x00")):
            with self.subTest(field=key):
                payload = make_payload()
                payload["lines"][0][key] = value
                first = self.assert_invalid(payload, f"lines[0].{key}", secret=secret)
                second = self.assert_invalid(payload, f"lines[0].{key}", secret=secret)
                self.assertEqual((first.code, first.path, str(first)),
                                 (second.code, second.path, str(second)))


if __name__ == "__main__":
    unittest.main()
