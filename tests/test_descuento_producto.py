# -*- coding: utf-8 -*-
"""Descuento individual de un producto desde el panel del administrador (PUT /admin/catalogo/productos/<id>)."""
import os
import sqlite3
import sys
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402
from cargar_datos import CSV_POR_DEFECTO, cargar  # noqa: E402

ADMIN = {"nombre": "Admin Desc", "email": "admin_desc@example.com", "password": "clave-admin-123",
         "codigo": "clave-admin", "autorizacion_datos": True}


@pytest.fixture()
def d(tmp_path):
    ruta = str(tmp_path / "desc.db")
    cargar(CSV_POR_DEFECTO, ruta)
    app = create_app({"DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "clave-admin", "TESTING": True})
    conn = sqlite3.connect(ruta)
    pid, nom = conn.execute("SELECT product_id, product_name FROM productos WHERE activo = 1 LIMIT 1").fetchone()
    c = app.test_client()
    assert c.post("/admin/registro", json=ADMIN).status_code == 201
    return {"c": c, "conn": conn, "url": f"/admin/catalogo/productos/{quote(pid)}?nombre_actual={quote(nom)}", "pid": pid}


def descuento_en_base(d):
    return d["conn"].execute("SELECT descuento FROM productos WHERE product_id = ?", (d["pid"],)).fetchone()[0]


def test_aplicar_descuento_individual_no_da_error_interno(d):
    r = d["c"].put(d["url"], json={"descuento": 15})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["descuento"] == 15 and r.get_json()["mensaje"] == "Producto actualizado"
    assert descuento_en_base(d) == 15


def test_el_cambio_de_descuento_queda_en_la_auditoria(d):
    d["c"].put(d["url"], json={"descuento": 15})
    d["c"].put(d["url"], json={"descuento": 20})
    detalles = [f[0] for f in d["conn"].execute("SELECT detalle FROM auditoria WHERE detalle LIKE 'Producto editado%'")]
    assert any("descuento: 0.0% → 15.0%" in x for x in detalles)
    assert any("descuento: 15.0% → 20.0%" in x for x in detalles)


def test_mismo_descuento_no_cuenta_como_cambio(d):
    d["c"].put(d["url"], json={"descuento": 15})
    r = d["c"].put(d["url"], json={"descuento": 15})
    assert r.status_code == 200 and r.get_json()["mensaje"] == "No había cambios que guardar"


def test_quitar_el_descuento_con_cero_o_vacio(d):
    d["c"].put(d["url"], json={"descuento": 30})
    assert d["c"].put(d["url"], json={"descuento": 0}).status_code == 200 and descuento_en_base(d) == 0
    d["c"].put(d["url"], json={"descuento": 30})
    assert d["c"].put(d["url"], json={"descuento": ""}).status_code == 200 and descuento_en_base(d) == 0


@pytest.mark.parametrize("malo", [150, -5, "abc", [10]])
def test_descuento_invalido_da_422_y_no_cambia(d, malo):
    d["c"].put(d["url"], json={"descuento": 10})
    r = d["c"].put(d["url"], json={"descuento": malo})
    assert r.status_code == 422 and "descuento" in str(r.get_json())
    assert descuento_en_base(d) == 10


def test_editar_otro_campo_no_toca_el_descuento(d):
    d["c"].put(d["url"], json={"descuento": 25})
    r = d["c"].put(d["url"], json={"stock": 77})
    assert r.status_code == 200 and descuento_en_base(d) == 25


def test_patch_tambien_aplica_descuento(d):
    assert d["c"].patch(d["url"], json={"descuento": 12.5}).status_code == 200
    assert descuento_en_base(d) == 12.5
