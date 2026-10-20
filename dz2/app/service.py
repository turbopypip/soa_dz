"""Business operations. Request and response DTOs/HTTP handlers are generated."""

import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from .db import transaction
from .errors import fail, invalid
from .security import passwords, DUMMY_HASH, require, token_pair, decode_token

CENT = Decimal("0.01")
MAX_MONEY = Decimal("9999999999.99")


def data(body):
    # Preserve Decimal and UUID values for psycopg; unwrap generated enums.
    return {k: getattr(v, "value", v) for k, v in body.model_dump().items()}


def register(request, body):
    d = data(body)
    with transaction(write=True) as conn:
        row = conn.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING RETURNING id,username,role",
            (
                uuid4(),
                d["username"],
                passwords.hash(body.password.get_secret_value()),
                d["role"],
            ),
        ).fetchone()
        if not row:
            fail(409, "USERNAME_TAKEN", "Username already exists")
        return row


def login(request, body):
    with transaction() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username=%s", (body.username,)
        ).fetchone()
    valid = passwords.verify(
        body.password.get_secret_value(), user["password_hash"] if user else DUMMY_HASH
    )
    if not user or not valid:
        fail(401, "TOKEN_INVALID", "Invalid username or password")
    return token_pair(user)


def refresh(request, body):
    claims = decode_token(body.refresh_token, "refresh")
    with transaction() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id=%s", (claims["sub"],)
        ).fetchone()
    if not user or user["role"] != claims["role"]:
        fail(401, "REFRESH_TOKEN_INVALID", "Invalid refresh token user")
    return token_pair(user)


def product(conn, id):
    row = conn.execute("SELECT * FROM products WHERE id=%s", (id,)).fetchone()
    if not row:
        fail(404, "PRODUCT_NOT_FOUND", "Product not found", {"product_id": str(id)})
    return row


def product_owner(user, row):
    if user["role"] != "ADMIN" and row["seller_id"] != user["id"]:
        fail(403, "ACCESS_DENIED", "Product belongs to another seller")


def create_product(request, body):
    user = require(request, "SELLER", "ADMIN")
    d = data(body)
    with transaction(write=True) as conn:
        return conn.execute(
            "INSERT INTO products(id,name,description,price,stock,category,status,seller_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                d["name"],
                d["description"],
                d["price"],
                d["stock"],
                d["category"],
                d["status"],
                user["id"],
            ),
        ).fetchone()


def get_product(request, id):
    with transaction() as conn:
        return product(conn, id)


def list_products(request, page=0, size=20, status=None, category=None):
    where, params = [], []
    for column, value in [("status", status), ("category", category)]:
        if value is not None:
            where.append(f"{column}=%s")
            params.append(value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with transaction() as conn:
        # Count and page come from the same database snapshot.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        total = conn.execute(
            "SELECT count(*) AS n FROM products" + clause, params
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM products"
            + clause
            + " ORDER BY created_at,id LIMIT %s OFFSET %s",
            params + [size, page * size],
        ).fetchall()
        return {"content": rows, "totalElements": total, "page": page, "size": size}


def update_product(request, id, body):
    user = require(request, "SELLER", "ADMIN")
    d = data(body)
    with transaction(write=True) as conn:
        product_owner(user, product(conn, id))
        return conn.execute(
            "UPDATE products SET name=%s,description=%s,price=%s,stock=%s,category=%s,status=%s WHERE id=%s RETURNING *",
            (
                d["name"],
                d["description"],
                d["price"],
                d["stock"],
                d["category"],
                d["status"],
                id,
            ),
        ).fetchone()


def delete_product(request, id):
    user = require(request, "SELLER", "ADMIN")
    with transaction(write=True) as conn:
        product_owner(user, product(conn, id))
        conn.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s", (id,))


def create_promo(request, body):
    require(request, "SELLER", "ADMIN")
    d = data(body)
    for field in ["valid_from", "valid_until"]:
        if d[field].tzinfo is None:
            invalid(field, "Timezone is required")
    if d["valid_until"] < d["valid_from"]:
        invalid("valid_until", "Must be at or after valid_from")
    with transaction(write=True) as conn:
        row = conn.execute(
            "INSERT INTO promo_codes(id,code,discount_type,discount_value,min_order_amount,max_uses,valid_from,valid_until,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING RETURNING *",
            (
                uuid4(),
                d["code"],
                d["discount_type"],
                d["discount_value"],
                d["min_order_amount"],
                d["max_uses"],
                d["valid_from"],
                d["valid_until"],
                d["active"],
            ),
        ).fetchone()
        if not row:
            fail(409, "PROMO_CODE_EXISTS", "Promo code already exists")
        return row


