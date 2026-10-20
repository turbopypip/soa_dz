"""HTTP E2E demonstration followed by actual SELECTs from PostgreSQL."""

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row

base = os.getenv("API_URL", "http://localhost:8080")
client = httpx.Client(base_url=base, timeout=20)
suffix = uuid4().hex[:8]


def request(method, path, token=None, body=None, expected=200):
    response = client.request(
        method,
        path,
        json=body,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    print(
        f'{method} {path} -> {response.status_code}; X-Request-Id: {response.headers.get("X-Request-Id")}'
    )
    assert response.status_code == expected, response.text
    result = response.json() if response.content else None
    if result:
        # Never print tokens or credentials in demo output.
        print(
            json.dumps(
                {k: v for k, v in result.items() if "token" not in k},
                ensure_ascii=False,
                indent=2,
            )
        )
    return result


def account(role):
    credentials = {
        "username": f"demo_{role.lower()}_{suffix}",
        "password": "demo-password",
    }
    user = request(
        "POST", "/auth/register", body=dict(credentials, role=role), expected=201
    )
    pair = request("POST", "/auth/login", body=credentials)
    return pair["access_token"], user["id"]


seller, seller_id = account("SELLER")
user, user_id = account("USER")
p = request(
    "POST",
    "/products",
    seller,
    {
        "name": "Demo keyboard",
        "price": 1500.50,
        "stock": 10,
        "category": "keyboards",
        "status": "ACTIVE",
    },
    201,
)
now = datetime.now(timezone.utc)
pr = request(
    "POST",
    "/promo-codes",
    seller,
    {
        "code": "DEMO_" + suffix.upper(),
        "discount_type": "PERCENTAGE",
        "discount_value": 10,
        "min_order_amount": 1000,
        "max_uses": 5,
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "valid_until": (now + timedelta(days=1)).isoformat(),
    },
    201,
)
body = {"items": [{"product_id": p["id"], "quantity": 2}], "promo_code": pr["code"]}
order = request("POST", "/orders", user, body, 201)
assert order["total_amount"] == 2700.90
request("GET", "/products/" + p["id"], user)
request(
    "PUT",
    "/orders/" + order["id"],
    user,
    {"items": [{"product_id": p["id"], "quantity": 3}]},
)
# Creation rate limiting is demonstrated without changing server configuration.
error = request("POST", "/orders", user, body, 429)
assert error["error_code"] == "ORDER_LIMIT_EXCEEDED"
request("POST", "/orders/" + order["id"] + "/cancel", user)
assert request("GET", "/products/" + p["id"], user)["stock"] == 10
request("DELETE", "/products/" + p["id"], seller, expected=204)
assert request("GET", "/products/" + p["id"], user)["status"] == "ARCHIVED"
with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
    queries = [
        ("products", "SELECT * FROM products WHERE id=%s", p["id"]),
        ("orders", "SELECT * FROM orders WHERE id=%s", order["id"]),
        ("order_items", "SELECT * FROM order_items WHERE order_id=%s", order["id"]),
        ("promo_codes", "SELECT * FROM promo_codes WHERE id=%s", pr["id"]),
        (
            "user_operations",
            "SELECT * FROM user_operations WHERE user_id=%s ORDER BY created_at",
            user_id,
        ),
    ]
    for table, query, value in queries:
        print("\n" + query + "  parameter=" + value)
        print(
            json.dumps(
                conn.execute(query, (value,)).fetchall(),
                default=str,
                ensure_ascii=False,
                indent=2,
            )
        )
print("\nE2E OK: HTTP requests, stock restoration, soft delete and SELECTs verified.")
