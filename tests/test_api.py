# -*- coding: utf-8 -*-
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402

USER = {"X-API-Key": "clave-usuario"}
ADMIN = {"X-API-Key": "clave-admin"}

PRODUCTO = {"product_id": "FUR-BO-10001798", "product_name": "Bush Somerset Collection Bookcase"}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    ruta = tmp_path_factory.mktemp("bd") / "test.db"
    return create_app({"CARGAR_DATASET": True, "DB_PATH": str(ruta), "API_KEY": "clave-usuario", "ADMIN_KEY": "clave-admin",
                       "TESTING": True})


@pytest.fixture()
def c(app):
    return app.test_client()


def pedido_valido(**extra):
    base = {"customer_id": "CG-12520", "modo_envio": "Standard Class",
            "fecha_pedido": "2026-10-01", "fecha_envio": "2026-10-05",
            "items": [dict(PRODUCTO, sales=150.0, quantity=1, discount=0.0, profit=30.0)]}
    base.update(extra)
    return base


def contar(app, tabla):
    conn = sqlite3.connect(app.config["DB_PATH"])
    n = conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    conn.close()
    return n


# ----------------------------------------------------------- datos cargados
def test_datos_cargados_igual_que_en_e1(c):
    r = c.get("/resumen", headers=USER).get_json()
    assert r["lineas_de_pedido"] == 9994
    assert r["clientes"] > 0 and r["pedidos"] > 0


def test_producto_con_id_repetido_sigue_siendo_dos_productos(app):
    # Hallazgo del E1: FUR-FU-10004270 se usó para dos productos distintos (llave compuesta)
    conn = sqlite3.connect(app.config["DB_PATH"])
    n = conn.execute("SELECT COUNT(*) FROM productos WHERE product_id = 'FUR-FU-10004270'").fetchone()[0]
    conn.close()
    assert n == 2


# ----------------------------------------------------------- autenticación
def test_sin_clave_401(c):
    assert c.get("/clientes").status_code == 401
    assert c.get("/clientes", headers={"X-API-Key": "mala"}).status_code == 401


def test_salud_es_publica(c):
    assert c.get("/salud").status_code == 200


def test_pantallas_se_sirven_sin_clave_pero_los_datos_no(c):
    tienda, admin = c.get("/"), c.get("/admin")
    assert tienda.status_code == 200 and b"Mercado Express" in tienda.data
    assert admin.status_code == 200 and b"Mercado Express" in admin.data
    assert c.get("/clientes").status_code == 401
    assert c.get("/clientes", headers=USER).headers["Cache-Control"] == "no-store"


# ----------------------------------------------------------- tienda pública
def test_tienda_es_publica_y_trae_precio_y_vendidos(c):
    r = c.get("/tienda/productos?limite=5")  # sin clave
    assert r.status_code == 200
    d = r.get_json()
    assert d["total"] > 1000 and len(d["resultados"]) == 5
    for p in d["resultados"]:
        assert p["precio"] > 0 and p["vendidos"] > 0 and 0 <= p["descuento_max"] <= 1
    vendidos = [p["vendidos"] for p in d["resultados"]]
    assert vendidos == sorted(vendidos, reverse=True)  # más vendidos primero


def test_tienda_no_expone_datos_personales(c):
    cuerpo = c.get("/tienda/productos?limite=100").get_data(as_text=True)
    for prohibido in ("customer", "cliente", "Claire", "postal", "CG-12520"):
        assert prohibido not in cuerpo
    assert set(c.get("/tienda/resumen").get_json()) == {"productos", "categorias", "pedidos", "descuento_max"}


def test_tienda_filtros_orden_y_paginacion(c):
    r = c.get("/tienda/productos?categoria=Technology&subcategoria=Phones&orden=precio_asc&limite=6").get_json()
    assert r["resultados"] and all(p["categoria"] == "Technology" and p["subcategoria"] == "Phones" for p in r["resultados"])
    precios = [p["precio"] for p in r["resultados"]]
    assert precios == sorted(precios)
    busc = c.get("/tienda/productos?q=bookcase").get_json()
    assert busc["total"] > 0 and all("bookcase" in p["nombre"].lower() for p in busc["resultados"])
    p1 = c.get("/tienda/productos?limite=4&pagina=1").get_json()["resultados"]
    p2 = c.get("/tienda/productos?limite=4&pagina=2").get_json()["resultados"]
    assert not {x["product_id"] + x["nombre"] for x in p1} & {x["product_id"] + x["nombre"] for x in p2}


def test_tienda_rechaza_orden_invalido_y_inyeccion(c):
    assert c.get("/tienda/productos?orden=precio;DROP TABLE productos").status_code == 400
    assert c.get("/tienda/productos?limite=1000").status_code == 400
    assert c.get("/tienda/productos?q=%27%20OR%201%3D1--").status_code == 200  # ' OR 1=1--  (es solo texto)


def test_tienda_config_trae_tasa_cop(c):
    d = c.get("/tienda/config").get_json()  # público
    assert d["monedas"] == ["USD", "COP"] and d["tasa_cop"] > 1000


# ----------------------------------------------------------- cuentas de cliente
CUENTA = {"nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com", "password": "clave-segura-1",
          "telefono": "+57 300 123 4567", "pais": "Colombia", "region": "Córdoba", "ciudad": "Montería",
          "direccion": "Calle 27 # 5-30, Barrio Centro", "codigo_postal": "230001", "autorizacion_datos": True}


def test_registro_crea_sesion_con_cookie_protegida_y_guarda_hash(app, c):
    r = c.post("/cuenta/registro", json=CUENTA)  # sin X-API-Key: es público
    assert r.status_code == 201 and r.get_json()["nombre"] == "Ana" and r.get_json()["apellido"] == "Pérez"
    cookie = r.headers["Set-Cookie"]
    assert "me_sesion=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    yo = c.get("/cuenta/yo").get_json()
    assert yo["sesion"] is True and yo["cuenta"]["nombre"] == "Ana" and yo["cuenta"]["email"] == "ana@example.com"
    conn = sqlite3.connect(app.config["DB_PATH"])
    guardado = conn.execute("SELECT password_hash FROM cuentas WHERE email = 'ana@example.com'").fetchone()[0]
    token = c.get_cookie("me_sesion").value
    en_bd = [f[0] for f in conn.execute("SELECT token_hash FROM sesiones")]
    conn.close()
    assert "clave-segura-1" not in guardado and guardado.count("$") >= 2   # hash, no texto
    assert token not in en_bd                                              # se guarda el hash del token


def test_registro_duplicado_y_datos_invalidos(c):
    assert c.post("/cuenta/registro", json=dict(CUENTA, email="dup@example.com")).status_code == 201
    assert c.post("/cuenta/registro", json=dict(CUENTA, email="DUP@example.com")).status_code == 409
    for malo in ({"password": "corta"}, {"email": "no-es-correo"}, {"nombre": "A"}, {"autorizacion_datos": False}):
        r = c.post("/cuenta/registro", json={**CUENTA, "email": "otro@example.com", **malo})
        assert r.status_code == 422 and r.get_json()["detalles"]


def test_entrar_salir_y_mensaje_generico(c):
    c.post("/cuenta/registro", json=dict(CUENTA, email="luis@example.com"))
    c2 = c.application.test_client()
    assert c2.get("/cuenta/yo").get_json()["sesion"] is False
    mal_clave = c2.post("/cuenta/entrar", json={"email": "luis@example.com", "password": "incorrecta1"})
    no_existe = c2.post("/cuenta/entrar", json={"email": "nadie@example.com", "password": "incorrecta1"})
    assert mal_clave.status_code == no_existe.status_code == 401
    assert mal_clave.get_json() == no_existe.get_json()        # no revela si el correo existe
    assert c2.post("/cuenta/entrar", json={"email": "luis@example.com", "password": CUENTA["password"]}).status_code == 200
    assert c2.get("/cuenta/yo").get_json()["sesion"] is True
    c2.post("/cuenta/salir")
    assert c2.get("/cuenta/yo").get_json()["sesion"] is False


def test_bloqueo_tras_5_intentos_fallidos(c):
    c.post("/cuenta/registro", json=dict(CUENTA, email="bloq@example.com"))
    c2 = c.application.test_client()
    for _ in range(5):
        assert c2.post("/cuenta/entrar", json={"email": "bloq@example.com", "password": "mala-clave-1"}).status_code == 401
    assert c2.post("/cuenta/entrar", json={"email": "bloq@example.com", "password": CUENTA["password"]}).status_code == 429


def test_cuenta_rechaza_cuerpo_que_no_es_json(c):
    assert c.post("/cuenta/entrar", data="email=a@b.co&password=x").status_code == 400


def test_la_sesion_de_cliente_no_abre_el_panel_de_gestion(c):
    c.post("/cuenta/registro", json=dict(CUENTA, email="cli@example.com"))
    assert c.get("/clientes").status_code == 401      # tener cuenta no da acceso al panel


def test_tienda_categorias(c):
    cats = c.get("/tienda/categorias").get_json()
    assert {x["categoria"] for x in cats} == {"Furniture", "Office Supplies", "Technology"}
    assert sum(len(x["subcategorias"]) for x in cats) == 17


# ----------------------------------------------------------- datos personales (Ley 1581)
def test_nombres_enmascarados_por_defecto(c):
    r = c.get("/clientes/CG-12520", headers=USER).get_json()
    assert r["nombre"] == "Claire G."
    assert "ciudad" not in r and "codigo_postal" not in r


