"""Generate DTOs and FastAPI handlers exclusively from the checked-in contract."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec_path = ROOT / "src/main/resources/openapi/openapi.json"
spec = json.loads(spec_path.read_text())
out = ROOT / "app/generated"
out.mkdir(exist_ok=True)
(out / "__init__.py").write_text("")
subprocess.run(
    [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(spec_path),
        "--input-file-type",
        "openapi",
        "--output",
        str(out / "models.py"),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--base-class",
        "app.model_base.ContractModel",
        "--use-standard-collections",
        "--use-union-operator",
        "--target-python-version",
        "3.12",
        "--strict-nullable",
        "--disable-timestamp",
    ],
    check=True,
)
lines = [
    "# Generated from OpenAPI. Do not edit.",
    "from uuid import UUID",
    "from typing import Literal",
    "from fastapi import APIRouter, Request, Query",
    "from . import models",
    "from app import service",
    "router = APIRouter()",
]
for path, operations in spec["paths"].items():
    for method, op in operations.items():
        args = ["request: Request"]
        forwarded = []
        if "requestBody" in op:
            model = op["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].split("/")[-1]
            args.append(f"body: models.{model}")
            forwarded.append("body=body")
        for p in op.get("parameters", []):
            s = p["schema"]
            name = p["name"]
            typ = (
                "UUID"
                if s.get("format") == "uuid"
                else "int" if s["type"] == "integer" else "str"
            )
            if "enum" in s:
                typ = "Literal[" + ", ".join(repr(x) for x in s["enum"]) + "]"
            if p["in"] == "path":
                args.append(f"{name}: {typ}")
            else:
                default = s.get("default")
                if default is None:
                    typ += " | None"
                constraints = ", ".join(
                    f"{b}={s[a]!r}"
                    for a, b in [
                        ("minimum", "ge"),
                        ("maximum", "le"),
                        ("minLength", "min_length"),
                        ("maxLength", "max_length"),
                    ]
                    if a in s
                )
                args.append(
                    f"{name}: {typ} = Query({default!r}"
                    + (", " + constraints if constraints else "")
                    + ")"
                )
            forwarded.append(f"{name}={name}")
        success = next(k for k in op["responses"] if k.startswith("2"))
        schema = (
            op["responses"][success]
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        response = "models." + schema["$ref"].split("/")[-1] if schema else "None"
        lines.extend(
            [
                f"@router.{method}({path!r}, status_code={success}, response_model={response})",
                f'def {op["operationId"]}({", ".join(args)}):',
                f'    return service.{op["operationId"]}(request'
                + (", " + ", ".join(forwarded) if forwarded else "")
                + ")",
                "",
            ]
        )
(out / "routes.py").write_text("\n".join(lines))
print("Generated models and handlers from", spec_path)
