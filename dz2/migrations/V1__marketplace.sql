CREATE TABLE users (
 id uuid PRIMARY KEY, username varchar(100) UNIQUE NOT NULL,
 password_hash text NOT NULL, role varchar(10) NOT NULL CHECK (role IN ('USER','SELLER','ADMIN'))
);
CREATE TABLE products (
 id uuid PRIMARY KEY, name varchar(255) NOT NULL, description varchar(4000),
 price numeric(12,2) NOT NULL CHECK (price > 0), stock integer NOT NULL CHECK (stock >= 0),
 category varchar(100) NOT NULL, status varchar(10) NOT NULL CHECK (status IN ('ACTIVE','INACTIVE','ARCHIVED')),
 seller_id uuid NOT NULL REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX products_status_idx ON products(status);
CREATE TABLE promo_codes (
 id uuid PRIMARY KEY, code varchar(20) UNIQUE NOT NULL,
 discount_type varchar(20) NOT NULL CHECK (discount_type IN ('PERCENTAGE','FIXED_AMOUNT')),
 discount_value numeric(12,2) NOT NULL CHECK (discount_value > 0),
 min_order_amount numeric(12,2) NOT NULL CHECK (min_order_amount >= 0),
 max_uses integer NOT NULL CHECK (max_uses > 0),
 current_uses integer NOT NULL DEFAULT 0 CHECK (current_uses >= 0 AND current_uses <= max_uses),
 valid_from timestamptz NOT NULL, valid_until timestamptz NOT NULL,
 active boolean NOT NULL DEFAULT true, CHECK(valid_until >= valid_from)
);
CREATE TABLE orders (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 status varchar(20) NOT NULL CHECK (status IN ('CREATED','PAYMENT_PENDING','PAID','SHIPPED','COMPLETED','CANCELED')),
 promo_code_id uuid REFERENCES promo_codes(id),
 total_amount numeric(12,2) NOT NULL CHECK (total_amount >= 0),
 discount_amount numeric(12,2) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX orders_one_active_per_user ON orders(user_id) WHERE status IN ('CREATED','PAYMENT_PENDING');
CREATE TABLE order_items (
 id uuid PRIMARY KEY, order_id uuid NOT NULL REFERENCES orders(id), product_id uuid NOT NULL REFERENCES products(id),
 quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 999),
 price_at_order numeric(12,2) NOT NULL CHECK (price_at_order > 0), UNIQUE(order_id, product_id)
);
CREATE TABLE user_operations (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 operation_type varchar(20) NOT NULL CHECK (operation_type IN ('CREATE_ORDER','UPDATE_ORDER')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_operations_latest_idx ON user_operations(user_id, operation_type, created_at DESC);
CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = clock_timestamp(); RETURN NEW; END $$;
CREATE TRIGGER products_updated_at BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER orders_updated_at BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
