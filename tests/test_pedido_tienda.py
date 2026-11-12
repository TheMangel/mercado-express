# -*- coding: utf-8 -*-
"""Compra de la tienda: pago con pasarela simulada (/cuenta/pago) + pedido (/cuenta/pedido)."""
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402
from cargar_datos import CSV_POR_DEFECTO, cargar  # noqa: E402

USER = {"X-API-Key": "clave-usuario"}
DESTINO = {"pais": "Colombia", "region": "Córdoba", "ciudad": "Montería"}
CUENTA = {"nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com", "password": "claveSegura1",
          "telefono": "3001234567", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001",
          "autorizacion_datos": True, **DESTINO}
OK = {"numero": "4242 4242 4242 4242", "nombre": "Ana Pérez", "vence": "12/40", "cvv": "123"}
SIN_FONDOS = dict(OK, numero="4000 0000 0000 9995")


@pytest.fixture(scope="module")
def entorno(tmp_path_factory):
    ruta = str(tmp_path_factory.mktemp("bd") / "pedido.db")
    cargar(CSV_POR_DEFECTO, ruta)                       # base con los productos del dataset
    app = create_app({"DB_PATH": ruta, "API_KEY": "clave-usuario", "ADMIN_KEY": "clave-admin", "TESTING": True})
    conn = sqlite3.connect(ruta)
    pid, nom = conn.execute("SELECT product_id, product_name FROM productos WHERE activo = 1 LIMIT 1").fetchone()
    conn.execute("UPDATE productos SET stock = 50 WHERE product_id = ? AND product_name = ?", (pid, nom))
    conn.commit()
    return {"app": app, "conn": conn, "pid": pid, "nom": nom}


def stock(e):
    return e["conn"].execute("SELECT stock FROM productos WHERE product_id = ? AND product_name = ?",
                             (e["pid"], e["nom"])).fetchone()[0]


def contar(e, tabla):
    return e["conn"].execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def base(e, opcion, cantidad=2):
    return {"opcion_envio": opcion, "destino": DESTINO,
            "items": [{"product_id": e["pid"], "product_name": e["nom"], "quantity": cantidad}]}


@pytest.fixture(scope="module")
def cliente(entorno):
    c = entorno["app"].test_client()
    assert c.post("/cuenta/registro", json=CUENTA).status_code == 201     # deja la sesión abierta
    return c


@pytest.fixture(scope="module")
def opcion(cliente):
    r = cliente.get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000")
    assert r.status_code == 200
    return r.get_json()["opciones"][0]["id"]


def pagar(cliente, e, opcion, tarjeta=OK, cantidad=2):
    return cliente.post("/cuenta/pago", json={**base(e, opcion, cantidad), "tarjeta": tarjeta})


# ---------------------------------------------------------------- sesión y pago
def test_sin_sesion_401(entorno, opcion):
    anonimo = entorno["app"].test_client()
    assert anonimo.post("/cuenta/pago", json={**base(entorno, opcion), "tarjeta": OK}).status_code == 401
    assert anonimo.post("/cuenta/pedido", json=base(entorno, opcion)).status_code == 401


def test_pago_aprobado_no_guarda_la_tarjeta(entorno, cliente, opcion):
    r = pagar(cliente, entorno, opcion)
    assert r.status_code == 201
    d = r.get_json()
    assert d["estado"] == "aprobado" and d["ultimos4"] == "4242" and d["marca"] == "Visa"
    fila = entorno["conn"].execute("SELECT * FROM pagos WHERE pago_id = ?", (d["pago_id"],)).fetchone()
    assert "4242424242424242" not in str(fila) and "123" not in str(fila[:-1][4:])   # ni número ni CVV
    texto = r.get_data(as_text=True)
    assert "4242 4242" not in texto and "4242424242424242" not in texto


@pytest.mark.parametrize("tarjeta,motivo", [(SIN_FONDOS, "Fondos insuficientes"),
                                           (dict(OK, numero="4000 0000 0000 0002"), "rechazó"),
                                           (dict(OK, numero="4111 1111 1111 1111"), "no reconocida")])
def test_pago_rechazado_402_y_queda_registrado(entorno, cliente, opcion, tarjeta, motivo):
    antes = contar(entorno, "pagos")
    r = pagar(cliente, entorno, opcion, tarjeta)
    assert r.status_code == 402 and motivo in r.get_json()["error"]
    assert contar(entorno, "pagos") == antes + 1
    assert entorno["conn"].execute("SELECT estado FROM pagos ORDER BY rowid DESC").fetchone()[0] == "rechazado"


@pytest.mark.parametrize("cambio", [
    {"numero": "1234 5678 9012 3456"}, {"numero": "abcd"}, {"vence": "13/30"}, {"vence": "01/20"},
    {"cvv": "12"}, {"nombre": ""},
])
def test_formato_de_tarjeta_invalido_422_sin_registrar_pago(entorno, cliente, opcion, cambio):
    antes = contar(entorno, "pagos")
    r = pagar(cliente, entorno, opcion, dict(OK, **cambio))
    assert r.status_code == 422
    assert contar(entorno, "pagos") == antes


