"""Validate bounded external orders and hash their precise business meaning."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

_REQUIRED_FIELDS = frozenset({
    "company_code", "source_code", "external_order_id", "customer_ref", "currency", "lines",
})
_TRANSPORT_FIELDS = ("request_id", "sent_at")
_ALLOWED_FIELDS = _REQUIRED_FIELDS.union(_TRANSPORT_FIELDS)
_LINE_FIELDS = frozenset({"external_line_id", "sku", "quantity", "unit_price", "discount"})
_DECIMAL_STRING = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_MAX_JSON_BYTES = 65536


class PayloadValidationError(ValueError):
    """Expose only a stable validation code and an internally selected path."""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = path
        super().__init__(f"{code}: {path}")


@dataclass(frozen=True)
class NormalizedOrder:
    raw_json: str
    canonical_json: str
    payload_hash: str

    def as_dict(self) -> dict:
        """Decode a new independent business payload on every call."""
        return json.loads(self.canonical_json)


def _require_fields(value: dict, required: frozenset, allowed: frozenset, path: str):
    if any(key not in allowed for key in value):
        raise PayloadValidationError("unknown_field", path)
    for key in sorted(required):
        if key not in value:
            field_path = key if path == "payload" else f"{path}.{key}"
            raise PayloadValidationError("missing_field", field_path)


def _string(value, path: str, limit: int, *, identifier: bool = True) -> str:
    if not isinstance(value, str):
        raise PayloadValidationError("invalid_string", path)
    if len(value) > limit:
        raise PayloadValidationError("string_too_long", path)
    if identifier and (not value or value.strip() != value):
        raise PayloadValidationError("invalid_identifier", path)
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise PayloadValidationError("unsupported_character", path)
    return value


def _number(value, path: str, maximum: int, *, positive: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise PayloadValidationError("invalid_decimal", path)
    if _DECIMAL_STRING.fullmatch(value) is None:
        raise PayloadValidationError("invalid_decimal", path)
    integer, _, fraction = value.partition(".")
    fraction = fraction.rstrip("0")
    if len(fraction) > 6:
        raise PayloadValidationError("decimal_precision", path)
    number = Decimal(value)
    if number > maximum or (number <= 0 if positive else number < 0):
        raise PayloadValidationError("decimal_range", path)
    integer = integer.lstrip("0") or "0"
    return f"{integer}.{fraction}" if fraction else integer


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def normalize_order_payload(payload) -> NormalizedOrder:
    """Validate the finite schema before encoding and hashing canonical UTF-8 JSON."""
    if not isinstance(payload, dict):
        raise PayloadValidationError("invalid_object", "payload")
    _require_fields(payload, _REQUIRED_FIELDS, _ALLOWED_FIELDS, "payload")

    raw = {}
    for key, limit in (("company_code", 64), ("source_code", 64),
                       ("external_order_id", 128), ("customer_ref", 64), ("currency", 64)):
        raw[key] = _string(payload[key], key, limit)
    canonical = dict(raw)
    for key in _TRANSPORT_FIELDS:
        if key in payload:
            raw[key] = _string(payload[key], key, 128, identifier=False)

    lines = payload["lines"]
    if not isinstance(lines, list):
        raise PayloadValidationError("invalid_array", "lines")
    if not 1 <= len(lines) <= 100:
        raise PayloadValidationError("line_count", "lines")
    raw_lines, canonical_lines = [], []
    line_ids = set()
    for index, line in enumerate(lines):
        path = f"lines[{index}]"
        if not isinstance(line, dict):
            raise PayloadValidationError("invalid_object", path)
        _require_fields(line, _LINE_FIELDS, _LINE_FIELDS, path)
        line_id = _string(line["external_line_id"], f"{path}.external_line_id", 128)
        if line_id in line_ids:
            raise PayloadValidationError("duplicate_line_id", f"{path}.external_line_id")
        line_ids.add(line_id)
        raw_line = {
            "external_line_id": line_id,
            "sku": _string(line["sku"], f"{path}.sku", 64),
        }
        canonical_line = dict(raw_line)
        for key, maximum in (("quantity", 10**9), ("unit_price", 10**9), ("discount", 100)):
            canonical_line[key] = _number(line[key], f"{path}.{key}", maximum,
                                          positive=key == "quantity")
            raw_line[key] = line[key]
        raw_lines.append(raw_line)
        canonical_lines.append(canonical_line)
    raw["lines"] = raw_lines
    canonical["lines"] = sorted(canonical_lines, key=lambda line: line["external_line_id"])

    raw_json = _json(raw)
    if len(raw_json.encode("utf-8")) > _MAX_JSON_BYTES:
        raise PayloadValidationError("payload_too_large", "payload")
    canonical_json = _json(canonical)
    payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return NormalizedOrder(raw_json, canonical_json, payload_hash)
