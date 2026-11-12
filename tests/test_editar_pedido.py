# -*- coding: utf-8 -*-
"""Cancelar o modificar un pedido de la tienda dentro de la ventana de tiempo."""
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402
from cargar_datos import CSV_POR_DEFECTO, cargar  # noqa: E402

DESTINO = {"pais": "Colombia", "region": "Córdoba", "ciudad": "Montería"}
CUENTA = {"nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com", "password": "claveSegura1",
          "telefono": "3001234567", "direccion": "Calle 1 # 2-3", "codigo_postal": "230001",
          "autorizacion_datos": True, **DESTINO}
TARJETA = {"numero": "4242 4242 4242 4242", "nombre": "Ana Pérez", "vence": "12/40", "cvv": "123"}


@pytest.fixture()
def e(tmp_path):
    ruta = str(tmp_path / "edit.db")
    cargar(CSV_POR_DEFECTO, ruta)
    app = create_app({"DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "a", "TESTING": True})
    conn = sqlite3.connect(ruta)
    prods = conn.execute("SELECT product_id, product_name FROM productos WHERE activo = 1 LIMIT 2").fetchall()
    conn.execute("UPDATE productos SET stock = 50")
    conn.commit()
    c = app.test_client()
    assert c.post("/cuenta/registro", json=CUENTA).status_code == 201
    return {"app": app, "c": c, "conn": conn, "prods": prods}


def stock(e, i):
    return e["conn"].execute("SELECT stock FROM productos WHERE product_id = ? AND product_name = ?",
                             e["prods"][i]).fetchone()[0]


def comprar(e, cants=(3, 2)):
    opciones = e["c"].get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000").get_json()
    base = {"opcion_envio": opciones["opciones"][-1]["id"], "destino": DESTINO,
            "items": [{"product_id": p, "product_name": n, "quantity": q} for (p, n), q in zip(e["prods"], cants)]}
    pago = e["c"].post("/cuenta/pago", json={**base, "tarjeta": TARJETA}).get_json()
    r = e["c"].post("/cuenta/pedido", json={**base, "pago_id": pago["pago_id"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["order_id"]


def envejecer(e, order_id, minutos):
    viejo = (datetime.now() - timedelta(minutes=minutos)).isoformat(timespec="seconds")
    e["conn"].execute("UPDATE pedidos SET creado = ? WHERE order_id = ?", (viejo, order_id))
    e["conn"].commit()


def items(e, oid, cants):
    return {"items": [{"product_id": p, "product_name": n, "quantity": q} for (p, n), q in zip(e["prods"], cants)]}


def test_lista_dice_si_se_puede_editar(e):
    oid = comprar(e)
    r = e["c"].get("/cuenta/pedidos").get_json()
    p = r["resultados"][0]
    assert r["ventana_minutos"] == 1440 and p["order_id"] == oid and p["editable"] is True
    assert p["editable_hasta"] and len(p["items"]) == 2 and p["items"][0]["product_id"]
    envejecer(e, oid, 1445)
    assert e["c"].get("/cuenta/pedidos").get_json()["resultados"][0]["editable"] is False


def test_cancelar_devuelve_stock_reembolsa_y_sale_de_reportes(e):
    campos = ("pedidos", "lineas_de_pedido", "ventas_totales", "ganancia_total")
    resumen = lambda: {k: e["c"].get("/resumen", headers={"X-API-Key": "u"}).get_json()[k] for k in campos}
    ventas0 = resumen()
    oid = comprar(e)
    assert (stock(e, 0), stock(e, 1)) == (47, 48)
    r = e["c"].post(f"/cuenta/pedidos/{oid}/cancelar")
    assert r.status_code == 200 and r.get_json()["pedido"]["estado"] == "cancelado"
    assert (stock(e, 0), stock(e, 1)) == (50, 50)
    assert e["conn"].execute("SELECT estado FROM pagos WHERE order_id = ?", (oid,)).fetchone()[0] == "reembolsado"
    assert resumen() == ventas0
    assert e["c"].post(f"/cuenta/pedidos/{oid}/cancelar").status_code == 409      # no se cancela dos veces
    assert (stock(e, 0), stock(e, 1)) == (50, 50)


def test_fuera_de_la_ventana_no_se_puede_cancelar_ni_modificar(e):
    oid = comprar(e)
    envejecer(e, oid, 1445)
    assert e["c"].post(f"/cuenta/pedidos/{oid}/cancelar").status_code == 409
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (1, 2))).status_code == 409
    assert (stock(e, 0), stock(e, 1)) == (47, 48)


def test_modificar_baja_cantidades_devuelve_stock_y_reembolsa_la_diferencia(e):
    oid = comprar(e)
    antes = e["conn"].execute("SELECT monto_cop FROM pagos WHERE order_id = ?", (oid,)).fetchone()[0]
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (1, 2)))
    assert r.status_code == 200
    d = r.get_json()
    assert d["reembolso_cop"] > 0 and d["pedido"]["total_cop"] == antes - d["reembolso_cop"]
    assert [i["cantidad"] for i in d["pedido"]["items"]] == [1, 2]
    assert (stock(e, 0), stock(e, 1)) == (49, 48)


def test_modificar_puede_quitar_un_producto_pero_no_todos(e):
    oid = comprar(e)
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (0, 2)))
    assert r.status_code == 200 and len(r.get_json()["pedido"]["items"]) == 1
    assert stock(e, 0) == 50
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (0, 0))).status_code == 422
    assert stock(e, 1) == 48                                   # el rechazo no tocó nada