def test_usuario_normal_no_puede_pedir_datos_completos(c):
    assert c.get("/clientes/CG-12520?completo=1", headers=USER).status_code == 403
    assert c.get("/reportes/ganancia-por-cliente?completo=1", headers=USER).status_code == 403


def test_admin_ve_datos_completos_y_queda_auditado(c):
    r = c.get("/clientes/CG-12520?completo=1", headers=ADMIN).get_json()
    assert r["nombre"] == "Claire Gute" and r["ciudad"] == "Henderson"
    log = c.get("/admin/auditoria", headers=ADMIN).get_json()
    assert any("CG-12520" in (f["detalle"] or "") for f in log)
    assert c.get("/admin/auditoria", headers=USER).status_code == 403


def test_crear_cliente_exige_autorizacion(c):
    cuerpo = {"nombre": "Ana Pérez", "segmento": "Consumer", "pais": "Colombia", "ciudad": "Montería",
              "estado": "Córdoba", "codigo_postal": "230001", "region": "South"}
    assert c.post("/clientes", json=cuerpo, headers=USER).status_code == 422
    assert c.post("/clientes", json=dict(cuerpo, autorizacion_datos=False), headers=USER).status_code == 422
    r = c.post("/clientes", json=dict(cuerpo, autorizacion_datos=True), headers=USER)
    assert r.status_code == 201
    nuevo = r.get_json()["customer_id"]
    ficha = c.get(f"/clientes/{nuevo}?completo=1", headers=ADMIN).get_json()
    assert ficha["autorizacion_datos"] is True


# ----------------------------------------------------------- consultas
def test_paginacion_y_filtros(c):
    r = c.get("/clientes?limite=5&pagina=2&region=West", headers=USER).get_json()
    assert len(r["resultados"]) == 5 and r["pagina"] == 2
    assert all(x["region"] == "West" for x in r["resultados"])
    assert c.get("/clientes?limite=1000", headers=USER).status_code == 400
    assert c.get("/clientes?pagina=abc", headers=USER).status_code == 400


def test_busqueda_con_comodines_no_rompe(c):
    assert c.get("/productos?q=%25%27%3B--", headers=USER).status_code == 200  # %'; --


def test_ver_pedido_del_dataset(c):
    r = c.get("/pedidos/CA-2016-152156", headers=USER).get_json()
    assert r["customer_id"] == "CG-12520" and len(r["items"]) >= 1
    assert c.get("/pedidos/NO-EXISTE", headers=USER).status_code == 404


# ----------------------------------------------------------- reportes
def test_reportes_coinciden_con_el_e1(c):
    cat = c.get("/reportes/ventas-por-categoria", headers=USER).get_json()
    assert len(cat) == 10 and cat[0]["total_ventas"] >= cat[-1]["total_ventas"]
    cli = c.get("/reportes/ganancia-por-cliente", headers=USER).get_json()
    assert len(cli) == 10 and "." in cli[0]["nombre"]  # enmascarado: "Tamara C."
    env = c.get("/reportes/envio-por-region", headers=USER).get_json()
    assert len(env) == 16  # 4 modos de envío x 4 regiones, como en el E1


# ----------------------------------------------------------- transacción
def test_pedido_valido_se_guarda_completo(app, c):
    antes_p, antes_d = contar(app, "pedidos"), contar(app, "detalle_pedido")
    r = c.post("/pedidos", json=pedido_valido(), headers=USER)
    assert r.status_code == 201
    assert contar(app, "pedidos") == antes_p + 1 and contar(app, "detalle_pedido") == antes_d + 1
    oid = r.get_json()["order_id"]
    assert c.get(f"/pedidos/{oid}", headers=USER).get_json()["total_ventas"] == 150.0