# ---------------------------------------------------------------- pedido
def test_compra_completa_guarda_pedido_enlaza_pago_y_descuenta_stock(entorno, cliente, opcion):
    antes = stock(entorno)
    pago = pagar(cliente, entorno, opcion, cantidad=2).get_json()
    r = cliente.post("/cuenta/pedido", json={**base(entorno, opcion, 2), "pago_id": pago["pago_id"]})
    assert r.status_code == 201
    d = r.get_json()
    assert d["total_cop"] == pago["monto_cop"] and d["ultimos4"] == "4242"
    assert stock(entorno) == antes - 2
    c = entorno["conn"]
    assert c.execute("SELECT order_id FROM pagos WHERE pago_id = ?", (pago["pago_id"],)).fetchone()[0] == d["order_id"]
    assert c.execute("SELECT customer_id FROM pedidos WHERE order_id = ?", (d["order_id"],)).fetchone()[0].startswith("WEB-")
    assert cliente.get(f"/pedidos/{d['order_id']}", headers=USER).status_code == 200


def test_pedido_sin_pago_402_y_no_guarda_nada(entorno, cliente, opcion):
    antes, pedidos = stock(entorno), contar(entorno, "pedidos")
    for body in (base(entorno, opcion), {**base(entorno, opcion), "pago_id": "PAG-NOEXISTE"}):
        assert cliente.post("/cuenta/pedido", json=body).status_code == 402
    assert stock(entorno) == antes and contar(entorno, "pedidos") == pedidos


def test_un_pago_sirve_para_un_solo_pedido(entorno, cliente, opcion):
    pago = pagar(cliente, entorno, opcion, cantidad=1).get_json()
    cuerpo = {**base(entorno, opcion, 1), "pago_id": pago["pago_id"]}
    assert cliente.post("/cuenta/pedido", json=cuerpo).status_code == 201
    antes = contar(entorno, "pedidos")
    assert cliente.post("/cuenta/pedido", json=cuerpo).status_code == 402
    assert contar(entorno, "pedidos") == antes


def test_pago_de_otra_cuenta_no_sirve(entorno, cliente, opcion):
    pago = pagar(cliente, entorno, opcion, cantidad=1).get_json()
    otra = entorno["app"].test_client()
    assert otra.post("/cuenta/registro", json=dict(CUENTA, email="otra@example.com")).status_code == 201
    assert otra.post("/cuenta/pedido", json={**base(entorno, opcion, 1), "pago_id": pago["pago_id"]}).status_code == 402


def test_si_el_carrito_cambia_despues_de_pagar_se_reembolsa(entorno, cliente, opcion):
    pago = pagar(cliente, entorno, opcion, cantidad=1).get_json()
    pedidos = contar(entorno, "pedidos")
    r = cliente.post("/cuenta/pedido", json={**base(entorno, opcion, 3), "pago_id": pago["pago_id"]})
    assert r.status_code == 422 and "reembolsado" in r.get_json()["error"]
    assert contar(entorno, "pedidos") == pedidos
    assert entorno["conn"].execute("SELECT estado FROM pagos WHERE pago_id = ?", (pago["pago_id"],)).fetchone()[0] == "reembolsado"


def test_pago_vencido_se_reembolsa(entorno, cliente, opcion):
    pago = pagar(cliente, entorno, opcion, cantidad=1).get_json()
    viejo = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    entorno["conn"].execute("UPDATE pagos SET creado = ? WHERE pago_id = ?", (viejo, pago["pago_id"]))
    entorno["conn"].commit()
    r = cliente.post("/cuenta/pedido", json={**base(entorno, opcion, 1), "pago_id": pago["pago_id"]})
    assert r.status_code == 422 and "venció" in r.get_json()["error"]


def test_stock_insuficiente_se_detecta_antes_de_cobrar(entorno, cliente, opcion):
    antes = contar(entorno, "pagos")
    r = pagar(cliente, entorno, opcion, cantidad=99)
    assert r.status_code == 422
    assert contar(entorno, "pagos") == antes                    # nunca se llegó a cobrar


@pytest.mark.parametrize("cambio", [{"opcion_envio": "volando"}, {"destino": {"pais": "Narnia"}}, {"items": []}])
def test_carrito_u_opcion_invalidos_422(entorno, cliente, opcion, cambio):
    assert cliente.post("/cuenta/pago", json={**base(entorno, opcion), **cambio, "tarjeta": OK}).status_code == 422


def test_cinco_rechazos_seguidos_bloquean_los_intentos(entorno, opcion):
    c = entorno["app"].test_client()
    assert c.post("/cuenta/registro", json=dict(CUENTA, email="probador@example.com")).status_code == 201
    codigos = [pagar(c, entorno, opcion, SIN_FONDOS).status_code for _ in range(6)]
    assert codigos[:5] == [402] * 5 and codigos[5] == 429