@pytest.mark.parametrize("cants", [(4, 2), (3, 2), (-1, 2), (1.5, 2)])
def test_modificar_no_acepta_aumentos_ni_cambios_vacios_ni_basura(e, cants):
    oid = comprar(e)
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, cants)).status_code == 422
    assert (stock(e, 0), stock(e, 1)) == (47, 48)


def test_no_se_puede_tocar_el_pedido_de_otra_cuenta_ni_sin_sesion(e):
    oid = comprar(e)
    otra = e["app"].test_client()
    assert otra.post(f"/cuenta/pedidos/{oid}/cancelar").status_code == 401
    assert otra.post("/cuenta/registro", json=dict(CUENTA, email="otra@example.com")).status_code == 201
    assert otra.post(f"/cuenta/pedidos/{oid}/cancelar").status_code == 404
    assert otra.put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (1, 1))).status_code == 404
    assert (stock(e, 0), stock(e, 1)) == (47, 48)


def test_base_vieja_sin_columnas_se_migra(tmp_path):
    ruta = str(tmp_path / "vieja.db")
    cargar(CSV_POR_DEFECTO, ruta)
    c = sqlite3.connect(ruta)
    c.executescript("ALTER TABLE pedidos DROP COLUMN creado; ALTER TABLE pedidos DROP COLUMN envio_cop;"
                    "ALTER TABLE pedidos DROP COLUMN cancelado_en;")
    c.commit()
    create_app({"DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "a", "TESTING": True})
    assert {"creado", "envio_cop", "cancelado_en", "dest_pais", "dest_ciudad"} <= {f[1] for f in c.execute("PRAGMA table_info(pedidos)")}


def test_modificar_direccion_y_telefono(e):
    oid = comprar(e)
    nuevo_dest = {
        "pais": "Colombia", "region": "Córdoba", "ciudad": "Lorica",
        "direccion": "Carrera 5 # 10-20, Barrio Centro", "codigo_postal": "230001",
        "telefono": "+57 310 987 6543"
    }
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json={"destino": nuevo_dest})
    assert r.status_code == 200
    p = r.get_json()["pedido"]
    assert p["dest_ciudad"] == "Lorica" and p["dest_telefono"] == "+57 310 987 6543"
    assert p["dest_direccion"] == "Carrera 5 # 10-20, Barrio Centro"


def test_modificar_envio_baja_costo_reembolsa(e):
    # Comprar con la opción más rápida/costosa (mismo_dia o express)
    opciones = e["c"].get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000").get_json()
    opt_cara = opciones["opciones"][0]      # la primera es la más rápida y costosa
    base = {"opcion_envio": opt_cara["id"], "destino": DESTINO,
            "items": [{"product_id": p, "product_name": n, "quantity": 1} for (p, n) in e["prods"]]}
    pago = e["c"].post("/cuenta/pago", json={**base, "tarjeta": TARJETA}).get_json()
    oid = e["c"].post("/cuenta/pedido", json={**base, "pago_id": pago["pago_id"]}).get_json()["order_id"]

    antes_pago = e["conn"].execute("SELECT monto_cop FROM pagos WHERE order_id = ?", (oid,)).fetchone()[0]

    # Cambiar a estándar (más económica)
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json={"opcion_envio": "estandar"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["reembolso_cop"] > 0
    assert d["pedido"]["envio_cop"] < opt_cara["costo_cop"]
    nuevo_pago = e["conn"].execute("SELECT monto_cop FROM pagos WHERE order_id = ?", (oid,)).fetchone()[0]
    assert nuevo_pago == antes_pago - d["reembolso_cop"]


def test_modificar_envio_sube_costo_responde_409(e):
    # Comprar con la opción estándar (la última de la lista)
    opciones = e["c"].get("/tienda/envio?pais=Colombia&region=C%C3%B3rdoba&ciudad=Monter%C3%ADa&subtotal_cop=100000").get_json()
    opt_barata = opciones["opciones"][-1]
    base = {"opcion_envio": opt_barata["id"], "destino": DESTINO,
            "items": [{"product_id": p, "product_name": n, "quantity": 1} for (p, n) in e["prods"]]}
    pago = e["c"].post("/cuenta/pago", json={**base, "tarjeta": TARJETA}).get_json()
    oid = e["c"].post("/cuenta/pedido", json={**base, "pago_id": pago["pago_id"]}).get_json()["order_id"]

    # Intentar cambiar a express (cuesta más)
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json={"opcion_envio": "express"})
    assert r.status_code == 409
    assert "más" in r.get_json()["error"]


def test_modificar_validaciones_y_rechazos(e):
    oid = comprar(e)
    # Sin cambios -> 422
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json={}).status_code == 422

    # Destino inválido -> 422
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json={"destino": {"pais": "PaisFicticio"}}).status_code == 422
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json={"telefono": "123"}).status_code == 422

    # Opción de envío inexistente -> 422
    assert e["c"].put(f"/cuenta/pedidos/{oid}", json={"opcion_envio": "teletransportacion"}).status_code == 422


