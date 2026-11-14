# -*- coding: utf-8 -*-
"""Pruebas para Tarea 1: Motivo del turno (preguntas frecuentes + consulta personalizada)."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import MOTIVOS_TURNO, create_app  # noqa: E402

CUENTA_1 = {
    "nombre": "Carlos", "apellido": "Gómez", "email": "carlos@example.com", "password": "claveSegura1",
    "telefono": "+57 300 111 2233", "pais": "Colombia", "region": "Córdoba", "ciudad": "Montería",
    "direccion": "Calle 10 # 20-30", "codigo_postal": "230001", "autorizacion_datos": True
}
CUENTA_2 = {
    "nombre": "Diana", "apellido": "López", "email": "diana@example.com", "password": "claveSegura2",
    "telefono": "+57 300 444 5566", "pais": "Colombia", "region": "Córdoba", "ciudad": "Montería",
    "direccion": "Carrera 15 # 40-50", "codigo_postal": "230001", "autorizacion_datos": True
}
ADMIN = {
    "nombre": "Admin Turnos", "email": "admin_tur@example.com", "password": "clave-admin-123",
    "codigo": "clave-admin", "autorizacion_datos": True
}
TARJETA = {"numero": "4242 4242 4242 4242", "nombre": "Carlos Gómez", "vence": "12/40", "cvv": "123"}


@pytest.fixture()
def app_tur(tmp_path):
    ruta = str(tmp_path / "tur_motivo.db")
    app = create_app({
        "DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "clave-admin",
        "TESTING": True, "TURNOS_MAX_POR_IP": 100, "TURNOS_MAX_FILA": 20
    })
    return app


@pytest.fixture()
def c(app_tur):
    return app_tur.test_client()


def test_endpoint_motivos(c):
    """GET /turnos/api/motivos es público y devuelve la lista de motivos permitidos por servicio."""
    r = c.get("/turnos/api/motivos")
    assert r.status_code == 200
    datos = r.get_json()
    assert isinstance(datos, dict)
    assert "Estado de mi pedido" in datos["Recogida de Pedidos web"]
    assert "Otra consulta" in datos["Recogida de Pedidos web"]
    assert datos == MOTIVOS_TURNO


def test_motivo_valido_y_con_detalle_opcional(app_tur, c):
    """Pedir turno con motivo válido funciona y guarda motivo y detalle opcional."""
    r = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Estado de mi pedido",
        "detalle": "Quiero saber cuándo llega mi paquete"
    })
    assert r.status_code == 200
    num = r.get_json()["numero"]
    assert num == 1

    conn = sqlite3.connect(app_tur.config["DB_PATH"])
    fila = conn.execute("SELECT motivo, detalle, pedido_id FROM turnos WHERE numero = ?", (num,)).fetchone()
    conn.close()
    assert fila[0] == "Estado de mi pedido"
    assert fila[1] == "Quiero saber cuándo llega mi paquete"
    assert fila[2] is None


def test_motivo_inventado_422(c):
    """Motivo fuera de la lista permitida responde 400."""
    r = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Motivo que no existe"
    })
    assert r.status_code == 400
    assert "motivo" in str(r.get_json()).lower()


def test_motivo_invalido_para_servicio(c):
    """Motivo que no pertenece al servicio elegido responde 400."""
    r = c.post("/turnos/api/pedir", json={
        "servicio_id": 2,  # Garantías y Devoluciones
        "motivo": "Cambiar o cancelar un pedido"  # Solo permitido en Recogida de Pedidos web
    })
    assert r.status_code == 400
    assert "motivo" in str(r.get_json()).lower()


def test_motivo_faltante_422(c):
    """Pedir turno sin motivo responde 422."""
    r = c.post("/turnos/api/pedir", json={"servicio_id": 1})
    assert r.status_code == 422


def test_otra_consulta_sin_texto_422(c):
    """'Otra consulta' sin detalle o con texto en blanco responde 422."""
    r1 = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Otra consulta"
    })
    assert r1.status_code == 422

    r2 = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Otra consulta",
        "detalle": "    "
    })
    assert r2.status_code == 422

    # Con texto válido sí pasa
    r3 = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Otra consulta",
        "detalle": "Tengo una duda sobre un convenio empresarial"
    })
    assert r3.status_code == 200


def test_detalle_muy_largo_422(c):
    """Detalle de más de 500 caracteres responde 422."""
    r = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Otra consulta",
        "detalle": "x" * 501
    })
    assert r.status_code == 422


def test_detalle_limpia_html(app_tur, c):
    """El detalle se almacena sin etiquetas HTML."""
    r = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Información de un producto",
        "detalle": "<script>alert('xss')</script>Consulta <b>importante</b>"
    })
    assert r.status_code == 200
    num = r.get_json()["numero"]

    conn = sqlite3.connect(app_tur.config["DB_PATH"])
    fila = conn.execute("SELECT detalle FROM turnos WHERE numero = ?", (num,)).fetchone()
    conn.close()
    assert "<script>" not in fila[0]
    assert "<b>" not in fila[0]
    assert "alert('xss')Consulta importante" in fila[0]


def test_pedido_propio_vs_pedido_ajeno(app_tur, c):
    """Un cliente con sesión puede asociar un pedido propio; pedido ajeno da 404/422."""
    # 1. Crear producto y registrar compra para Carlos
    conn = sqlite3.connect(app_tur.config["DB_PATH"])
    conn.execute("INSERT OR IGNORE INTO productos (product_id, product_name, category, sub_category, precio, stock, activo) "
                 "VALUES ('P-01', 'Libro Test', 'Tecnología', 'Phones', 20.0, 10, 1)")
    conn.commit(); conn.close()

    assert c.post("/cuenta/registro", json=CUENTA_1).status_code == 201
    dest = {"pais": "Colombia", "region": "Córdoba", "ciudad": "Montería"}
    opciones = c.get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000").get_json()
    base = {"opcion_envio": opciones["opciones"][0]["id"], "destino": dest,
            "items": [{"product_id": "P-01", "product_name": "Libro Test", "quantity": 1}]}
    pago = c.post("/cuenta/pago", json={**base, "tarjeta": TARJETA}).get_json()
    oid_carlos = c.post("/cuenta/pedido", json={**base, "pago_id": pago["pago_id"]}).get_json()["order_id"]

    # Carlos pide turno con su propio pedido -> 200
    r_carlos = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Estado de mi pedido",
        "pedido_id": oid_carlos
    })
    assert r_carlos.status_code == 200
    num_carlos = r_carlos.get_json()["numero"]

    # Salir de Carlos y registrar a Diana
    c.post("/cuenta/salir")
    assert c.post("/cuenta/registro", json=CUENTA_2).status_code == 201

    # Diana intenta asociar el pedido de Carlos -> 404 o 422
    r_ajeno = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Estado de mi pedido",
        "pedido_id": oid_carlos
    })
    assert r_ajeno.status_code in (404, 422)

    # Anónimo sin sesión intenta asociar pedido -> 422
    c.post("/cuenta/salir")
    r_anon = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Estado de mi pedido",
        "pedido_id": oid_carlos
    })
    assert r_anon.status_code == 422

    # Verificar en BD que el turno de Carlos tiene el pedido_id
    conn = sqlite3.connect(app_tur.config["DB_PATH"])
    fila = conn.execute("SELECT pedido_id FROM turnos WHERE numero = ?", (num_carlos,)).fetchone()
    conn.close()
    assert fila[0] == oid_carlos


def test_admin_ve_motivo_detalle_y_pedido(c):
    """El administrador ve el motivo, detalle y pedido_id en la lista de turnos (sin correo ni teléfono)."""
    # Pedir turno con motivo y detalle
    r_pedir = c.post("/turnos/api/pedir", json={
        "servicio_id": 1,
        "motivo": "Garantía o devolución",
        "detalle": "El producto llegó roto en una esquina"
    })
    assert r_pedir.status_code == 200
    num = r_pedir.get_json()["numero"]

    # Iniciar sesión de admin
    assert c.post("/admin/registro", json=ADMIN).status_code == 201

    # Consultar lista
    r_lista = c.get("/admin/turnos/api/lista")
    assert r_lista.status_code == 200
    servicios = r_lista.get_json()
    s1 = next(s for s in servicios if s["id"] == 1)
    turno = next(t for t in s1["activos"] if t["numero"] == num)

    assert turno["motivo"] == "Garantía o devolución"
    assert turno["detalle"] == "El producto llegó roto en una esquina"
    # Privacidad de datos personales (Ley 1581)
    assert "email" not in turno and "correo" not in turno and "telefono" not in turno

