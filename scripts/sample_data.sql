-- 練習用のデータ（架空の通販の、商品・顧客・注文）。
-- scripts/setup_data_db.sh --sample が、データベース coding_agent_sample に読み込む。
-- 乱数は使わず、日付から決まる値にしてある（何度作っても同じ中身になる）。名前とメールアドレスは架空のもの。

CREATE TABLE products (
    product_id serial PRIMARY KEY,
    name text NOT NULL,
    category text NOT NULL,
    unit_price integer NOT NULL
);
COMMENT ON TABLE products IS '商品';
COMMENT ON COLUMN products.category IS '分類（飲料、食品、雑貨）';
COMMENT ON COLUMN products.unit_price IS '単価（円）';

CREATE TABLE customers (
    customer_id serial PRIMARY KEY,
    name text NOT NULL,
    region text NOT NULL,
    email text NOT NULL,
    registered_on date NOT NULL
);
COMMENT ON TABLE customers IS '顧客';
COMMENT ON COLUMN customers.region IS '地域（東京、大阪、名古屋、福岡、札幌）';
COMMENT ON COLUMN customers.registered_on IS '登録日';

CREATE TABLE orders (
    order_id serial PRIMARY KEY,
    order_date date NOT NULL,
    customer_id integer NOT NULL REFERENCES customers,
    product_id integer NOT NULL REFERENCES products,
    quantity integer NOT NULL,
    amount integer NOT NULL
);
COMMENT ON TABLE orders IS '注文（1行が、1つの商品の注文）';
COMMENT ON COLUMN orders.quantity IS '数量';
COMMENT ON COLUMN orders.amount IS '金額（円）。数量 × 単価';

INSERT INTO products (name, category, unit_price) VALUES
    ('緑茶 500ml', '飲料', 150),
    ('コーヒー豆 200g', '飲料', 1200),
    ('炭酸水 1L', '飲料', 180),
    ('米 5kg', '食品', 2800),
    ('カレー（レトルト）', '食品', 320),
    ('チョコレート', '食品', 450),
    ('ノート 5冊組', '雑貨', 600),
    ('ボールペン 10本', '雑貨', 800);

INSERT INTO customers (name, region, email, registered_on)
SELECT
    (ARRAY['山田', '佐藤', '鈴木', '高橋', '田中'])[1 + (n - 1) % 5] || ' '
        || (ARRAY['太郎', '花子', '一郎', '美咲'])[1 + (n - 1) % 4],
    (ARRAY['東京', '大阪', '名古屋', '福岡', '札幌'])[1 + (n * 3) % 5],
    'customer' || n || '@example.com',
    DATE '2024-01-10' + (n * 17)
FROM generate_series(1, 20) AS n;

-- 2025-01-01 から 2026-10-31 まで、1日に3件ずつ
INSERT INTO orders (order_date, customer_id, product_id, quantity, amount)
SELECT d::date, 1 + (doy * 3 + n * 7) % 20, p.product_id, q, q * p.unit_price
FROM generate_series(DATE '2025-01-01', DATE '2026-10-31', INTERVAL '1 day') AS d
CROSS JOIN generate_series(1, 3) AS n
CROSS JOIN LATERAL (
    SELECT extract(doy FROM d)::int AS doy, 1 + (extract(day FROM d)::int + n * 2) % 5 AS q
) AS v
JOIN products p ON p.product_id = 1 + (v.doy * n + n * 5) % 8;

ANALYZE;
