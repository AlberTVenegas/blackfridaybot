-- =============================================================================
-- Black Friday Chile Price Hunter Bot - PostgreSQL Schema (Supabase Ready)
-- Ejecutar directamente en el SQL Editor de Supabase.
-- =============================================================================

-- 1. Tiendas objetivo (Chile)
CREATE TABLE IF NOT EXISTS retailers (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    base_url    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Catálogo de productos monitoreados
CREATE TABLE IF NOT EXISTS products (
    id          BIGSERIAL PRIMARY KEY,
    retailer_id BIGINT NOT NULL REFERENCES retailers(id) ON DELETE CASCADE,
    sku         TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT,
    url         TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_products_retailer_sku UNIQUE (retailer_id, sku)
);

-- 3. Historial de precios (CLP normalizados a entero)
CREATE TABLE IF NOT EXISTS price_history (
    id                   BIGSERIAL PRIMARY KEY,
    product_id           BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price                INTEGER NOT NULL CHECK (price >= 0),
    previous_price       INTEGER,
    normal_price         INTEGER CHECK (normal_price IS NULL OR normal_price >= 0),
    payment_method       TEXT NOT NULL DEFAULT 'ALL_PAYMENT_METHODS'
                         CHECK (payment_method IN ('ALL_PAYMENT_METHODS', 'STORE_CARD')),
    payment_method_label TEXT,
    in_stock             BOOLEAN NOT NULL DEFAULT TRUE,
    scraped_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Alertas enviadas (control de duplicados, cooldown por producto y rate limit global Twilio)
CREATE TABLE IF NOT EXISTS alerts_sent (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alert_type      TEXT NOT NULL CHECK (alert_type IN ('HISTORICAL_LOW', 'PRICE_ERROR')),
    price_triggered INTEGER NOT NULL,
    twilio_called   BOOLEAN NOT NULL DEFAULT FALSE,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- Índices optimizados para consultas de historial (30 días) y alertas
-- =============================================================================

-- Productos activos por tienda
CREATE INDEX IF NOT EXISTS idx_products_retailer_active
    ON products (retailer_id, is_active)
    WHERE is_active = TRUE;

-- Covering index para consultar últimos 30 días de un producto sin ir al heap
CREATE INDEX IF NOT EXISTS idx_price_history_product_scraped_desc
    ON price_history (product_id, scraped_at DESC)
    INCLUDE (price, in_stock, normal_price, payment_method);

-- Limpieza / retención por fecha
CREATE INDEX IF NOT EXISTS idx_price_history_scraped_at
    ON price_history (scraped_at DESC);

-- Cooldown O(log N) por producto, tipo y precio
CREATE INDEX IF NOT EXISTS idx_alerts_sent_cooldown
    ON alerts_sent (product_id, alert_type, price_triggered, sent_at DESC);

-- Consulta instantánea de la última llamada telefónica Twilio (rate limit global)
CREATE INDEX IF NOT EXISTS idx_alerts_sent_twilio_last_call
    ON alerts_sent (sent_at DESC)
    WHERE twilio_called = TRUE;

-- =============================================================================
-- Seed inicial de las 8 tiendas chilenas objetivo
-- =============================================================================
INSERT INTO retailers (name, base_url) VALUES
    ('Falabella', 'https://www.falabella.com/falabella-cl'),
    ('Paris',     'https://www.paris.cl'),
    ('Ripley',    'https://simple.ripley.cl'),
    ('Samsung',   'https://www.samsung.com/cl'),
    ('Xiaomi',    'https://www.mi.com/cl'),
    ('Nike',      'https://www.nike.cl'),
    ('Adidas',    'https://www.adidas.cl'),
    ('Decathlon', 'https://www.decathlon.cl')
ON CONFLICT (name) DO NOTHING;
