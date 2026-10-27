# -*- coding: utf-8 -*-
"""Carga datos_originales.csv en SQLite con el mismo proceso del E1.

Uso:
    python cargar_datos.py                 # crea mercado_express.db
    python cargar_datos.py --rehacer       # borra la base y la crea de nuevo

El CSV original nunca se modifica: todo se hace leyéndolo y escribiendo en la base.
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime

from esquema import SCHEMA

AQUI = os.path.dirname(os.path.abspath(__file__))
CSV_POR_DEFECTO = os.path.join(AQUI, "data", "datos_originales.csv")
DB_POR_DEFECTO = os.path.join(AQUI, "mercado_express.db")


def _fecha_iso(texto):
    """'11/8/2016' (mes/día/año) -> '2016-11-08' (así SQLite puede calcular con fechas)."""
    return datetime.strptime(texto, "%m/%d/%Y").strftime("%Y-%m-%d")


def cargar(csv_path=CSV_POR_DEFECTO, db_path=DB_POR_DEFECTO):
    clientes, productos, pedidos, detalle = {}, {}, {}, []

    with open(csv_path, newline="", encoding="latin-1") as f:
        for r in csv.DictReader(f):
            # Igual que en el E1: se conserva la primera aparición de cada llave.
            clientes.setdefault(r["Customer ID"], (
                r["Customer ID"], r["Customer Name"], r["Segment"], r["Country"],
                r["City"], r["State"], str(r["Postal Code"]), r["Region"]))
            productos.setdefault((r["Product ID"], r["Product Name"]), (
                r["Product ID"], r["Product Name"], r["Category"], r["Sub-Category"]))
            pedidos.setdefault(r["Order ID"], (
                r["Order ID"], _fecha_iso(r["Order Date"]), _fecha_iso(r["Ship Date"]),
                r["Ship Mode"], r["Customer ID"]))
            detalle.append((
                r["Order ID"], r["Product ID"], r["Product Name"],
                float(r["Sales"]), int(r["Quantity"]), float(r["Discount"]), float(r["Profit"])))

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    with conn:  # una sola transacción: o se carga todo o no se carga nada
        conn.executemany(
            "INSERT INTO clientes (customer_id, customer_name, segment, country, city, state, "
            "postal_code, region) VALUES (?,?,?,?,?,?,?,?)", clientes.values())
        conn.executemany(
            "INSERT INTO productos (product_id, product_name, category, sub_category) "
            "VALUES (?,?,?,?)", productos.values())
        conn.executemany(
            "INSERT INTO pedidos (order_id, order_date, ship_date, ship_mode, customer_id) "
            "VALUES (?,?,?,?,?)", pedidos.values())
        conn.executemany(
            "INSERT INTO detalle_pedido (order_id, product_id, product_name, sales, quantity, "
            "discount, profit) VALUES (?,?,?,?,?,?,?)", detalle)
    conteos = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("clientes", "productos", "pedidos", "detalle_pedido")}
    conn.close()
    return conteos


if __name__ == "__main__":
    if os.path.exists(DB_POR_DEFECTO):
        if "--rehacer" in sys.argv:
            os.remove(DB_POR_DEFECTO)
        else:
            sys.exit("Ya existe mercado_express.db. Usa --rehacer si quieres crearla de nuevo.")
    for tabla, n in cargar().items():
        print(f"{tabla}: {n} filas")
