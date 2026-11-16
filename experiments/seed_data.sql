BEGIN;

CREATE SCHEMA IF NOT EXISTS benchmark;

CREATE TABLE benchmark.customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    region TEXT NOT NULL
);

CREATE TABLE benchmark.orders (
    order_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id INTEGER NOT NULL
        REFERENCES benchmark.customers(customer_id),
    order_date DATE NOT NULL,
    status TEXT NOT NULL,
    total_amount NUMERIC(10, 2) NOT NULL
);

SELECT setseed(0.42);

INSERT INTO benchmark.customers
    (customer_id, customer_name, region)
SELECT
    id,
    'Customer ' || id,
    (ARRAY['North', 'South', 'East', 'West'])[random(1, 4)]
FROM generate_series(1, 10000) AS g(id);

INSERT INTO benchmark.orders
    (customer_id, order_date, status, total_amount)
SELECT
    random(1, 10000),
    DATE '2025-01-01' + random(0, 729),
    (ARRAY['pending', 'shipped', 'delivered', 'cancelled'])[random(1, 4)],
    round((10 + random() * 990)::numeric, 2)
FROM generate_series(1, 100000);

ANALYZE benchmark.customers;
ANALYZE benchmark.orders;

COMMIT;

SELECT 'customers' AS table_name, count(*) AS row_count
FROM benchmark.customers
UNION ALL
SELECT 'orders', count(*)
FROM benchmark.orders;