def order(conn, id, user):
    row = conn.execute("SELECT * FROM orders WHERE id=%s", (id,)).fetchone()
    if not row:
        fail(404, "ORDER_NOT_FOUND", "Order not found")
    if row["user_id"] != user["id"] and user["role"] != "ADMIN":
        fail(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    row["items"] = conn.execute(
        "SELECT product_id,quantity,price_at_order FROM order_items WHERE order_id=%s ORDER BY product_id",
        (id,),
    ).fetchall()
    return row


def get_order(request, id):
    user = require(request, "USER", "ADMIN")
    with transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return order(conn, id, user)


def check_state(row, *allowed):
    if row["status"] not in allowed:
        fail(
            409,
            "INVALID_STATE_TRANSITION",
            "Operation is not allowed in this state",
            {"status": row["status"], "allowed": list(allowed)},
        )


def rate_limit(conn, user_id, kind):
    minutes = Decimal(os.getenv("ORDER_RATE_LIMIT_MINUTES", "1"))
    if minutes < 0:
        raise RuntimeError("ORDER_RATE_LIMIT_MINUTES cannot be negative")
    last = conn.execute(
        "SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s ORDER BY created_at DESC LIMIT 1",
        (user_id, kind),
    ).fetchone()
    if (
        last
        and Decimal(
            str((datetime.now(timezone.utc) - last["created_at"]).total_seconds())
        )
        < minutes * 60
    ):
        fail(
            429,
            "ORDER_LIMIT_EXCEEDED",
            "Order operation rate limit exceeded",
            {"operation_type": kind, "limit_minutes": str(minutes)},
        )


def record_operation(conn, user_id, kind):
    conn.execute(
        "INSERT INTO user_operations(id,user_id,operation_type,created_at) VALUES (%s,%s,%s,clock_timestamp())",
        (uuid4(), user_id, kind),
    )


def reserve(conn, items, old_prices=None):
    old_prices = old_prices or {}
    # Duplicates are rejected by contract validation before reaching this function.
    rows = [(item, product(conn, item.product_id)) for item in items]
    for item, row in rows:
        if row["status"] != "ACTIVE":
            fail(
                409,
                "PRODUCT_INACTIVE",
                "Product is not active",
                {"product_id": str(item.product_id)},
            )
    insufficient = [
        {
            "product_id": str(i.product_id),
            "requested": i.quantity,
            "available": p["stock"],
        }
        for i, p in rows
        if p["stock"] < i.quantity
    ]
    if insufficient:
        fail(
            409, "INSUFFICIENT_STOCK", "Insufficient stock", {"products": insufficient}
        )
    result = []
    for item, row in rows:
        conn.execute(
            "UPDATE products SET stock=stock-%s WHERE id=%s",
            (item.quantity, item.product_id),
        )
        result.append(
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "price_at_order": old_prices.get(item.product_id, row["price"]),
            }
        )
    return result


def restore(conn, items):
    for item in items:
        # A seller may have replenished stock since reservation. Prevent integer overflow.
        row = product(conn, item["product_id"])
        if row["stock"] + item["quantity"] > 2147483647:
            invalid("stock", "Returning reserved stock would exceed the stock limit")
        conn.execute(
            "UPDATE products SET stock=stock+%s WHERE id=%s",
            (item["quantity"], item["product_id"]),
        )


def subtotal(items):
    total = sum((i["price_at_order"] * i["quantity"] for i in items), Decimal(0))
    if total > MAX_MONEY:
        invalid("items", "Order amount exceeds 9999999999.99")
    return total