def test_rollback_si_una_linea_falla_no_queda_pedido_fantasma(app, c):
    """Salta la validación de la app para probar la ÚLTIMA barrera: la base de datos."""
    antes_p, antes_d = contar(app, "pedidos"), contar(app, "detalle_pedido")
    conn = sqlite3.connect(app.config["DB_PATH"], isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO pedidos (order_id, order_date, ship_date, ship_mode, customer_id) VALUES ('FANTASMA-1','2026-10-01','2026-10-05','Same Day','CG-12520')")
        conn.execute("INSERT INTO detalle_pedido (order_id, product_id, product_name, sales, quantity) "
                     "VALUES ('FANTASMA-1','FUR-BO-10001798','Bush Somerset Collection Bookcase',100,-5)")
        conn.execute("COMMIT")
        pytest.fail("la base debió rechazar quantity = -5")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
    conn.close()
    assert contar(app, "pedidos") == antes_p and contar(app, "detalle_pedido") == antes_d


def test_pedido_con_producto_inexistente_no_guarda_nada(app, c):
    antes_p, antes_d = contar(app, "pedidos"), contar(app, "detalle_pedido")
    malo = pedido_valido()
    malo["items"].append({"product_id": "XXX-00", "product_name": "No existe", "sales": 10, "quantity": 1})
    r = c.post("/pedidos", json=malo, headers=USER)
    assert r.status_code == 422
    assert contar(app, "pedidos") == antes_p and contar(app, "detalle_pedido") == antes_d


def test_pedido_cliente_inexistente(c):
    assert c.post("/pedidos", json=pedido_valido(customer_id="NADIE-1"), headers=USER).status_code == 422


def test_order_id_repetido_409_y_sin_cambios(app, c):
    c.post("/pedidos", json=pedido_valido(order_id="REPE-1"), headers=USER)
    antes_d = contar(app, "detalle_pedido")
    r = c.post("/pedidos", json=pedido_valido(order_id="REPE-1"), headers=USER)
    assert r.status_code == 409 and contar(app, "detalle_pedido") == antes_d


@pytest.mark.parametrize("cambio", [
    {"modo_envio": "Dron"},
    {"fecha_pedido": "01/10/2026"},
    {"fecha_envio": "2026-09-01"},               # antes de la fecha del pedido
    {"items": []},
    {"items": [dict(PRODUCTO, sales=10, quantity=-5)]},
    {"items": [dict(PRODUCTO, sales=10, quantity=1.5)]},
    {"items": [dict(PRODUCTO, sales=0, quantity=1)]},
    {"items": [dict(PRODUCTO, sales=10, quantity=True)]},
    {"items": [dict(PRODUCTO, sales="10", quantity=1)]},
    {"items": [dict(PRODUCTO, sales=10, quantity=1, discount=1.5)]},
])
def test_validaciones_de_la_app(c, cambio):
    r = c.post("/pedidos", json=pedido_valido(**cambio), headers=USER)
    assert r.status_code == 422 and r.get_json()["detalles"]


def test_cuerpo_no_json(c):
    assert c.post("/pedidos", data="hola", headers=USER).status_code == 400


# ----------------------------------------------------------- sección de administradores (catálogo)
ADM = {"nombre": "Admin Uno", "email": "admin1@example.com", "password": "clave-admin-123",
       "codigo": "clave-admin", "autorizacion_datos": True}


@pytest.fixture()
def adm(tmp_path):
    """App nueva (base limpia) para no mezclar estos cambios con las otras pruebas."""
    app = create_app({"CARGAR_DATASET": True, "DB_PATH": str(tmp_path / "adm.db"), "API_KEY": "clave-usuario", "ADMIN_KEY": "clave-admin",
                      "TESTING": True})
    return app, app.test_client()


def admin_con_sesion(cli):
    r = cli.post("/admin/registro", json=ADM)
    assert r.status_code == 201
    return cli


def test_pagina_de_catalogo_es_publica_pero_la_api_no(adm):
    _, cli = adm
    assert cli.get("/admin/catalogo").status_code == 200
    for ruta in ("/admin/catalogo/datos",):
        assert cli.get(ruta).status_code == 401                       # sin sesión
    for ruta in ("/admin/catalogo/categorias", "/admin/catalogo/subcategorias", "/admin/catalogo/productos"):
        assert cli.post(ruta, json={}).status_code == 401
    # la clave de API tampoco sirve: aquí se exige una cuenta de administrador con sesión
    assert cli.post("/admin/catalogo/categorias", json={"nombre": "X"}, headers=ADMIN).status_code == 401


def test_registro_de_admin_exige_el_codigo_correcto(adm):
    _, cli = adm
    r = cli.post("/admin/registro", json=dict(ADM, codigo="adivinando"))
    assert r.status_code == 403
    assert cli.get("/admin/yo").get_json()["admin"] is False
    assert cli.post("/admin/registro", json=dict(ADM, autorizacion_datos=False)).status_code == 422
    assert cli.post("/admin/registro", json=ADM).status_code == 201
    yo = cli.get("/admin/yo").get_json()
    assert yo["admin"] is True and yo["cuenta"]["email"] == "admin1@example.com"
    assert cli.post("/admin/registro", json=ADM).status_code == 409   # correo repetido


def test_codigo_de_admin_se_bloquea_tras_5_intentos(adm):
    _, cli = adm
    for _ in range(5):
        assert cli.post("/admin/registro", json=dict(ADM, codigo="mal")).status_code == 403
    assert cli.post("/admin/registro", json=ADM).status_code == 429   # ni con el código correcto, hasta esperar


def test_cuenta_de_cliente_no_puede_administrar_ni_entrar_como_admin(adm):
    app, cli = adm
    assert cli.post("/cuenta/registro", json=dict(CUENTA, email="cliente@example.com")).status_code == 201
    yo = cli.get("/admin/yo").get_json()
    assert yo["sesion"] is False and yo["admin"] is False
    assert cli.get("/admin/catalogo/datos").status_code == 403
    assert cli.post("/admin/catalogo/categorias", json={"nombre": "Hack"}).status_code == 403
    # entrar por la puerta de admin con una cuenta de cliente: mismo error genérico que una clave mala
    otro = app.test_client()
    r = otro.post("/admin/entrar", json={"email": "cliente@example.com", "password": CUENTA["password"]})
    assert r.status_code == 401 and r.get_json()["error"] == "Correo o contraseña incorrectos"


def test_admin_entra_con_su_cuenta_y_sale(adm):
    app, cli = adm
    admin_con_sesion(cli)
    nuevo = app.test_client()
    assert nuevo.post("/admin/entrar", json={"email": ADM["email"], "password": "mala-clave-1"}).status_code == 401
    assert nuevo.post("/admin/entrar", json={"email": ADM["email"], "password": ADM["password"]}).status_code == 200
    assert nuevo.get("/admin/catalogo/datos").status_code == 200
    nuevo.post("/admin/salir")
    assert nuevo.get("/admin/catalogo/datos").status_code == 401


def test_el_dataset_original_queda_como_categorias(adm):
    _, cli = adm
    admin_con_sesion(cli)
    d = cli.get("/admin/catalogo/datos").get_json()
    assert {c["categoria"] for c in d["arbol"]} == {"Furniture", "Office Supplies", "Technology"}
    assert d["totales"]["subcategorias"] == 17 and d["totales"]["creados"] == 0
    tec = next(c for c in d["arbol"] if c["categoria"] == "Technology")
    assert tec["tono"] == "tec" and {s["nombre"]: s["icono"] for s in tec["subcategorias"]}["Phones"] == "phone"


def test_crear_categoria_subcategoria_y_producto_aparecen_en_la_tienda(adm):
    app, cli = adm
    admin_con_sesion(cli)
    antes = cli.get("/tienda/resumen").get_json()
    assert cli.post("/admin/catalogo/categorias", json={"nombre": "Deportes", "tono": "nat"}).status_code == 201
    # categoría vacía: ya se ve en la tienda, con 0 productos
    tienda = {c["categoria"]: c for c in cli.get("/tienda/categorias").get_json()}
    assert tienda["Deportes"]["productos"] == 0 and tienda["Deportes"]["tono"] == "nat"
    assert cli.post("/admin/catalogo/subcategorias", json={"categoria": "Deportes", "nombre": "Balones", "icono": "box"}).status_code == 201
    r = cli.post("/admin/catalogo/productos", json={"nombre": "Balón de fútbol N.º 5", "categoria": "Deportes",
                                                    "subcategoria": "Balones", "precio": 25.5, "stock": 10})
    assert r.status_code == 201 and r.get_json()["product_id"].startswith("ME-P-")
    # producto nuevo (sin ventas) visible en la tienda pública, con su precio, color e ícono
    p = cli.get("/tienda/productos?q=bal%C3%B3n").get_json()["resultados"]
    assert len(p) == 1 and p[0]["precio"] == 25.5 and p[0]["vendidos"] == 0
    assert p[0]["tono"] == "nat" and p[0]["icono"] == "box" and p[0]["descuento_max"] == 0
    # "recientes" lo pone de primero; el total del catálogo subió en 1
    assert cli.get("/tienda/productos?orden=recientes&limite=1").get_json()["resultados"][0]["nombre"].startswith("Balón")
    despues = cli.get("/tienda/resumen").get_json()
    assert despues["productos"] == antes["productos"] + 1 and despues["categorias"] == antes["categorias"] + 1
    assert cli.get("/tienda/productos?categoria=Deportes").get_json()["total"] == 1
    # y se puede registrar un pedido con él (la clave de API sigue funcionando para el panel de gestión)
    ped = {"customer_id": "CG-12520", "modo_envio": "Standard Class", "fecha_pedido": "2026-10-01",
           "items": [{"product_id": r.get_json()["product_id"], "product_name": "Balón de fútbol N.º 5",
                      "sales": 25.5, "quantity": 1, "discount": 0.0, "profit": 5.0}]}
    assert cli.post("/pedidos", json=ped, headers=USER).status_code == 201
    vendido = cli.get("/tienda/productos?q=bal%C3%B3n").get_json()["resultados"][0]
    assert vendido["vendidos"] == 1 and vendido["precio"] == 25.5     # el precio propio manda sobre el deducido


def test_catalogo_validaciones_y_duplicados(adm):
    _, cli = adm
    admin_con_sesion(cli)
    cat = "/admin/catalogo/categorias"
    assert cli.post(cat, json={"nombre": ""}).status_code == 422
    assert cli.post(cat, json={"nombre": "A"}).status_code == 422
    assert cli.post(cat, json={"nombre": "Hogar", "tono": "rojo-fuego"}).status_code == 422
    assert cli.post(cat, json={"nombre": "Hogar", "tono": "mar"}).status_code == 201
    assert cli.post(cat, json={"nombre": "hogar"}).status_code == 409           # mayúsculas no cuentan
    assert cli.post(cat, json={"nombre": "furniture"}).status_code == 409       # ni choca con las del dataset
    sub = "/admin/catalogo/subcategorias"
    assert cli.post(sub, json={"categoria": "NoExiste", "nombre": "Algo"}).status_code == 422
    assert cli.post(sub, json={"categoria": "Hogar", "nombre": "Cocina", "icono": "dragon"}).status_code == 422
    assert cli.post(sub, json={"categoria": "Hogar", "nombre": "Cocina", "icono": "lamp"}).status_code == 201
    assert cli.post(sub, json={"categoria": "Hogar", "nombre": "cocina"}).status_code == 409
    prod = "/admin/catalogo/productos"
    base = {"nombre": "Licuadora", "categoria": "Hogar", "subcategoria": "Cocina", "precio": 40, "stock": 3}
    for malo in ({"precio": 0}, {"precio": -5}, {"precio": "10"}, {"precio": True}, {"precio": 10**9},
                 {"nombre": ""}, {"subcategoria": "Baño"}, {"categoria": "Furniture"},
                 {"stock": None}, {"stock": -1}, {"stock": 1.5}, {"stock": "5"}, {"stock": True}, {"stock": 10**7}):
        assert cli.post(prod, json=dict(base, **malo)).status_code == 422, malo
    assert cli.post(prod, json=base).status_code == 201
    assert cli.post(prod, json=dict(base, nombre="LICUADORA")).status_code == 409
    assert cli.post(prod, data="no es json", content_type="text/plain").status_code == 400


def test_catalogo_queda_auditado_y_el_texto_no_rompe_la_pagina(adm):
    app, cli = adm
    admin_con_sesion(cli)
    cli.post("/admin/catalogo/categorias", json={"nombre": "<b>X</b>", "tono": "ofi"})
    aud = cli.get("/admin/auditoria", headers=ADMIN).get_json()
    detalles = " | ".join(a["detalle"] for a in aud)
    assert "Nueva cuenta de administrador: admin1@example.com" in detalles
    assert "Categoría creada" in detalles and all(a["nivel"] == "admin" for a in aud)


def test_base_vieja_se_actualiza_sin_perder_datos(tmp_path):
    """Una base creada antes de esta versión (sin precio, rol ni categorías) se migra al arrancar."""
    ruta = tmp_path / "vieja.db"
    create_app({"CARGAR_DATASET": True, "DB_PATH": str(ruta), "API_KEY": "a", "ADMIN_KEY": "b"})         # crea la base completa
    conn = sqlite3.connect(ruta)
    conn.executescript("""
        DROP TABLE subcategorias; DROP TABLE categorias;
        ALTER TABLE productos DROP COLUMN precio; ALTER TABLE productos DROP COLUMN activo;
        ALTER TABLE cuentas DROP COLUMN rol;
        ALTER TABLE cuentas DROP COLUMN apellido; ALTER TABLE cuentas DROP COLUMN telefono;
        ALTER TABLE cuentas DROP COLUMN pais; ALTER TABLE cuentas DROP COLUMN region;
        ALTER TABLE cuentas DROP COLUMN ciudad; ALTER TABLE cuentas DROP COLUMN direccion;
        ALTER TABLE cuentas DROP COLUMN codigo_postal;""")
    conn.commit(); conn.close()
    app = create_app({"CARGAR_DATASET": True, "DB_PATH": str(ruta), "API_KEY": "a", "ADMIN_KEY": "b", "TESTING": True})
    cli = app.test_client()
    assert len(cli.get("/tienda/categorias").get_json()) == 3
    assert cli.get("/tienda/productos?limite=1").get_json()["total"] > 1000      # todos quedan activos
    assert cli.post("/admin/registro", json=dict(ADM, codigo="b")).status_code == 201
    assert cli.post("/cuenta/registro", json=dict(CUENTA, email="nueva@example.com")).status_code == 201   # ya existen las columnas del perfil
    assert cli.get("/cuenta/yo").get_json()["cuenta"]["direccion"]["ciudad"] == "Montería"


# ----------------------------------------------------------- perfil de la cuenta: teléfono y dirección de envío
def test_ubicaciones_son_publicas_y_traen_cordoba_y_monteria(c):
    d = c.get("/tienda/ubicaciones").get_json()          # sin clave ni sesión
    paises = {p["pais"]: p for p in d}
    assert d[0]["pais"] == "Colombia" and "Estados Unidos" in paises and "México" in paises
    regiones = {r["nombre"]: r["ciudades"] for r in paises["Colombia"]["regiones"]}
    assert len(regiones) == 33 and "Montería" in regiones["Córdoba"] and regiones["Bogotá D.C."] == ["Bogotá"]
    assert paises["México"]["regiones"] == []             # región y ciudad escritas a mano


def test_registro_guarda_telefono_y_direccion_y_yo_la_devuelve(app, c):
    r = c.post("/cuenta/registro", json=dict(CUENTA, email="perfil@example.com"))
    assert r.status_code == 201
    yo = c.get("/cuenta/yo").get_json()["cuenta"]
    assert yo["telefono"] == "+57 300 123 4567" and yo["apellido"] == "Pérez"
    assert yo["direccion"] == {"pais": "Colombia", "region": "Córdoba", "ciudad": "Montería",
                               "direccion": "Calle 27 # 5-30, Barrio Centro", "codigo_postal": "230001"}
    assert "password" not in str(yo) and "hash" not in str(yo)
    conn = sqlite3.connect(app.config["DB_PATH"])
    fila = conn.execute("SELECT telefono, ciudad, codigo_postal FROM cuentas WHERE email='perfil@example.com'").fetchone()
    conn.close()
    assert fila == ("+57 300 123 4567", "Montería", "230001")


@pytest.mark.parametrize("cambio, campo", [
    ({"apellido": ""}, "apellido"), ({"apellido": "P"}, "apellido"),
    ({"telefono": ""}, "telefono"), ({"telefono": "abc"}, "telefono"), ({"telefono": "12345"}, "telefono"),
    ({"pais": "Narnia"}, "pais"), ({"pais": ""}, "pais"),
    ({"region": "Atlantis"}, "region"), ({"region": "Texas"}, "region"),     # Texas no es de Colombia
    ({"ciudad": ""}, "ciudad"), ({"direccion": "x"}, "direccion"),
    ({"codigo_postal": "123"}, "codigo_postal"), ({"codigo_postal": "ABCDEF"}, "codigo_postal"),
])
def test_registro_exige_perfil_completo_y_valido(c, cambio, campo):
    r = c.post("/cuenta/registro", json=dict(CUENTA, email="malo@example.com", **cambio))
    assert r.status_code == 422
    assert any(d.startswith(campo) for d in r.get_json()["detalles"])


def test_registro_sin_los_campos_nuevos_ya_no_se_acepta(c):
    minimo = {"nombre": "Ana", "email": "viejo@example.com", "password": "clave-segura-1", "autorizacion_datos": True}
    r = c.post("/cuenta/registro", json=minimo)
    assert r.status_code == 422 and len(r.get_json()["detalles"]) >= 5


def test_codigo_postal_se_valida_segun_el_pais(c):
    usa = dict(CUENTA, email="usa@example.com", pais="Estados Unidos", region="Florida", ciudad="Miami", codigo_postal="33101")
    assert c.post("/cuenta/registro", json=usa).status_code == 201
    assert c.post("/cuenta/registro", json=dict(usa, email="usa2@example.com", codigo_postal="33101-1234")).status_code == 201
    assert c.post("/cuenta/registro", json=dict(usa, email="usa3@example.com", codigo_postal="230001")).status_code == 422
    mx = dict(CUENTA, email="mx@example.com", pais="México", region="Jalisco", ciudad="Guadalajara", codigo_postal="44100")
    assert c.post("/cuenta/registro", json=mx).status_code == 201     # país con región escrita a mano


def test_cambiar_direccion_exige_sesion_y_guarda_solo_en_la_propia_cuenta(app, c):
    nueva = {"pais": "Colombia", "region": "Antioquia", "ciudad": "Medellín", "direccion": "Carrera 43A # 1-50, Of. 301",
             "codigo_postal": "050021", "telefono": "3109876543"}
    assert app.test_client().put("/cuenta/direccion", json=nueva).status_code == 401           # sin sesión
    c.post("/cuenta/registro", json=dict(CUENTA, email="dir1@example.com"))
    otro = app.test_client()
    otro.post("/cuenta/registro", json=dict(CUENTA, email="dir2@example.com"))
    assert c.put("/cuenta/direccion", json=dict(nueva, region="Texas")).status_code == 422
    r = c.put("/cuenta/direccion", json=nueva)
    assert r.status_code == 200 and r.get_json()["direccion"]["ciudad"] == "Medellín" and r.get_json()["telefono"] == "3109876543"
    assert c.get("/cuenta/yo").get_json()["cuenta"]["direccion"]["region"] == "Antioquia"
    assert otro.get("/cuenta/yo").get_json()["cuenta"]["direccion"]["ciudad"] == "Montería"   # la otra cuenta no cambió
    assert c.put("/cuenta/direccion", data="no es json", content_type="text/plain").status_code == 400


def test_entrar_devuelve_el_perfil_con_la_direccion(app):
    cli = app.test_client()
    cli.post("/cuenta/registro", json=dict(CUENTA, email="login@example.com"))
    nuevo = app.test_client()
    r = nuevo.post("/cuenta/entrar", json={"email": "login@example.com", "password": CUENTA["password"]})
    assert r.status_code == 200 and r.get_json()["direccion"]["ciudad"] == "Montería" and "password_hash" not in r.get_json()


# ----------------------------------------------------------- CRUD de productos (leer, editar, borrar)
RUTA_PROD = "/admin/catalogo/productos"


def producto_nuevo(cli, nombre="Balón de fútbol", precio=25.5, stock=10):
    """Crea (como admin) una categoría, subcategoría y producto de prueba; devuelve el product_id."""
    cli.post("/admin/catalogo/categorias", json={"nombre": "Deportes", "tono": "nat"})
    cli.post("/admin/catalogo/subcategorias", json={"categoria": "Deportes", "nombre": "Balones", "icono": "box"})
    r = cli.post(RUTA_PROD, json={"nombre": nombre, "categoria": "Deportes", "subcategoria": "Balones", "precio": precio, "stock": stock})
    assert r.status_code == 201
    return r.get_json()["product_id"]


def test_crud_solo_para_administradores(adm):
    _, cli = adm
    pid = "FUR-BO-10001798"
    rutas = [("get", RUTA_PROD), ("get", f"{RUTA_PROD}/{pid}"), ("put", f"{RUTA_PROD}/{pid}"),
             ("patch", f"{RUTA_PROD}/{pid}"), ("delete", f"{RUTA_PROD}/{pid}")]
    for metodo, ruta in rutas:                                   # sin sesión
        assert getattr(cli, metodo)(ruta, json={"precio": 5}).status_code == 401, (metodo, ruta)
    cli.post("/cuenta/registro", json=dict(CUENTA, email="cliente-crud@example.com"))
    for metodo, ruta in rutas:                                   # con sesión de cliente
        assert getattr(cli, metodo)(ruta, json={"precio": 5}).status_code == 403, (metodo, ruta)
    # la clave de API tampoco sirve: estas rutas piden sesión de administrador
    assert cli.get(RUTA_PROD, headers=ADMIN).status_code == 403
    assert cli.get("/tienda/productos?q=Bookcase").get_json()["total"] > 0     # y nada cambió


def test_listar_productos_con_filtros_orden_y_paginacion(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    d = cli.get(RUTA_PROD + "?limite=5").get_json()
    assert d["total"] > 1000 and len(d["resultados"]) == 5
    assert d["resultados"][0]["product_id"] == pid                           # "recientes" por defecto
    esperado = {"product_id": pid, "nombre": "Balón de fútbol", "categoria": "Deportes", "subcategoria": "Balones",
                "precio": 25.5, "precio_propio": True, "activo": True, "stock": 10, "vendidos": 0, "con_ventas": False}
    assert {k: d["resultados"][0][k] for k in esperado} == esperado
    viejo = cli.get(RUTA_PROD + "?q=Bookcase&limite=1").get_json()["resultados"][0]
    assert viejo["precio_propio"] is False and viejo["precio"] > 0 and viejo["con_ventas"] is True   # precio deducido
    assert cli.get(RUTA_PROD + "?categoria=Deportes").get_json()["total"] == 1
    assert cli.get(RUTA_PROD + "?estado=ocultos").get_json()["total"] == 0
    p2 = cli.get(RUTA_PROD + "?limite=5&pagina=2").get_json()["resultados"]
    assert p2 and p2[0]["product_id"] != d["resultados"][0]["product_id"]
    caros = cli.get(RUTA_PROD + "?orden=precio_desc&limite=3").get_json()["resultados"]
    assert caros[0]["precio"] >= caros[1]["precio"] >= caros[2]["precio"]
    assert cli.get(RUTA_PROD + "?estado=raro").status_code == 400
    assert cli.get(RUTA_PROD + "?orden=raro").status_code == 400
    assert cli.get(RUTA_PROD + "?limite=0").status_code == 400
    assert cli.get(RUTA_PROD + "?q=%25_").get_json()["total"] == 0           # comodines tratados como texto
    uno = cli.get(f"{RUTA_PROD}/{pid}").get_json()
    assert uno["nombre"] == "Balón de fútbol" and cli.get(f"{RUTA_PROD}/NO-EXISTE").status_code == 404


def test_editar_producto_nombre_categoria_y_precio(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    cli.post("/admin/catalogo/subcategorias", json={"categoria": "Deportes", "nombre": "Raquetas", "icono": "mouse"})
    r = cli.put(f"{RUTA_PROD}/{pid}", json={"precio": 30})                    # solo el precio
    d = r.get_json()
    assert r.status_code == 200 and d["precio"] == 30 and d["nombre"] == "Balón de fútbol"
    r = cli.patch(f"{RUTA_PROD}/{pid}", json={"nombre": "Balón de microfútbol", "subcategoria": "Raquetas"})
    d = r.get_json()
    assert r.status_code == 200 and d["nombre"] == "Balón de microfútbol" and d["subcategoria"] == "Raquetas"
    assert d["icono"] == "mouse" and d["precio"] == 30                        # lo demás no se toca
    tienda = cli.get("/tienda/productos?categoria=Deportes").get_json()["resultados"]
    assert [(p["nombre"], p["precio"], p["icono"]) for p in tienda] == [("Balón de microfútbol", 30, "mouse")]
    # mover a otra categoría existente (del dataset) exige elegir también su subcategoría
    assert cli.put(f"{RUTA_PROD}/{pid}", json={"categoria": "Technology"}).status_code == 422
    r = cli.put(f"{RUTA_PROD}/{pid}", json={"categoria": "Technology", "subcategoria": "Phones"})
    assert r.status_code == 200 and r.get_json()["categoria"] == "Technology"
    assert cli.get("/tienda/productos?categoria=Deportes").get_json()["total"] == 0
    sin_cambios = cli.put(f"{RUTA_PROD}/{pid}", json={"precio": 30}).get_json()
    assert sin_cambios["mensaje"] == "No había cambios que guardar"


@pytest.mark.parametrize("malo", [
    {}, {"nombre": ""}, {"nombre": "A"}, {"nombre": 5}, {"nombre": "x" * 201},
    {"precio": 0}, {"precio": -1}, {"precio": "9"}, {"precio": True}, {"precio": None}, {"precio": 10**9},
    {"activo": "si"}, {"activo": 1}, {"categoria": ""}, {"stock": -1}, {"stock": 2.5}, {"stock": True}, {"stock": "3"},
    {"ajuste_stock": 0}, {"ajuste_stock": -11}, {"ajuste_stock": 1.5}, {"ajuste_stock": "2"}, {"ajuste_stock": False},
    {"stock": 5, "ajuste_stock": 1}, {"ajuste_stock": 10**7}, {"subcategoria": "No existe"},
    {"categoria": "No existe", "subcategoria": "Balones"}, {"color": "rojo"}, {"precio": 5, "id": "otro"}])
def test_editar_producto_rechaza_datos_invalidos(adm, malo):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    assert cli.put(f"{RUTA_PROD}/{pid}", json=malo).status_code == 422, malo
    despues = cli.get(f"{RUTA_PROD}/{pid}").get_json()                       # no se guardó nada
    assert (despues["nombre"], despues["precio"], despues["categoria"], despues["activo"]) == ("Balón de fútbol", 25.5, "Deportes", True)


def test_editar_producto_cuerpo_no_json_y_duplicados(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    otro = cli.post(RUTA_PROD, json={"nombre": "Balón de baloncesto", "categoria": "Deportes",
                                     "subcategoria": "Balones", "precio": 40, "stock": 4}).get_json()["product_id"]
    assert cli.put(f"{RUTA_PROD}/{pid}", data="no es json", content_type="text/plain").status_code == 400
    assert cli.put(f"{RUTA_PROD}/{pid}", json={"nombre": "BALÓN DE BALONCESTO"}).status_code == 409
    assert cli.put(f"{RUTA_PROD}/{pid}", json={"nombre": "balón de fútbol"}).status_code == 200   # su propio nombre, otra mayúscula
    assert cli.get(f"{RUTA_PROD}/{otro}").get_json()["nombre"] == "Balón de baloncesto"


def test_id_repetido_del_dataset_exige_indicar_el_nombre(adm):
    _, cli = adm
    admin_con_sesion(cli)
    conn = sqlite3.connect(adm[0].config["DB_PATH"])
    nombres = [f[0] for f in conn.execute("SELECT product_name FROM productos WHERE product_id = 'FUR-FU-10004270' ORDER BY product_name")]
    conn.close()
    assert len(nombres) == 2
    ruta = f"{RUTA_PROD}/FUR-FU-10004270"
    assert cli.get(ruta).status_code == 422 and cli.put(ruta, json={"precio": 9}).status_code == 422
    assert cli.delete(ruta).status_code == 422
    r = cli.put(ruta, query_string={"nombre_actual": nombres[0]}, json={"precio": 9.99})
    assert r.status_code == 200 and r.get_json()["nombre"] == nombres[0] and r.get_json()["precio"] == 9.99
    otro = cli.get(ruta, query_string={"nombre_actual": nombres[1]}).get_json()
    assert otro["nombre"] == nombres[1] and otro["precio_propio"] is False          # el otro producto no cambió
    assert cli.get(ruta, query_string={"nombre_actual": "no es este"}).status_code == 404


def test_renombrar_producto_con_ventas_mantiene_el_historial_consistente(adm):
    app, cli = adm
    admin_con_sesion(cli)
    antes = cli.get(f"/pedidos/{_pedido_del_dataset(app, PRODUCTO)}", headers=USER).get_json()
    r = cli.put(f"{RUTA_PROD}/{PRODUCTO['product_id']}", json={"nombre": "Biblioteca Bush Somerset"})
    d = r.get_json()
    assert r.status_code == 200 and d["lineas_de_pedido_actualizadas"] >= 1 and d["con_ventas"] is True
    conn = sqlite3.connect(app.config["DB_PATH"])
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []                  # ninguna línea quedó huérfana
    assert conn.execute("SELECT COUNT(*) FROM detalle_pedido WHERE product_name = ?", (PRODUCTO["product_name"],)).fetchone()[0] == 0
    conn.close()
    despues = cli.get(f"/pedidos/{antes['order_id']}", headers=USER).get_json()
    nombres = {i["producto"] for i in despues["items"]}
    assert "Biblioteca Bush Somerset" in nombres and despues["total_ventas"] == antes["total_ventas"]
    # y se puede seguir vendiendo con el nombre nuevo, pero no con el viejo
    ped = pedido_valido(items=[{"product_id": PRODUCTO["product_id"], "product_name": "Biblioteca Bush Somerset",
                                "sales": 10, "quantity": 1, "discount": 0, "profit": 1}])
    assert cli.post("/pedidos", json=ped, headers=USER).status_code == 201
    assert cli.post("/pedidos", json=pedido_valido(), headers=USER).status_code == 422


def _pedido_del_dataset(app, producto):
    conn = sqlite3.connect(app.config["DB_PATH"])
    fila = conn.execute("SELECT order_id FROM detalle_pedido WHERE product_id = ? AND product_name = ? LIMIT 1",
                        (producto["product_id"], producto["product_name"])).fetchone()
    conn.close()
    return fila[0]


def test_borrar_producto_sin_ventas_lo_elimina_de_verdad(adm):
    app, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    antes = cli.get("/tienda/resumen").get_json()["productos"]
    r = cli.delete(f"{RUTA_PROD}/{pid}")
    assert r.status_code == 200 and r.get_json()["resultado"] == "eliminado"
    assert cli.get(f"{RUTA_PROD}/{pid}").status_code == 404
    assert cli.get("/tienda/productos?q=bal%C3%B3n").get_json()["total"] == 0
    assert cli.get("/tienda/resumen").get_json()["productos"] == antes - 1
    assert cli.delete(f"{RUTA_PROD}/{pid}").status_code == 404                          # ya no existe


def test_borrar_producto_con_ventas_lo_oculta_y_se_puede_volver_a_mostrar(adm):
    app, cli = adm
    admin_con_sesion(cli)
    pid, nombre = PRODUCTO["product_id"], PRODUCTO["product_name"]
    total = cli.get("/tienda/productos?limite=1").get_json()["total"]
    cat = next(c for c in cli.get("/tienda/categorias").get_json() if c["categoria"] == "Furniture")["productos"]
    r = cli.delete(f"{RUTA_PROD}/{pid}")
    assert r.status_code == 200 and r.get_json()["resultado"] == "oculto"
    assert cli.get(f"{RUTA_PROD}/{pid}").get_json()["activo"] is False                   # sigue existiendo
    assert cli.get("/tienda/productos?limite=1").get_json()["total"] == total - 1        # pero la tienda no lo muestra
    assert cli.get("/tienda/productos?q=Somerset%20Collection%20Bookcase").get_json()["total"] == 0
    assert next(c for c in cli.get("/tienda/categorias").get_json() if c["categoria"] == "Furniture")["productos"] == cat - 1
    assert cli.get(RUTA_PROD + "?estado=ocultos").get_json()["total"] == 1
    assert cli.get(RUTA_PROD + "?estado=activos&q=Somerset").get_json()["total"] == 0
    assert cli.get("/admin/catalogo/datos").get_json()["totales"]["ocultos"] == 1
    assert cli.post("/pedidos", json=pedido_valido(), headers=USER).status_code == 422   # no se puede vender oculto
    assert contar(app, "pedidos") > 0 and cli.get(f"/pedidos/{_pedido_del_dataset(app, PRODUCTO)}", headers=USER).status_code == 200
    r = cli.put(f"{RUTA_PROD}/{pid}", query_string={"nombre_actual": nombre}, json={"activo": True})
    assert r.status_code == 200 and r.get_json()["activo"] is True
    assert cli.get("/tienda/productos?limite=1").get_json()["total"] == total
    assert cli.post("/pedidos", json=pedido_valido(), headers=USER).status_code == 201


def test_ocultar_y_mostrar_con_activo(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    assert cli.put(f"{RUTA_PROD}/{pid}", json={"activo": False}).get_json()["activo"] is False
    assert cli.get("/tienda/productos?categoria=Deportes").get_json()["total"] == 0
    assert cli.get(RUTA_PROD + "?categoria=Deportes").get_json()["total"] == 1             # el admin sí lo ve
    assert cli.put(f"{RUTA_PROD}/{pid}", json={"activo": True}).get_json()["activo"] is True
    assert cli.get("/tienda/productos?categoria=Deportes").get_json()["total"] == 1


def test_crud_queda_auditado(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli)
    cli.put(f"{RUTA_PROD}/{pid}", json={"precio": 99})
    cli.delete(f"{RUTA_PROD}/{pid}")
    cli.delete(f"{RUTA_PROD}/{PRODUCTO['product_id']}")
    detalles = " | ".join(a["detalle"] for a in cli.get("/admin/auditoria", headers=ADMIN).get_json())
    assert "Producto editado" in detalles and "precio: 25.5 → 99.0" in detalles and "admin1@example.com" in detalles
    assert "Producto eliminado" in detalles and "Producto ocultado (tiene ventas)" in detalles


# ----------------------------------------------------------- stock (cantidad disponible)
def test_crear_producto_exige_el_stock(adm):
    _, cli = adm
    admin_con_sesion(cli)
    cli.post("/admin/catalogo/categorias", json={"nombre": "Deportes", "tono": "nat"})
    cli.post("/admin/catalogo/subcategorias", json={"categoria": "Deportes", "nombre": "Balones", "icono": "box"})
    base = {"nombre": "Balón", "categoria": "Deportes", "subcategoria": "Balones", "precio": 10}
    assert cli.post(RUTA_PROD, json=base).status_code == 422                                  # sin stock
    for malo in (None, -1, 1.5, "5", True, 10**7):
        assert cli.post(RUTA_PROD, json=dict(base, stock=malo)).status_code == 422, malo
    assert cli.get(RUTA_PROD + "?categoria=Deportes").get_json()["total"] == 0               # no se guardó nada
    r = cli.post(RUTA_PROD, json=dict(base, stock=0))                                         # 0 es válido (agotado)
    assert r.status_code == 201 and r.get_json()["stock"] == 0
    r = cli.post(RUTA_PROD, json=dict(base, nombre="Balón 2", stock=25))
    assert cli.get(f"{RUTA_PROD}/{r.get_json()['product_id']}").get_json()["stock"] == 25


def test_editar_stock_fijo_y_ajuste_agregar_o_quitar(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli, stock=10)
    ruta = f"{RUTA_PROD}/{pid}"
    assert cli.put(ruta, json={"ajuste_stock": 5}).get_json()["stock"] == 15                   # agregar
    assert cli.put(ruta, json={"ajuste_stock": -7}).get_json()["stock"] == 8                   # quitar
    assert cli.patch(ruta, json={"stock": 100}).get_json()["stock"] == 100                     # cantidad exacta
    assert cli.put(ruta, json={"ajuste_stock": -100}).get_json()["stock"] == 0                 # quitar todo es válido
    r = cli.put(ruta, json={"ajuste_stock": -1})                                               # no baja de 0
    assert r.status_code == 422 and "solo hay 0" in " ".join(r.get_json()["detalles"])
    assert cli.get(ruta).get_json()["stock"] == 0
    # ajustar el stock no toca lo demás, y se puede combinar con otros cambios en la misma petición
    r = cli.put(ruta, json={"ajuste_stock": 4, "precio": 33}).get_json()
    assert (r["stock"], r["precio"], r["nombre"]) == (4, 33, "Balón de fútbol")
    # un cambio que falla no deja el stock a medias
    assert cli.put(ruta, json={"ajuste_stock": 6, "nombre": ""}).status_code == 422
    assert cli.get(ruta).get_json()["stock"] == 4
    # sin cambios de stock, el stock se conserva
    assert cli.put(ruta, json={"precio": 40}).get_json()["stock"] == 4


def test_stock_se_ve_en_la_lista_y_queda_auditado(adm):
    _, cli = adm
    admin_con_sesion(cli)
    pid = producto_nuevo(cli, stock=10)
    cli.put(f"{RUTA_PROD}/{pid}", json={"ajuste_stock": -3})
    assert cli.get(RUTA_PROD + "?categoria=Deportes").get_json()["resultados"][0]["stock"] == 7
    detalles = " | ".join(a["detalle"] for a in cli.get("/admin/auditoria", headers=ADMIN).get_json())
    assert "stock inicial 10" in detalles and "stock: 10 → 7" in detalles


def test_productos_del_dataset_empiezan_con_stock_0_y_se_les_puede_cargar(adm):
    _, cli = adm
    admin_con_sesion(cli)
    p = cli.get(RUTA_PROD + "?q=Bookcase&limite=1").get_json()["resultados"][0]
    assert p["stock"] == 0
    r = cli.put(f"{RUTA_PROD}/{p['product_id']}", query_string={"nombre_actual": p["nombre"]}, json={"ajuste_stock": 20})
    assert r.status_code == 200 and r.get_json()["stock"] == 20


def test_base_vieja_sin_stock_se_migra_con_cero(tmp_path):
    ruta = tmp_path / "sin_stock.db"
    create_app({"CARGAR_DATASET": True, "DB_PATH": str(ruta), "API_KEY": "a", "ADMIN_KEY": "b"})
    conn = sqlite3.connect(ruta)
    conn.execute("ALTER TABLE productos DROP COLUMN stock")
    conn.commit(); conn.close()
    create_app({"CARGAR_DATASET": True, "DB_PATH": str(ruta), "API_KEY": "a", "ADMIN_KEY": "b"})
    conn = sqlite3.connect(ruta)
    assert conn.execute("SELECT COUNT(*), MIN(stock), MAX(stock) FROM productos").fetchone()[1:] == (0, 0)
    conn.close()


# ----------------------------------------------------------- CSRF (origen de las peticiones)
def test_csrf_rechaza_peticion_que_cambia_datos_desde_otro_sitio(c):
    r = c.post("/cuenta/entrar", json={"email": "a@b.co", "password": "x"},
               headers={"Origin": "https://sitio-malicioso.example"})
    assert r.status_code == 403
    r = c.post("/cuenta/entrar", json={"email": "a@b.co", "password": "x"},
               headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_csrf_acepta_el_mismo_sitio_y_los_clientes_sin_navegador(c):
    malo = {"email": "no-existe@b.co", "password": "x"}
    # Mismo origen (como lo envía el navegador): llega a la lógica normal (401 por credenciales)
    r = c.post("/cuenta/entrar", json=malo, headers={"Origin": "http://localhost"})
    assert r.status_code == 401
    # Sin Origin (curl, scripts): también llega a la lógica normal
    assert c.post("/cuenta/entrar", json=malo).status_code == 401
    # Las lecturas (GET) nunca se bloquean
    assert c.get("/tienda/config", headers={"Origin": "https://otro.example"}).status_code == 200


# ----------------------------------------------------------- base vacía (arranque por defecto)
def test_base_nueva_arranca_vacia_y_todo_responde_sin_errores(tmp_path):
    app = create_app({"DB_PATH": str(tmp_path / "vacia.db"), "API_KEY": "a", "ADMIN_KEY": "b", "TESTING": True})
    c = app.test_client()
    h = {"X-API-Key": "a"}
    r = c.get("/resumen", headers=h).get_json()
    assert r["clientes"] == 0 and r["pedidos"] == 0 and r["lineas_de_pedido"] == 0 and r["productos"] == 0
    for ruta in ("/clientes", "/productos", "/pedidos"):
        d = c.get(ruta, headers=h)
        assert d.status_code == 200 and d.get_json()["total"] == 0
    for ruta in ("/reportes/ventas-por-categoria", "/reportes/ganancia-por-cliente", "/reportes/envio-por-region"):
        assert c.get(ruta, headers=h).status_code == 200, ruta
    assert c.get("/tienda/productos").get_json()["total"] == 0


# ----------------------------------------------------------- modelo de colas M/M/c (valores de la teoría)
def test_colas_coincide_con_la_teoria():
    from colas import erlang_c, espera_promedio_min, recomendar_asesores, analizar
    # M/M/1 con utilización 0,5: probabilidad de esperar = 0,5 y espera promedio = 1 hora
    assert erlang_c(0.5, 1, 1) == pytest.approx(0.5)
    assert espera_promedio_min(0.5, 1, 1) == pytest.approx(60)
    # M/M/2 con llegadas = servicio = 1: probabilidad 1/3 y espera 20 minutos
    assert erlang_c(1, 1, 2) == pytest.approx(1 / 3)
    assert espera_promedio_min(1, 1, 2) == pytest.approx(20)
    # Si llegan más rápido de lo que se atiende, la fila crece sin límite
    assert erlang_c(2, 1, 2) is None and espera_promedio_min(3, 1, 2) is None
    # 30 llegadas/h y 20 atenciones/h por asesor: con 1 asesor es imposible, con 2 la espera es ~3,9 min
    assert recomendar_asesores(30, 20, 5) == 2
    assert recomendar_asesores(30, 20, 3) == 3
    # Sin llegadas no hay espera
    assert espera_promedio_min(0, 20, 1) == 0
    a = analizar(30, 180, 1, 5, 4)                       # 30/h, 3 min por cliente, 1 asesor
    assert a["estable"] is False and a["asesores_recomendados"] == 2 and a["espera_actual_min"] == 12


# ----------------------------------------------------------- turnos y renombrar categorías (sección admin)
@pytest.fixture()
def tur(tmp_path):
    app = create_app({"DB_PATH": str(tmp_path / "t.db"), "API_KEY": "u", "ADMIN_KEY": "clave-admin",
                      "TESTING": True, "TURNOS_MAX_POR_IP": 3, "TURNOS_MAX_FILA": 4})
    return app, app.test_client()


def test_turnos_estado_publico_y_vacio(tur):
    _, c = tur
    r = c.get("/turnos/api/estado")
    assert r.status_code == 200
    servicios = r.get_json()
    assert len(servicios) == 2 and all(s["en_espera"] == 0 and s["asesores"] == 1 for s in servicios)


@pytest.mark.parametrize("cuerpo", [{}, {"servicio_id": 999}, {"servicio_id": [1]}, {"servicio_id": "1"},
                                    {"servicio_id": True}, {"servicio_id": -3}, {"servicio_id": 1.5}])
def test_pedir_turno_rechaza_servicios_invalidos(tur, cuerpo):
    _, c = tur
    r = c.post("/turnos/api/pedir", json=cuerpo)
    assert r.status_code in (404, 422)
    assert c.get("/turnos/api/estado").get_json()[0]["en_espera"] == 0     # no quedó ningún turno fantasma


def test_pedir_turno_numera_en_orden_y_limita_el_abuso(tur):
    _, c = tur
    assert [c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).get_json()["numero"] for _ in range(3)] == [1, 2, 3]
    r = c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"})                 # 4.º seguido desde el mismo equipo
    assert r.status_code == 429
    assert c.get("/turnos/api/estado").get_json()[0]["en_espera"] == 3


def test_fila_llena_se_rechaza(tmp_path):
    app = create_app({"DB_PATH": str(tmp_path / "f.db"), "API_KEY": "u", "ADMIN_KEY": "k", "TESTING": True,
                      "TURNOS_MAX_POR_IP": 100, "TURNOS_MAX_FILA": 2})
    c = app.test_client()
    assert c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).status_code == 200
    assert c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).status_code == 200
    assert c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).status_code == 409
    assert c.post("/turnos/api/pedir", json={"servicio_id": 2, "motivo": "Estado de mi pedido"}).status_code == 200   # otra fila sigue libre


def test_llamar_turno_exige_admin_y_registra_atencion_y_fin(tur):
    _, c = tur
    c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"})
    c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Garantía o devolución"})
    assert c.post("/admin/turnos/api/llamar", json={"servicio_id": 1}).status_code == 401   # sin sesión
    assert c.post("/admin/registro", json=ADM).status_code == 201
    r = c.post("/admin/turnos/api/llamar", json={"servicio_id": 1})
    assert r.status_code == 200 and r.get_json()["numero"] == 1
    e = c.get("/turnos/api/estado").get_json()[0]
    assert e["turno_actual"] == 1 and e["en_espera"] == 1
    r = c.post("/admin/turnos/api/llamar", json={"servicio_id": 1})                          # termina el 1, llama al 2
    assert r.get_json()["numero"] == 2
    r = c.post("/admin/turnos/api/llamar", json={"servicio_id": 1})                          # termina el 2, fila vacía
    assert r.get_json()["numero"] is None
    assert c.get("/turnos/api/estado").get_json()[0]["turno_actual"] == "-"
    assert c.post("/admin/turnos/api/llamar", json={"servicio_id": 999}).status_code == 404


def test_estimacion_usa_llegadas_duracion_y_asesores(tur):
    app, c = tur
    import sqlite3
    from datetime import datetime, timedelta
    ahora = datetime.now()
    conn = sqlite3.connect(app.config["DB_PATH"])
    # 12 turnos terminados en la última hora, cada uno de 6 minutos de atención
    for i in range(12):
        llegada = ahora - timedelta(minutes=55 - i * 4)
        conn.execute("INSERT INTO turnos (servicio_id, numero, estado, fecha_llegada, fecha_atencion, fecha_fin) "
                     "VALUES (1, ?, 'finalizado', ?, ?, ?)",
                     (i + 1, llegada.isoformat(), llegada.isoformat(), (llegada + timedelta(minutes=6)).isoformat()))
    conn.commit(); conn.close()
    e = c.get("/turnos/api/estado").get_json()[0]
    assert e["datos_suficientes"] is True
    assert e["duracion_promedio_seg"] == 360 and e["llegadas_por_hora"] == 12
    # 12 llegadas/h y 10 atenciones/h por asesor: con 1 asesor la fila crece sin parar
    assert e["estable"] is False and e["asesores_recomendados"] >= 2 and "asesores" in e["alerta"]
    assert c.post("/admin/registro", json=ADM).status_code == 201
    r = c.put("/admin/turnos/api/servicio/1", json={"asesores": e["asesores_recomendados"], "meta_espera_min": 5})
    assert r.status_code == 200
    e2 = c.get("/turnos/api/estado").get_json()[0]
    assert e2["estable"] is True and e2["espera_modelo_min"] <= 5


@pytest.mark.parametrize("cuerpo", [{"asesores": 0}, {"asesores": 21}, {"asesores": "2"}, {"asesores": True},
                                    {"meta_espera_min": 0}, {"meta_espera_min": 61}])
def test_configurar_servicio_valida_y_exige_admin(tur, cuerpo):
    _, c = tur
    assert c.put("/admin/turnos/api/servicio/1", json={"asesores": 2}).status_code == 401
    assert c.post("/admin/registro", json=ADM).status_code == 201
    assert c.put("/admin/turnos/api/servicio/1", json=cuerpo).status_code == 422
    assert c.put("/admin/turnos/api/servicio/999", json={"asesores": 2}).status_code == 404


def test_renombrar_categoria_con_subcategorias_y_productos(tur):
    _, c = tur
    assert c.post("/admin/registro", json=ADM).status_code == 201
    assert c.post("/admin/catalogo/categorias", json={"nombre": "Muebles", "tono": "mue"}).status_code == 201
    assert c.post("/admin/catalogo/subcategorias", json={"categoria": "Muebles", "nombre": "Sillas", "icono": "chair"}).status_code == 201
    r = c.put("/admin/catalogo/categorias/Muebles", json={"nuevo_nombre": "Hogar"})
    assert r.status_code == 200, r.get_json()
    arbol = c.get("/admin/catalogo/datos").get_json()["arbol"]
    assert "Hogar" in str(arbol) and "Muebles" not in str(arbol) and "Sillas" in str(arbol)
    r = c.put("/admin/catalogo/subcategorias/Hogar/Sillas", json={"nuevo_nombre": "Sillones"})
    assert r.status_code == 200 and "Sillones" in str(c.get("/admin/catalogo/datos").get_json()["arbol"])


@pytest.mark.parametrize("cuerpo", [{}, {"nuevo_nombre": ""}, {"nuevo_nombre": "x"}, {"nuevo_nombre": 12345},
                                    {"nuevo_nombre": "x" * 300}])
def test_renombrar_categoria_valida_el_nombre(tur, cuerpo):
    _, c = tur
    assert c.post("/admin/registro", json=ADM).status_code == 201
    c.post("/admin/catalogo/categorias", json={"nombre": "Muebles", "tono": "mue"})
    assert c.put("/admin/catalogo/categorias/Muebles", json=cuerpo).status_code == 422


def test_renombrar_exige_admin_y_no_pisa_otra_categoria(tur):
    _, c = tur
    assert c.put("/admin/catalogo/categorias/Muebles", json={"nuevo_nombre": "Hogar"}).status_code == 401
    assert c.post("/admin/registro", json=ADM).status_code == 201
    c.post("/admin/catalogo/categorias", json={"nombre": "Muebles", "tono": "mue"})
    c.post("/admin/catalogo/categorias", json={"nombre": "Hogar", "tono": "ofi"})
    assert c.put("/admin/catalogo/categorias/Muebles", json={"nuevo_nombre": "hogar"}).status_code == 409
    assert c.put("/admin/catalogo/categorias/Nada", json={"nuevo_nombre": "Otra"}).status_code == 404


# ----------------------------------------------------------- catálogo de ejemplo (sembrar_datos.py)
def test_sembrar_datos_carga_el_catalogo_completo_y_no_se_duplica(tmp_path):
    from sembrar_datos import sembrar
    ruta = str(tmp_path / "s.db")
    assert sembrar(ruta, 3250) == 120
    assert sembrar(ruta, 3250) == 0                                   # repetirlo no duplica nada
    conn = sqlite3.connect(ruta)
    por_categoria = dict(conn.execute("SELECT category, COUNT(*) FROM productos GROUP BY category"))
    assert por_categoria == {"Hogar": 30, "Electrodomésticos": 30, "Tecnología": 30, "Moda": 30}
    subs_moda = {r[0] for r in conn.execute("SELECT nombre FROM subcategorias WHERE categoria = 'Moda'")}
    assert {"Ropa de mujer", "Ropa de hombre", "Calzado de mujer", "Calzado de hombre"} <= subs_moda
    # cada producto apunta a una subcategoría que existe, con precio válido y stock no negativo
    assert conn.execute("SELECT COUNT(*) FROM productos p LEFT JOIN subcategorias s "
                        "ON s.categoria = p.category AND s.nombre = p.sub_category WHERE s.nombre IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM productos WHERE precio <= 0 OR stock < 0 OR activo != 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM productos WHERE stock = 0").fetchone()[0] >= 1      # hay agotados de ejemplo
    # el precio en COP de la lista se recupera exacto con la tasa
    cop = conn.execute("SELECT ROUND(precio * 3250) FROM productos WHERE product_name = 'Jean tiro alto skinny'").fetchone()[0]
    assert cop == 139900
    conn.close()


def test_tienda_muestra_el_catalogo_sembrado(tmp_path):
    from sembrar_datos import sembrar
    ruta = str(tmp_path / "t.db")
    sembrar(ruta, 3250)
    c = create_app({"DB_PATH": ruta, "API_KEY": "a", "ADMIN_KEY": "b", "TESTING": True}).test_client()
    d = c.get("/tienda/productos?limite=100").get_json()
    assert d["total"] == 120 and all(p["precio"] > 0 for p in d["resultados"])
    cats = {k["categoria"]: k["productos"] for k in c.get("/tienda/categorias").get_json()}
    assert cats == {"Hogar": 30, "Electrodomésticos": 30, "Tecnología": 30, "Moda": 30}


# ----------------------------------------------------------- envío: costo, despacho y llegada estimada (envio.py)
def test_festivos_de_colombia_2026():
    from datetime import date
    from envio import festivos
    f = festivos(2026)
    esperados = {"2026-01-01", "2026-01-12", "2026-03-23", "2026-04-02", "2026-04-03", "2026-05-01", "2026-05-18",
                 "2026-06-08", "2026-06-15", "2026-06-29", "2026-07-20", "2026-08-07", "2026-08-17", "2026-10-12",
                 "2026-11-02", "2026-11-16", "2026-12-08", "2026-12-25"}
    assert {d.isoformat() for d in f} == esperados
    assert date(2026, 11, 16) not in {d for d in f if d.weekday() >= 5}   # todos los festivos de 2026 caen en día hábil


def test_envio_salta_festivos_y_fines_de_semana():
    from datetime import date, datetime
    from envio import cotizar, sumar_habiles
    assert sumar_habiles(date(2026, 11, 13), 1) == date(2026, 11, 17)     # viernes + 1 hábil: el lunes 16 es festivo
    assert sumar_habiles(date(2026, 11, 14), 0) == date(2026, 11, 17)     # sábado: pasa al siguiente hábil
    r = cotizar("Colombia", "Antioquia", "Medellín", 0, datetime(2026, 11, 10, 10, 0))   # martes 10:00
    ex = {o["id"]: o for o in r["opciones"]}
    assert r["zona"] == "nacional" and r["origen"] == "Montería, Córdoba"
    assert ex["express"]["despacho"] == "2026-11-11"                       # 1 día hábil de alistamiento
    assert (ex["express"]["entrega_desde"], ex["express"]["entrega_hasta"]) == ("2026-11-13", "2026-11-17")
    assert (ex["estandar"]["entrega_desde"], ex["estandar"]["entrega_hasta"]) == ("2026-11-17", "2026-11-20")
    assert ex["estandar"]["costo_cop"] == 17_900 and ex["estandar"]["gratis"] is False
    assert [o["id"] for o in r["opciones"]] == ["express", "estandar"]    # la más rápida primero


@pytest.mark.parametrize("pais,region,ciudad,zona", [
    ("Colombia", "Córdoba", "Montería", "local"), ("Colombia", "Córdoba", "monteria", "local"),   # sin tildes ni mayúsculas
    ("Colombia", "Córdoba", "Lorica", "regional"), ("Colombia", "Atlántico", "Barranquilla", "costa"),
    ("Colombia", "Antioquia", "Medellín", "nacional"), ("Colombia", "Amazonas", "Leticia", "especial"),
    ("España", "", "Madrid", "internacional")])
def test_envio_zonas(pais, region, ciudad, zona):
    from envio import zona_de
    assert zona_de(pais, region, ciudad) == zona


def test_envio_gratis_hora_de_corte_e_internacional():
    from datetime import datetime
    from envio import cotizar
    # Compras desde 200.000 COP: el estándar sale gratis (el express no)
    r = cotizar("Colombia", "Antioquia", "Medellín", 200_000, datetime(2026, 11, 10, 10, 0))
    ex = {o["id"]: o for o in r["opciones"]}
    assert ex["estandar"]["gratis"] is True and ex["estandar"]["costo_cop"] == 0 and ex["express"]["costo_cop"] == 27_900
    # "Mismo día": solo en Montería, en día hábil y antes de las 2:00 p. m.
    ids = lambda cuando: [o["id"] for o in cotizar("Colombia", "Córdoba", "Montería", 0, cuando)["opciones"]]
    assert "mismo_dia" in ids(datetime(2026, 11, 10, 13, 59))
    assert "mismo_dia" not in ids(datetime(2026, 11, 10, 14, 0))           # pasó la hora de corte
    assert "mismo_dia" not in ids(datetime(2026, 11, 14, 9, 0))            # sábado
    assert "mismo_dia" not in ids(datetime(2026, 11, 16, 9, 0))            # festivo
    r = cotizar("España", "", "Madrid", 900_000, datetime(2026, 11, 10, 10, 0))
    assert [o["id"] for o in r["opciones"]] == ["internacional"] and r["envio_gratis_desde_cop"] is None
    assert r["opciones"][0]["costo_cop"] == 120_000                         # nunca gratis fuera de Colombia


def test_ruta_tienda_envio_es_publica_y_valida_los_datos(c):
    r = c.get("/tienda/envio?pais=Colombia&region=Antioquia&ciudad=Medell%C3%ADn&subtotal_cop=150000")   # sin clave: es pública
    assert r.status_code == 200
    d = r.get_json()
    assert d["zona"] == "nacional" and d["opciones"] and d["hora_de_corte"] == 14
    assert {"id", "costo_cop", "gratis", "despacho", "entrega_desde", "entrega_hasta", "alistamiento_dias",
            "transito_min", "transito_max"} <= set(d["opciones"][0])
    for q in ("", "?pais=Narnia", "?pais=Colombia&subtotal_cop=abc", "?pais=Colombia&subtotal_cop=-5",
              "?pais=Colombia&subtotal_cop=99999999999999"):
        assert c.get("/tienda/envio" + q).status_code == 422, q


def test_admin_turnos_lista_y_finalizar(tur):
    app, c = tur
    # 1. Sin sesión de admin da 401
    assert c.get("/admin/turnos/api/lista").status_code == 401
    assert c.post("/admin/turnos/api/finalizar", json={"servicio_id": 1}).status_code == 401

    # 2. Cliente normal da 403
    assert c.post("/cuenta/registro", json=dict(CUENTA, email="cli_turno@example.com")).status_code == 201
    assert c.get("/admin/turnos/api/lista").status_code == 403
    assert c.post("/admin/turnos/api/finalizar", json={"servicio_id": 1}).status_code == 403

    # Pedir turno con sesión iniciada de cliente (se liga a cuenta_id)
    r_pedir = c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"})
    assert r_pedir.status_code == 200
    num_cliente = r_pedir.get_json()["numero"]

    # Salir de cliente y pedir turno anónimo
    c.post("/cuenta/salir")
    r_pedir_anon = c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Información de un producto"})
    assert r_pedir_anon.status_code == 200
    num_anon = r_pedir_anon.get_json()["numero"]

    # Iniciar sesión como admin
    assert c.post("/admin/registro", json=ADM).status_code == 201

    # Consultar lista
    r_lista = c.get("/admin/turnos/api/lista")
    assert r_lista.status_code == 200
    servicios = r_lista.get_json()
    assert len(servicios) == 2
    s1 = next(s for s in servicios if s["id"] == 1)
    assert s1["en_espera"] == 2
    assert "asesores" in s1 and "meta_espera_min" in s1 and "asesores_recomendados" in s1
    assert len(s1["turnos"]) == 2

    # Verificar datos del turno del cliente (con nombre, SIN telefono ni correo)
    t_cli = next(t for t in s1["turnos"] if t["numero"] == num_cliente)
    assert t_cli["estado"] == "espera"
    assert t_cli["cliente_nombre"] is not None
    assert "Ana" in t_cli["cliente_nombre"]
    assert "telefono" not in t_cli and "email" not in t_cli and "correo" not in t_cli
    assert "hora_llegada" in t_cli and "minutos_esperando" in t_cli

    # Turno anónimo no tiene nombre
    t_anon = next(t for t in s1["turnos"] if t["numero"] == num_anon)
    assert t_anon["cliente_nombre"] is None

    # Llamar siguiente turno
    r_llam = c.post("/admin/turnos/api/llamar", json={"servicio_id": 1})
    assert r_llam.status_code == 200 and r_llam.get_json()["numero"] == num_cliente

    # Lista ahora muestra num_cliente en atendiendo
    s1_act = c.get("/admin/turnos/api/lista").get_json()[0]
    t_atendiendo = next(t for t in s1_act["turnos"] if t["numero"] == num_cliente)
    assert t_atendiendo["estado"] == "atendiendo"

    # Finalizar turno en atención
    r_fin = c.post("/admin/turnos/api/finalizar", json={"servicio_id": 1})
    assert r_fin.status_code == 200

    # Lista ahora muestra 1 en espera y num_cliente en finalizados
    s1_post = c.get("/admin/turnos/api/lista").get_json()[0]
    assert s1_post["en_espera"] == 1
    assert any(t["numero"] == num_cliente and t["estado"] == "finalizado" for t in s1_post["finalizados"])
