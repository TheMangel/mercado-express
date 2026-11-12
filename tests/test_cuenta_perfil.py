# -*- coding: utf-8 -*-
"""Pruebas del endpoint PUT /cuenta/perfil para edición de datos personales."""
import os
import sqlite3
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402
from cargar_datos import CSV_POR_DEFECTO, cargar  # noqa: E402

DESTINO = {"pais": "Colombia", "region": "Córdoba", "ciudad": "Montería"}
CUENTA = {
    "nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com", "password": "claveSegura1",
    "telefono": "3001234567", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001",
    "autorizacion_datos": True, **DESTINO
}
TARJETA = {"numero": "4242 4242 4242 4242", "nombre": "Ana Pérez", "vence": "12/40", "cvv": "123"}


@pytest.fixture()
def e(tmp_path):
    ruta = str(tmp_path / "perfil.db")
    cargar(CSV_POR_DEFECTO, ruta)
    app = create_app({"DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "a", "TESTING": True})
    conn = sqlite3.connect(ruta)
    prods = conn.execute("SELECT product_id, product_name FROM productos WHERE activo = 1 LIMIT 2").fetchall()
    conn.execute("UPDATE productos SET stock = 50")
    conn.commit()
    c = app.test_client()
    assert c.post("/cuenta/registro", json=CUENTA).status_code == 201
    return {"app": app, "c": c, "conn": conn, "prods": prods, "ruta": ruta}


def comprar(e):
    opciones = e["c"].get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000").get_json()
    base = {"opcion_envio": opciones["opciones"][-1]["id"], "destino": DESTINO,
            "items": [{"product_id": p, "product_name": n, "quantity": 1} for (p, n) in e["prods"]]}
    pago = e["c"].post("/cuenta/pago", json={**base, "tarjeta": TARJETA}).get_json()
    r = e["c"].post("/cuenta/pedido", json={**base, "pago_id": pago["pago_id"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["order_id"]


def test_cuenta_perfil_sin_sesion_da_401(e):
    c_anonimo = e["app"].test_client()
    r = c_anonimo.put("/cuenta/perfil", json={"nombre": "Nuevo"})
    assert r.status_code == 401
    assert "Inicia sesión" in r.get_json()["error"]


def test_cuenta_perfil_actualizado_exitoso(e):
    nuevo = {
        "nombre": "Ana María",
        "apellido": "Gómez",
        "email": "ana@example.com",
        "telefono": "3119876543",
        "pais": "Colombia",
        "region": "Córdoba",
        "ciudad": "Montería",
        "direccion": "Carrera 5 # 10-20",
        "codigo_postal": "230002"
    }
    r = e["c"].put("/cuenta/perfil", json=nuevo)
    assert r.status_code == 200, r.get_json()
    datos = r.get_json()
    assert datos["nombre"] == "Ana María"
    assert datos["apellido"] == "Gómez"
    assert datos["email"] == "ana@example.com"
    assert datos["telefono"] == "3119876543"
    assert datos["direccion"]["direccion"] == "Carrera 5 # 10-20"
    assert datos["direccion"]["codigo_postal"] == "230002"

    # Verificar que /cuenta/yo devuelve los mismos datos
    yo = e["c"].get("/cuenta/yo").get_json()
    assert yo["cuenta"]["nombre"] == "Ana María"
    assert yo["cuenta"]["apellido"] == "Gómez"

    # Verificar auditoría: sin contraseñas ni datos completos en el detalle
    conn = sqlite3.connect(e["ruta"])
    fila_aud = conn.execute("SELECT endpoint, detalle FROM auditoria WHERE endpoint = '/cuenta/perfil'").fetchone()
    assert fila_aud is not None
    assert "Perfil actualizado cuenta_id=" in fila_aud[1]
    assert "Carrera 5" not in fila_aud[1]
    assert "claveSegura1" not in fila_aud[1]


def test_cuenta_perfil_cambio_correo_exige_clave(e):
    nuevo = {
        "nombre": "Ana",
        "apellido": "Pérez",
        "email": "ana_nueva@example.com",
        "telefono": "3001234567",
        "pais": "Colombia",
        "region": "Córdoba",
        "ciudad": "Montería",
        "direccion": "Calle 1 # 2-3",
        "codigo_postal": "230001"
    }
    # Sin contraseña actual
    r = e["c"].put("/cuenta/perfil", json=nuevo)
    assert r.status_code == 422
    assert any("password_actual" in d for d in r.get_json().get("detalles", []))


def test_cuenta_perfil_clave_incorrecta_da_401(e):
    nuevo = {
        "nombre": "Ana",
        "apellido": "Pérez",
        "email": "ana_nueva@example.com",
        "password_actual": "claveErronea123",
        "telefono": "3001234567",
        "pais": "Colombia",
        "region": "Córdoba",
        "ciudad": "Montería",
        "direccion": "Calle 1 # 2-3",
        "codigo_postal": "230001"
    }
    r = e["c"].put("/cuenta/perfil", json=nuevo)
    assert r.status_code == 401
    assert "contraseña actual es incorrecta" in r.get_json()["error"]


def test_cuenta_perfil_correo_repetido_da_409(e):
    # Crear segunda cuenta
    c2 = e["app"].test_client()
    cuenta2 = {
        "nombre": "Carlos", "apellido": "Ruiz", "email": "carlos@example.com", "password": "claveSegura2",
        "telefono": "3009998877", "direccion": "Calle 50 # 10-20", "codigo_postal": "230001",
        "autorizacion_datos": True, **DESTINO
    }
    assert c2.post("/cuenta/registro", json=cuenta2).status_code == 201

    # Ana intenta cambiar su correo al de Carlos con su clave correcta
    r = e["c"].put("/cuenta/perfil", json={
        "nombre": "Ana", "apellido": "Pérez", "email": "carlos@example.com",
        "password_actual": "claveSegura1",
        "telefono": "3001234567", "pais": "Colombia", "region": "Córdoba",
        "ciudad": "Montería", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001"
    })
    assert r.status_code == 409
    assert "Ya existe una cuenta con ese correo" in r.get_json()["error"]


def test_cuenta_perfil_otra_cuenta_no_puede_editar(e):
    # Registrar Carlos
    c2 = e["app"].test_client()
    cuenta2 = {
        "nombre": "Carlos", "apellido": "Ruiz", "email": "carlos@example.com", "password": "claveSegura2",
        "telefono": "3009998877", "direccion": "Calle 50 # 10-20", "codigo_postal": "230001",
        "autorizacion_datos": True, **DESTINO
    }
    r2 = c2.post("/cuenta/registro", json=cuenta2)
    assert r2.status_code == 201
    carlos_id = sqlite3.connect(e["ruta"]).execute("SELECT id FROM cuentas WHERE email = 'carlos@example.com'").fetchone()[0]

    # Ana intenta enviar id de Carlos en el payload
    r = e["c"].put("/cuenta/perfil", json={
        "id": carlos_id, "cuenta_id": carlos_id,
        "nombre": "Hack", "apellido": "Intento", "email": "ana@example.com",
        "telefono": "3001234567", "pais": "Colombia", "region": "Córdoba",
        "ciudad": "Montería", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001"
    })
    assert r.status_code == 200

    # Verificar en BD que Carlos NO cambió
    conn = sqlite3.connect(e["ruta"])
    carlos_db = conn.execute("SELECT nombre, apellido FROM cuentas WHERE id = ?", (carlos_id,)).fetchone()
    assert carlos_db[0] == "Carlos" and carlos_db[1] == "Ruiz"


def test_cuenta_perfil_rol_no_cliente_da_403(e):
    # Registrar cuenta admin
    c_admin = e["app"].test_client()
    adm = {"nombre": "Admin Boss", "email": "admin@example.com", "password": "claveAdmin123", "codigo": "a", "autorizacion_datos": True}
    assert c_admin.post("/admin/registro", json=adm).status_code == 201

    r = c_admin.put("/cuenta/perfil", json={
        "nombre": "Admin Modificado", "apellido": "Boss", "email": "admin@example.com",
        "telefono": "3001234567", "pais": "Colombia", "region": "Córdoba",
        "ciudad": "Montería", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001"
    })
    assert r.status_code == 403
    assert "Solo los clientes pueden editar su perfil" in r.get_json()["error"]


def test_cuenta_perfil_cambio_password_exitoso(e):
    # Cambiar contraseña
    r = e["c"].put("/cuenta/perfil", json={
        "nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com",
        "password_actual": "claveSegura1",
        "password_nueva": "nuevaClave88",
        "telefono": "3001234567", "pais": "Colombia", "region": "Córdoba",
        "ciudad": "Montería", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001"
    })
    assert r.status_code == 200

    # Cerrar sesión
    assert e["c"].post("/cuenta/salir").status_code == 200

    # Iniciar sesión con clave vieja falla
    r_vieja = e["c"].post("/cuenta/entrar", json={"email": "ana@example.com", "password": "claveSegura1"})
    assert r_vieja.status_code == 401

    # Iniciar sesión con clave nueva funciona
    r_nueva = e["c"].post("/cuenta/entrar", json={"email": "ana@example.com", "password": "nuevaClave88"})
    assert r_nueva.status_code == 200


def test_cuenta_perfil_no_altera_pedidos_previos(e):
    oid = comprar(e)
    conn = sqlite3.connect(e["ruta"])
    dest_antes = conn.execute("SELECT dest_pais, dest_region, dest_ciudad, dest_direccion, dest_telefono FROM pedidos WHERE order_id = ?", (oid,)).fetchone()
    assert dest_antes[0] == "Colombia"
    assert dest_antes[2] == "Montería"

    # Ana cambia completamente su perfil a otra dirección
    r = e["c"].put("/cuenta/perfil", json={
        "nombre": "Ana Actualizada", "apellido": "Pérez", "email": "ana@example.com",
        "telefono": "3209999999", "pais": "Colombia", "region": "Córdoba",
        "ciudad": "Cereté", "direccion": "Avenida 1 # 50-60", "codigo_postal": "230501"
    })
    assert r.status_code == 200

    # El pedido previo debe seguir intacto
    dest_despues = conn.execute("SELECT dest_pais, dest_region, dest_ciudad, dest_direccion, dest_telefono FROM pedidos WHERE order_id = ?", (oid,)).fetchone()
    assert dest_despues == dest_antes
    assert dest_despues[2] == "Montería"
    assert dest_despues[3] == "Calle 1 # 2-3"