# ----------------------------------------------------------- Tarea 2: edición independiente y cancelar
def test_cambiar_solo_cantidades_sin_direccion(e):
    oid = comprar(e, cants=(3, 2))
    p_antes = e["c"].get("/cuenta/pedidos").get_json()["resultados"][0]
    dest_antes = p_antes["destino"]

    # Enviar solo items sin dirección ni teléfono ni envío
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json=items(e, oid, (2, 1)))
    assert r.status_code == 200
    d = r.get_json()
    assert d["reembolso_cop"] > 0
    p_despues = d["pedido"]
    assert [i["cantidad"] for i in p_despues["items"]] == [2, 1]
    # La dirección debe seguir exactamente igual
    assert p_despues["destino"] == dest_antes
    assert (stock(e, 0), stock(e, 1)) == (48, 49)


def test_cambiar_solo_direccion(e):
    oid = comprar(e, cants=(2, 2))
    nuevo_dest = {
        "pais": "Colombia", "region": "Córdoba", "ciudad": "Cereté",
        "direccion": "Calle 14 # 12-34, Barrio Centro", "codigo_postal": "230001",
        "telefono": "+57 320 555 4321"
    }
    # Enviar solo destino sin items ni opción de envío
    r = e["c"].put(f"/cuenta/pedidos/{oid}", json={"destino": nuevo_dest})
    assert r.status_code == 200
    p = r.get_json()["pedido"]
    assert p["dest_ciudad"] == "Cereté"
    assert p["dest_direccion"] == "Calle 14 # 12-34, Barrio Centro"
    assert p["dest_telefono"] == "+57 320 555 4321"
    # Las cantidades y stock no deben cambiar
    assert [i["cantidad"] for i in p["items"]] == [2, 2]
    assert (stock(e, 0), stock(e, 1)) == (48, 48)


def test_cancelar_sin_mas_datos(e):
    oid = comprar(e, cants=(2, 1))
    assert (stock(e, 0), stock(e, 1)) == (48, 49)
    # Cancelar solo requiere llamar al endpoint sin enviar dirección ni nada extra
    r = e["c"].post(f"/cuenta/pedidos/{oid}/cancelar")
    assert r.status_code == 200
    p = r.get_json()["pedido"]
    assert p["estado"] == "cancelado"
    # Stock devuelto y pago reembolsado
    assert (stock(e, 0), stock(e, 1)) == (50, 50)
    assert e["conn"].execute("SELECT estado FROM pagos WHERE order_id = ?", (oid,)).fetchone()[0] == "reembolsado"