def apply_promo(conn, total, code=None, promo_id=None):
    if code is None and promo_id is None:
        return None, Decimal(0)
    existing = promo_id is not None
    promo = conn.execute(
        "SELECT * FROM promo_codes WHERE " + ("id" if existing else "code") + "=%s",
        (promo_id if existing else code,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    # The existing order already owns one use; it must not consume another use on update.
    if (
        not promo
        or not promo["active"]
        or not promo["valid_from"] <= now <= promo["valid_until"]
        or promo["current_uses"] - int(existing) >= promo["max_uses"]
    ):
        fail(422, "PROMO_CODE_INVALID", "Promo code is inactive, expired or exhausted")
    if total < promo["min_order_amount"]:
        if existing:
            conn.execute(
                "UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s",
                (promo_id,),
            )
            return None, Decimal(0)
        fail(
            422,
            "PROMO_CODE_MIN_AMOUNT",
            "Order amount is below promo minimum",
            {"min_order_amount": str(promo["min_order_amount"])},
        )
    if promo["discount_type"] == "PERCENTAGE":
        # Assignment explicitly says full price if percentage discount exceeds 70%.
        discount = (
            total * promo["discount_value"] / 100
            if promo["discount_value"] <= 70
            else Decimal(0)
        )
    else:
        discount = min(promo["discount_value"], total)
    if not existing:
        conn.execute(
            "UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s",
            (promo["id"],),
        )
    return promo["id"], discount.quantize(CENT, rounding=ROUND_HALF_UP)


def save_items(conn, order_id, items):
    for item in items:
        conn.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,price_at_order) VALUES (%s,%s,%s,%s,%s)",
            (
                uuid4(),
                order_id,
                item["product_id"],
                item["quantity"],
                item["price_at_order"],
            ),
        )


def create_order(request, body):
    user = require(request, "USER", "ADMIN")
    with transaction(write=True) as conn:
        rate_limit(conn, user["id"], "CREATE_ORDER")
        if conn.execute(
            "SELECT 1 FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')",
            (user["id"],),
        ).fetchone():
            fail(409, "ORDER_HAS_ACTIVE", "User already has an active order")
        items = reserve(conn, body.items)
        total = subtotal(items)
        promo_id, discount = apply_promo(conn, total, code=body.promo_code)
        id = uuid4()
        conn.execute(
            "INSERT INTO orders(id,user_id,status,promo_code_id,total_amount,discount_amount) VALUES (%s,%s,'CREATED',%s,%s,%s)",
            (id, user["id"], promo_id, total - discount, discount),
        )
        save_items(conn, id, items)
        record_operation(conn, user["id"], "CREATE_ORDER")
        return order(conn, id, user)


def update_order(request, id, body):
    user = require(request, "USER", "ADMIN")
    with transaction(write=True) as conn:
        current = order(conn, id, user)
        check_state(current, "CREATED")
        rate_limit(conn, user["id"], "UPDATE_ORDER")
        restore(conn, current["items"])
        items = reserve(
            conn,
            body.items,
            {i["product_id"]: i["price_at_order"] for i in current["items"]},
        )
        total = subtotal(items)
        promo_id, discount = apply_promo(conn, total, promo_id=current["promo_code_id"])
        conn.execute("DELETE FROM order_items WHERE order_id=%s", (id,))
        save_items(conn, id, items)
        conn.execute(
            "UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s",
            (total - discount, discount, promo_id, id),
        )
        record_operation(conn, user["id"], "UPDATE_ORDER")
        return order(conn, id, user)


def cancel_order(request, id):
    user = require(request, "USER", "ADMIN")
    with transaction(write=True) as conn:
        current = order(conn, id, user)
        check_state(current, "CREATED", "PAYMENT_PENDING")
        restore(conn, current["items"])
        if current["promo_code_id"]:
            conn.execute(
                "UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s",
                (current["promo_code_id"],),
            )
        conn.execute("UPDATE orders SET status='CANCELED' WHERE id=%s", (id,))
        return order(conn, id, user)


def transition_order(request, id, body):
    user = require(request, "ADMIN")
    transitions = {
        "CREATED": "PAYMENT_PENDING",
        "PAYMENT_PENDING": "PAID",
        "PAID": "SHIPPED",
        "SHIPPED": "COMPLETED",
    }
    with transaction(write=True) as conn:
        current = order(conn, id, user)
        if transitions.get(current["status"]) != body.status.value:
            fail(409, "INVALID_STATE_TRANSITION", "Invalid order state transition")
        conn.execute("UPDATE orders SET status=%s WHERE id=%s", (body.status.value, id))
        return order(conn, id, user)
