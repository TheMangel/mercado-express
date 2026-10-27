# -*- coding: utf-8 -*-
"""Esquema de la base de datos de Mercado Express.

Es el mismo modelo de 4 tablas del Momento Evaluativo 1, con agregados
para la app:
  - clientes.autorizacion_datos / fecha_autorizacion  (Ley 1581 de 2012)
  - tabla auditoria (queda registro de cada acceso a datos personales completos)
  - un índice extra en pedidos(customer_id)
  - cuentas (con rol cliente/admin) y sesiones
  - categorias / subcategorias y productos.precio / productos.activo (catálogo administrable)
"""

# Datos de contacto y envío de la cuenta. Son opcionales en la base (las cuentas anteriores no los tienen);
# la app los exige al registrarse. Se usan también en migrar() para bases que ya existían.
COLUMNAS_PERFIL = {
    "apellido":      "TEXT",
    "telefono":      "TEXT CHECK (telefono IS NULL OR length(telefono) BETWEEN 7 AND 25)",
    "pais":          "TEXT",
    "region":        "TEXT",
    "ciudad":        "TEXT",
    "direccion":     "TEXT",
    "codigo_postal": "TEXT CHECK (codigo_postal IS NULL OR length(codigo_postal) BETWEEN 3 AND 10)",
}
_PERFIL_SQL = ",\n    ".join(f"{col:<18} {definicion}" for col, definicion in COLUMNAS_PERFIL.items()) + ","

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    customer_id        TEXT PRIMARY KEY,
    customer_name      TEXT NOT NULL,
    segment            TEXT,
    country            TEXT,
    city               TEXT,
    state              TEXT,
    postal_code        TEXT,
    region             TEXT,
    autorizacion_datos INTEGER,          -- 1 = el cliente autorizó el tratamiento de sus datos
    fecha_autorizacion TEXT              -- NULL en los clientes históricos del dataset
);

CREATE TABLE IF NOT EXISTS productos (
    product_id   TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category     TEXT,
    sub_category TEXT,
    precio       REAL CHECK (precio IS NULL OR precio > 0),  -- NULL = precio deducido del historial de ventas
    activo       INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1)),  -- 0 = oculto en la tienda (no se borra si tiene ventas)
    stock        INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),       -- unidades disponibles (nunca negativo)
    PRIMARY KEY (product_id, product_name)   -- llave compuesta: un mismo ID se usó para 2 productos
);

-- Categorías del catálogo (se pueden crear desde /admin/catalogo, aunque aún no tengan productos)
CREATE TABLE IF NOT EXISTS categorias (
    nombre TEXT PRIMARY KEY,
    tono   TEXT NOT NULL DEFAULT 'ofi' CHECK (tono IN ('mue','tec','ofi','nat','mar')),  -- color de la ilustración
    creada TEXT
);

CREATE TABLE IF NOT EXISTS subcategorias (
    categoria TEXT NOT NULL,
    nombre    TEXT NOT NULL,
    icono     TEXT NOT NULL DEFAULT 'box'
              CHECK (icono IN ('chair','shelf','table','lamp','phone','mouse','printer','binder','box')),
    PRIMARY KEY (categoria, nombre),
    FOREIGN KEY (categoria) REFERENCES categorias(nombre)
);

CREATE TABLE IF NOT EXISTS pedidos (
    order_id    TEXT PRIMARY KEY,
    order_date  TEXT NOT NULL,
    ship_date   TEXT,
    ship_mode   TEXT,
    customer_id TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES clientes(customer_id)
);

CREATE TABLE IF NOT EXISTS detalle_pedido (
    detalle_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    product_name TEXT NOT NULL,
    sales        REAL NOT NULL CHECK (sales > 0),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    discount     REAL CHECK (discount >= 0 AND discount <= 1),
    profit       REAL,
    FOREIGN KEY (order_id) REFERENCES pedidos(order_id),
    FOREIGN KEY (product_id, product_name) REFERENCES productos(product_id, product_name)
);

CREATE TABLE IF NOT EXISTS auditoria (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha    TEXT NOT NULL,
    nivel    TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    detalle  TEXT
);

-- Cuentas de cliente de la tienda (Ley 1581: solo se guarda lo necesario + la autorización)
CREATE TABLE IF NOT EXISTS cuentas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE,
    nombre             TEXT NOT NULL,
    password_hash      TEXT NOT NULL,          -- nunca la contraseña en texto
    rol                TEXT NOT NULL DEFAULT 'cliente' CHECK (rol IN ('cliente','admin')),
    -- PERFIL (apellido, teléfono y dirección de envío; se agregan desde COLUMNAS_PERFIL)
    autorizacion_datos INTEGER NOT NULL CHECK (autorizacion_datos = 1),
    fecha_autorizacion TEXT NOT NULL,
    creada             TEXT NOT NULL
);

-- Sesiones abiertas: se guarda el hash del token, no el token
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash TEXT PRIMARY KEY,
    cuenta_id  INTEGER NOT NULL,
    expira     TEXT NOT NULL,
    FOREIGN KEY (cuenta_id) REFERENCES cuentas(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_detalle_order_id ON detalle_pedido(order_id);
CREATE INDEX IF NOT EXISTS idx_pedidos_customer_id ON pedidos(customer_id);
""".replace("-- PERFIL (apellido, teléfono y dirección de envío; se agregan desde COLUMNAS_PERFIL)", _PERFIL_SQL)
