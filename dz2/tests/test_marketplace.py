"""Integration tests use real PostgreSQL migrated by Flyway; no mocked storage.
Records have unique names; tests do not truncate or modify unrelated records.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app, logger
from app.db import transaction
from app.validation import SPEC, normalize
from jsonschema import Draft4Validator, FormatChecker


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ORDER_RATE_LIMIT_MINUTES", "0")
    with TestClient(app) as c:
        yield c


def call(c, method, path, token=None, body=None, status=200, code=None):
    response = c.request(
        method,
        path,
        headers={"Authorization": f"Bearer {token}"} if token else {},
        json=body,
    )
    assert response.status_code == status, response.text
    assert response.headers.get("X-Request-Id")
    if status == 204:
        assert response.content == b""
        return None
    result = response.json()
    if code:
        assert result["error_code"] == code, result
    # Check every response against the authored contract, including decimal number type.
    import re

    for template, operations in SPEC["paths"].items():
        if re.fullmatch(template.replace("{id}", "[^/]+"), path.split("?")[0]):
            schema = operations[method.lower()]["responses"][str(status)]["content"][
                "application/json"
            ]["schema"]
            Draft4Validator(
                dict(normalize(SPEC), **schema), format_checker=FormatChecker()
            ).validate(result)
            break
    return result


def account(c, role="USER"):
    body = {
        "username": "test_" + uuid4().hex,
        "password": "test-password",
        "role": role,
    }
    user = call(c, "POST", "/auth/register", body=body, status=201)
    pair = call(
        c, "POST", "/auth/login", body={k: body[k] for k in ["username", "password"]}
    )
    return pair["access_token"], user["id"]


def admin(c):
    return call(
        c,
        "POST",
        "/auth/login",
        body={
            "username": os.environ["ADMIN_USERNAME"],
            "password": os.environ["ADMIN_PASSWORD"],
        },
    )["access_token"]


def product(c, token, **changes):
    body = dict(
        name="Keyboard",
        description=None,
        price=100,
        stock=10,
        category="test_" + uuid4().hex,
        status="ACTIVE",
    )
    body.update(changes)
    return call(c, "POST", "/products", token, body, 201)


def promo(c, token, **changes):
    now = datetime.now(timezone.utc)
    body = dict(
        code=uuid4().hex[:12].upper(),
        discount_type="PERCENTAGE",
        discount_value=10,
        min_order_amount=100,
        max_uses=1,
        valid_from=(now - timedelta(days=1)).isoformat(),
        valid_until=(now + timedelta(days=1)).isoformat(),
        active=True,
    )
    body.update(changes)
    return call(c, "POST", "/promo-codes", token, body, 201)


def items(p, quantity=1):
    return {"items": [{"product_id": p["id"], "quantity": quantity}]}


def stock(c, token, p):
    return call(c, "GET", "/products/" + p["id"], token)["stock"]


def sql(query, params=()):
    with transaction() as conn:
        return conn.execute(query, params).fetchall()


def test_products_crud_roles_and_pagination(client):
    c = client
    seller, sid = account(c, "SELLER")
    buyer, _ = account(c)
    other, _ = account(c, "SELLER")
    p = product(c, seller, price=0.01)
    assert p["seller_id"] == sid and p["price"] == 0.01
    page = call(
        c,
        "GET",
        "/products?category=" + p["category"] + "&status=ACTIVE&page=0&size=1",
        buyer,
    )
    assert page["totalElements"] == 1 and page["content"][0]["id"] == p["id"]
    assert (
        call(c, "GET", "/products?category=" + p["category"] + "&page=1&size=1", buyer)[
            "content"
        ]
        == []
    )
    update = {
        k: p[k] for k in ["name", "description", "price", "stock", "category", "status"]
    }
    update["price"] = 123.45
    call(c, "PUT", "/products/" + p["id"], buyer, update, 403, "ACCESS_DENIED")
    call(c, "PUT", "/products/" + p["id"], other, update, 403, "ACCESS_DENIED")
    changed = call(c, "PUT", "/products/" + p["id"], seller, update)
    assert (
        changed["created_at"] == p["created_at"]
        and changed["updated_at"] > p["updated_at"]
    )
    call(c, "DELETE", "/products/" + p["id"], admin(c), status=204)
    assert call(c, "GET", "/products/" + p["id"], buyer)["status"] == "ARCHIVED"
    call(
        c,
        "GET",
        "/products/" + str(uuid4()),
        buyer,
        status=404,
        code="PRODUCT_NOT_FOUND",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "a" * 256),
        ("description", "a" * 4001),
        ("price", 0),
        ("price", -0.01),
        ("price", 0.001),
        ("price", "1.00"),
        ("stock", -1),
        ("stock", 1.5),
        ("stock", True),
        ("category", ""),
        ("category", "x" * 101),
        ("status", "UNKNOWN"),
    ],
)
def test_product_validation(client, field, value):
    seller, _ = account(client, "SELLER")
    body = dict(name="p", price=1, stock=1, category="c", status="ACTIVE")
    body[field] = value
    error = call(client, "POST", "/products", seller, body, 400, "VALIDATION_ERROR")
    assert error["details"]["violations"]


def test_auth_and_role_security(client):
    c = client
    user, _ = account(c)
    seller, _ = account(c, "SELLER")
    call(c, "GET", "/products", status=401, code="TOKEN_INVALID")
    call(c, "GET", "/products", "garbage", status=401, code="TOKEN_INVALID")
    claims = jwt.decode(
        user, os.environ["JWT_SECRET"], algorithms=["HS256"], audience="marketplace-api"
    )
    claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")
    call(c, "GET", "/products", expired, status=401, code="TOKEN_EXPIRED")
    call(
        c,
        "POST",
        "/auth/refresh",
        body={"refresh_token": user},
        status=401,
        code="REFRESH_TOKEN_INVALID",
    )
    pair = call(
        c,
        "POST",
        "/auth/login",
        body={
            "username": os.environ["ADMIN_USERNAME"],
            "password": os.environ["ADMIN_PASSWORD"],
        },
    )
    call(c, "GET", "/products", pair["refresh_token"], status=401, code="TOKEN_INVALID")
    renewed = call(
        c, "POST", "/auth/refresh", body={"refresh_token": pair["refresh_token"]}
    )
    call(c, "GET", "/products", renewed["access_token"])
    call(
        c,
        "POST",
        "/auth/register",
        body={"username": "evil", "password": "password1", "role": "ADMIN"},
        status=400,
        code="VALIDATION_ERROR",
    )
    call(
        c,
        "POST",
        "/orders",
        seller,
        {"items": [{"product_id": str(uuid4()), "quantity": 1}]},
        403,
        "ACCESS_DENIED",
    )
    call(c, "GET", "/orders/" + str(uuid4()), user, status=404, code="ORDER_NOT_FOUND")


def test_order_snapshots_promo_removal_and_cancel(client):
    c = client
    seller, _ = account(c, "SELLER")
    user, uid = account(c)
    p = product(c, seller)
    pr = promo(c, seller, min_order_amount=200)
    order = call(
        c, "POST", "/orders", user, dict(items(p, 3), promo_code=pr["code"]), 201
    )
    assert (
        order["total_amount"] == 270
        and order["discount_amount"] == 30
        and stock(c, user, p) == 7
    )
    assert (
        sql("SELECT current_uses FROM promo_codes WHERE id=%s", (pr["id"],))[0][
            "current_uses"
        ]
        == 1
    )
    changed = {
        k: p[k] for k in ["name", "description", "price", "stock", "category", "status"]
    }
    changed.update(price=500, stock=7)
    call(c, "PUT", "/products/" + p["id"], seller, changed)
    updated = call(c, "PUT", "/orders/" + order["id"], user, items(p, 2))
    assert (
        updated["total_amount"] == 180 and updated["items"][0]["price_at_order"] == 100
    )
    updated = call(c, "PUT", "/orders/" + order["id"], user, items(p))
    assert updated["promo_code_id"] is None and updated["total_amount"] == 100
    assert (
        sql("SELECT current_uses FROM promo_codes WHERE id=%s", (pr["id"],))[0][
            "current_uses"
        ]
        == 0
    )
    call(c, "POST", "/orders/" + order["id"] + "/cancel", user)
    assert stock(c, user, p) == 10
    call(
        c,
        "POST",
        "/orders/" + order["id"] + "/cancel",
        user,
        status=409,
        code="INVALID_STATE_TRANSITION",
    )
    assert stock(c, user, p) == 10
    assert len(sql("SELECT * FROM user_operations WHERE user_id=%s", (uid,))) == 3


def test_failures_roll_back_everything(client):
    c = client
    seller, _ = account(c, "SELLER")
    user, uid = account(c)
    p = product(c, seller)
    p2 = product(c, seller, stock=0)
    call(
        c,
        "POST",
        "/orders",
        user,
        {"items": items(p)["items"] + items(p2)["items"]},
        409,
        "INSUFFICIENT_STOCK",
    )
    call(
        c,
        "POST",
        "/orders",
        user,
        dict(items(p), promo_code="MISSING"),
        422,
        "PROMO_CODE_INVALID",
    )
    assert stock(c, user, p) == 10
    assert sql("SELECT * FROM user_operations WHERE user_id=%s", (uid,)) == []
    order = call(c, "POST", "/orders", user, items(p, 2), 201)
    call(c, "PUT", "/orders/" + order["id"], user, items(p2), 409, "INSUFFICIENT_STOCK")
    assert stock(c, user, p) == 8
    assert call(c, "GET", "/orders/" + order["id"], user) == order
    inactive = product(c, seller, status="INACTIVE")
    call(
        c,
        "PUT",
        "/orders/" + order["id"],
        user,
        items(inactive),
        409,
        "PRODUCT_INACTIVE",
    )
    call(
        c,
        "PUT",
        "/orders/" + order["id"],
        user,
        {"items": [{"product_id": str(uuid4()), "quantity": 1}]},
        404,
        "PRODUCT_NOT_FOUND",
    )
    assert stock(c, user, p) == 8


def test_rate_limits_and_check_precedence(client, monkeypatch):
    c = client
    seller, _ = account(c, "SELLER")
    user, _ = account(c)
    other, _ = account(c)
    p = product(c, seller)
    order = call(c, "POST", "/orders", user, items(p), 201)
    call(c, "POST", "/orders", user, items(p), 409, "ORDER_HAS_ACTIVE")
    monkeypatch.setenv("ORDER_RATE_LIMIT_MINUTES", "1")
    call(c, "POST", "/orders", user, items(p), 429, "ORDER_LIMIT_EXCEEDED")
    call(c, "PUT", "/orders/" + order["id"], user, items(p, 2))
    call(
        c, "PUT", "/orders/" + order["id"], user, items(p), 429, "ORDER_LIMIT_EXCEEDED"
    )
    call(
        c,
        "PUT",
        "/orders/" + order["id"],
        other,
        items(p),
        403,
        "ORDER_OWNERSHIP_VIOLATION",
    )
    for method, suffix in [("GET", ""), ("POST", "/cancel")]:
        call(
            c,
            method,
            "/orders/" + order["id"] + suffix,
            other,
            status=403,
            code="ORDER_OWNERSHIP_VIOLATION",
        )


def test_state_machine(client):
    c = client
    seller, _ = account(c, "SELLER")
    user, _ = account(c)
    a = admin(c)
    p = product(c, seller)
    o = call(c, "POST", "/orders", user, items(p), 201)
    path = "/orders/" + o["id"]
    call(
        c,
        "POST",
        path + "/status",
        user,
        {"status": "PAYMENT_PENDING"},
        403,
        "ACCESS_DENIED",
    )
    call(
        c,
        "POST",
        path + "/status",
        a,
        {"status": "SHIPPED"},
        409,
        "INVALID_STATE_TRANSITION",
    )
    for state in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
        assert (
            call(c, "POST", path + "/status", a, {"status": state})["status"] == state
        )
        call(c, "PUT", path, user, items(p), 409, "INVALID_STATE_TRANSITION")
    call(c, "POST", path + "/cancel", user, status=409, code="INVALID_STATE_TRANSITION")
    assert stock(c, user, p) == 9
    o2 = call(c, "POST", "/orders", user, items(p), 201)
    call(c, "POST", "/orders/" + o2["id"] + "/status", a, {"status": "PAYMENT_PENDING"})
    call(c, "POST", "/orders/" + o2["id"] + "/cancel", a)
    assert stock(c, user, p) == 9


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("PERCENTAGE", 70, 70),
        ("PERCENTAGE", 71, 0),
        ("FIXED_AMOUNT", 500, 100),
        ("PERCENTAGE", 12.34, 12.34),
    ],
)
def test_discount_boundaries(client, kind, value, expected):
    c = client
    seller, _ = account(c, "SELLER")
    user, _ = account(c)
    p = product(c, seller)
    pr = promo(c, seller, discount_type=kind, discount_value=value)
    o = call(c, "POST", "/orders", user, dict(items(p), promo_code=pr["code"]), 201)
    assert o["discount_amount"] == expected and o["total_amount"] == 100 - expected
    call(c, "POST", "/orders/" + o["id"] + "/cancel", user)
    assert (
        sql("SELECT current_uses FROM promo_codes WHERE id=%s", (pr["id"],))[0][
            "current_uses"
        ]
        == 0
    )


def test_promo_invalid_minimum_and_update_expiry(client):
    c = client
    seller, _ = account(c, "SELLER")
    user, _ = account(c)
    p = product(c, seller)
    for changes in [
        {"active": False},
        {"valid_until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
    ]:
        pr = promo(c, seller, **changes)
        call(
            c,
            "POST",
            "/orders",
            user,
            dict(items(p), promo_code=pr["code"]),
            422,
            "PROMO_CODE_INVALID",
        )
    pr = promo(c, seller, min_order_amount=200)
    call(
        c,
        "POST",
        "/orders",
        user,
        dict(items(p), promo_code=pr["code"]),
        422,
        "PROMO_CODE_MIN_AMOUNT",
    )
    o = call(c, "POST", "/orders", user, dict(items(p, 2), promo_code=pr["code"]), 201)
    with transaction(write=True) as conn:
        conn.execute("UPDATE promo_codes SET active=false WHERE id=%s", (pr["id"],))
    call(c, "PUT", "/orders/" + o["id"], user, items(p, 3), 422, "PROMO_CODE_INVALID")
    assert stock(c, user, p) == 8


@pytest.mark.parametrize(
    "body",
    [
        {"items": []},
        {"items": [{"product_id": str(uuid4()), "quantity": 0}]},
        {"items": [{"product_id": str(uuid4()), "quantity": 1000}]},
        {"items": [{"product_id": "bad", "quantity": 1}]},
        {"items": [{"product_id": str(uuid4()), "quantity": 1}] * 51},
        {"items": [{"product_id": str(uuid4()), "quantity": 1}], "promo_code": "bad"},
    ],
)
def test_order_validation(client, body):
    token, _ = account(client)
    call(client, "POST", "/orders", token, body, 400, "VALIDATION_ERROR")


def test_duplicate_products_and_query_validation(client):
    c = client
    seller, _ = account(c, "SELLER")
    user, _ = account(c)
    p = product(c, seller)
    call(
        c,
        "POST",
        "/orders",
        user,
        {"items": items(p)["items"] * 2},
        400,
        "VALIDATION_ERROR",
    )
    assert stock(c, user, p) == 10
    for query in ["page=-1", "size=0", "size=101", "status=INVALID"]:
        call(c, "GET", "/products?" + query, user, status=400, code="VALIDATION_ERROR")
    call(c, "GET", "/products/not-a-uuid", user, status=400, code="VALIDATION_ERROR")


def test_concurrent_stock_and_single_active_order(client):
    c = client
    seller, _ = account(c, "SELLER")
    p = product(c, seller, stock=1)
    buyers = [account(c)[0] for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda t: c.post(
                    "/orders", headers={"Authorization": f"Bearer {t}"}, json=items(p)
                ),
                buyers,
            )
        )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert (
        next(r for r in responses if r.status_code == 409).json()["error_code"]
        == "INSUFFICIENT_STOCK"
    )
    assert stock(c, seller, p) == 0
    p2 = product(c, seller)
    user, _ = account(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: c.post(
                    "/orders",
                    headers={"Authorization": f"Bearer {user}"},
                    json=items(p2),
                ),
                range(2),
            )
        )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert (
        next(r for r in responses if r.status_code == 409).json()["error_code"]
        == "ORDER_HAS_ACTIVE"
    )
    assert stock(c, seller, p2) == 9


def test_concurrent_promo_last_use(client):
    c = client
    seller, _ = account(c, "SELLER")
    p = product(c, seller)
    pr = promo(c, seller)
    buyers = [account(c)[0] for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda t: c.post(
                    "/orders",
                    headers={"Authorization": f"Bearer {t}"},
                    json=dict(items(p), promo_code=pr["code"]),
                ),
                buyers,
            )
        )
    assert sorted(r.status_code for r in responses) == [201, 422]
    assert stock(c, seller, p) == 9
    assert (
        sql("SELECT current_uses FROM promo_codes WHERE id=%s", (pr["id"],))[0][
            "current_uses"
        ]
        == 1
    )


def test_json_logging_redaction(client):
    import logging

    events = []

    class Capture(logging.Handler):
        def emit(self, record):
            events.append(json.loads(record.getMessage()))

    handler = Capture()
    logger.addHandler(handler)
    try:
        response = client.post(
            "/auth/login",
            json={"username": "nonexistent", "password": "secret-password"},
        )
        event = events[-1]
        assert event["request_body"]["password"] == "***"
        assert event["request_id"] == response.headers["X-Request-Id"]
        assert {
            "method",
            "endpoint",
            "status_code",
            "duration_ms",
            "user_id",
            "timestamp",
        } <= event.keys()
        assert event["user_id"] is None and event["status_code"] == 401
        client.post(
            "/auth/login",
            content='{"password":"secret-password",broken',
            headers={"Content-Type": "application/json"},
        )
        assert "secret-password" not in json.dumps(events)
    finally:
        logger.removeHandler(handler)
