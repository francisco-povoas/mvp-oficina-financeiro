import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

CONTRACTS = Path("contracts")
SCHEMA_PATH = CONTRACTS / "messages" / "financial-events.v1.schema.json"
EXAMPLES_PATH = CONTRACTS / "messages" / "financial-events.v1.examples.json"

EXPECTED_TYPES = {
    "QuoteCommunicated",
    "QuoteApproved",
    "QuoteRejected",
    "PaymentConfirmed",
}


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def test_financial_schema_is_valid_and_covers_all_events() -> None:
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    assert EXPECTED_TYPES.issubset(schema["$defs"])


def test_every_financial_example_is_valid() -> None:
    validator = Draft202012Validator(
        _load(SCHEMA_PATH),
        format_checker=FormatChecker(),
    )
    examples = _load(EXAMPLES_PATH)

    assert {example["message_type"] for example in examples} == EXPECTED_TYPES
    for example in examples:
        validator.validate(example)


@pytest.mark.parametrize("mutation", ["unknown_version", "missing_payload_field"])
def test_every_financial_example_has_an_invalid_counterexample(mutation: str) -> None:
    validator = Draft202012Validator(
        _load(SCHEMA_PATH),
        format_checker=FormatChecker(),
    )

    for original in _load(EXAMPLES_PATH):
        invalid = copy.deepcopy(original)
        if mutation == "unknown_version":
            invalid["message_version"] = 2
        else:
            invalid["payload"].pop(next(iter(invalid["payload"])))
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_common_envelope_and_problem_schemas_are_valid() -> None:
    for path in sorted((CONTRACTS / "common").glob("*.schema.json")):
        Draft202012Validator.check_schema(_load(path))

