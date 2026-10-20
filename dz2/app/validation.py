"""Validate JSON types and all constraints directly from the OpenAPI contract."""

import json
import re
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft4Validator, FormatChecker
from .errors import invalid, fail

SPEC_PATH = (
    Path(__file__).resolve().parents[1] / "src/main/resources/openapi/openapi.json"
)
SPEC = json.loads(SPEC_PATH.read_text())


def normalize(value):
    if isinstance(value, list):
        return [normalize(x) for x in value]
    if not isinstance(value, dict):
        return value
    result = {k: normalize(v) for k, v in value.items() if k != "nullable"}
    if value.get("nullable"):
        return {"anyOf": [result, {"type": "null"}]}
    return result


VALIDATION_SPEC = normalize(json.loads(SPEC_PATH.read_text(), parse_float=Decimal))


def validate_body(path, method, raw):
    operation = None
    for template, methods in SPEC["paths"].items():
        if re.fullmatch(template.replace("{id}", "[^/]+"), path):
            operation = methods.get(method.lower())
            break
    if not operation or "requestBody" not in operation:
        return
    try:
        value = json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeDecodeError):
        invalid("body", "Request body must be valid JSON")
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    validator = Draft4Validator(
        dict(VALIDATION_SPEC, **schema), format_checker=FormatChecker()
    )
    errors = list(validator.iter_errors(value))
    if errors:
        violations = [
            {
                "field": ".".join(map(str, e.absolute_path)) or "body",
                "message": e.message,
            }
            for e in errors
        ]
        fail(400, "VALIDATION_ERROR", "Invalid request", {"violations": violations})
    model = SPEC["components"]["schemas"][schema["$ref"].split("/")[-1]]
    for name, field in model.get("properties", {}).items():
        if "x-unique-by" in field and name in value:
            key = field["x-unique-by"]
            keys = [str(i[key]).lower() for i in value[name]]
            if len(set(keys)) != len(keys):
                invalid(name, f"Duplicate {key} values are not allowed")
