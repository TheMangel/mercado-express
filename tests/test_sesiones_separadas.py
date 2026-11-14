# -*- coding: utf-8 -*-
"""La sesión del cliente (tienda) y la del administrador (panel) son independientes."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import COOKIE_SESION, COOKIE_SESION_ADMIN, create_app  # noqa: E402

CLIENTE = {"nombre": "Carlos", "apellido": "Gómez", "email": "carlos@example.com", "password": "claveSegura1",
           "telefono": "+57 300 111 2233", "pais": "Colombia", "region": "Córdoba", "ciudad": "Montería",
           "direccion": "Calle 10 # 20-30", "codigo_postal": "230001", "autorizacion_datos": True}
ADMIN = {"nombre": "Admin Panel", "email": "admin_ses@example.com", "password": "clave-admin-123",
         "codigo": "clave-admin", "autorizacion_datos": True}


@pytest.fixture()
def s(tmp_path):
    ruta = str(tmp_path / "sesiones.db")
    app = create_app({"DB_PATH": ruta, "API_KEY": "u", "ADMIN_KEY": "clave-admin", "CARGAR_DATASET": False,
                      "TESTING": True, "TURNOS_MAX_POR_IP": 100})
    # Las dos cuentas existen desde el principio; cada prueba abre las sesiones que necesita.
    c = app.test_client()
    assert c.post("/cuenta/registro", json=CLIENTE).status_code == 201
    assert c.post("/cuenta/salir").status_code == 200
    assert c.post("/admin/registro", json=ADMIN).status_code == 201
    assert c.post("/admin/salir").status_code == 200
    return {"app": app, "ruta": ruta}


def navegador(s):
    return s["app"].test_client()


def entrar_cliente(c):
    assert c.post("/cuenta/entrar", json={"email": CLIENTE["email"], "password": CLIENTE["password"]}).status_code == 200


def entrar_admin(c):
    assert c.post("/admin/entrar", json={"email": ADMIN["email"], "password": ADMIN["password"]}).status_code == 200


def test_entrar_como_admin_no_cierra_la_sesion_del_cliente(s):
    c = navegador(s)
    entrar_cliente(c)
    entrar_admin(c)                                           # mismo navegador, las dos sesiones a la vez
    yo = c.get("/cuenta/yo").get_json()
    assert yo["sesion"] is True and yo["cuenta"]["email"] == CLIENTE["email"]    # la tienda sigue con el cliente
    assert c.get("/cuenta/pedidos").status_code == 200
    panel = c.get("/admin/yo").get_json()
    assert panel["admin"] is True and panel["cuenta"]["email"] == ADMIN["email"]
    assert c.get_cookie(COOKIE_SESION) and c.get_cookie(COOKIE_SESION_ADMIN)
    assert c.get_cookie(COOKIE_SESION).value != c.get_cookie(COOKIE_SESION_ADMIN).value


def test_entrar_como_cliente_tampoco_cierra_la_del_admin(s):
    c = navegador(s)
    entrar_admin(c)
    entrar_cliente(c)
    assert c.get("/admin/yo").get_json()["admin"] is True
    assert c.get("/admin/catalogo/datos").status_code == 200
    assert c.get("/cuenta/yo").get_json()["cuenta"]["email"] == CLIENTE["email"]


def test_la_tienda_no_muestra_la_cuenta_del_admin(s):
    c = navegador(s)
    entrar_admin(c)                                           # solo administrador, sin cliente
    assert c.get("/cuenta/yo").get_json() == {"sesion": False, "cuenta": None}
    assert c.get("/cuenta/pedidos").status_code == 403        # sabe que es admin, pero no es una función suya
    assert c.put("/cuenta/perfil", json={}).status_code == 403
    assert c.post("/cuenta/pago", json={}).status_code == 403


def test_el_panel_no_acepta_la_sesion_del_cliente(s):
    c = navegador(s)
    entrar_cliente(c)                                         # solo cliente, sin administrador
    assert c.get("/admin/yo").get_json()["admin"] is False
    assert c.get("/admin/catalogo/datos").status_code == 403
    assert c.get("/admin/turnos/api/lista").status_code == 403


def test_sin_ninguna_sesion_el_panel_da_401(s):
    c = navegador(s)
    assert c.get("/admin/catalogo/datos").status_code == 401
    assert c.get("/cuenta/pedidos").status_code == 401


def test_cada_salir_cierra_solo_su_sesion(s):
    c = navegador(s)
    entrar_cliente(c)
    entrar_admin(c)
    assert c.post("/cuenta/salir").status_code == 200         # sale el cliente...
    assert c.get("/cuenta/yo").get_json()["sesion"] is False
    assert c.get("/admin/yo").get_json()["admin"] is True     # ...y el administrador sigue
    entrar_cliente(c)
    assert c.post("/admin/salir").status_code == 200          # sale el administrador...
    assert c.get("/admin/yo").get_json()["admin"] is False
    assert c.get("/cuenta/yo").get_json()["sesion"] is True   # ...y el cliente sigue
    conn = sqlite3.connect(s["ruta"])
    assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 1     # la sesión cerrada se borra de la base
    conn.close()


def test_cada_formulario_solo_recibe_su_tipo_de_cuenta(s):
    c = navegador(s)
    r = c.post("/cuenta/entrar", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert r.status_code == 401 and r.get_json()["error"] == "Correo o contraseña incorrectos"
    r = c.post("/admin/entrar", json={"email": CLIENTE["email"], "password": CLIENTE["password"]})
    assert r.status_code == 401 and r.get_json()["error"] == "Correo o contraseña incorrectos"
    assert c.get_cookie(COOKIE_SESION) is None and c.get_cookie(COOKIE_SESION_ADMIN) is None


def test_cada_login_pone_solo_su_cookie_y_httponly(s):
    c = navegador(s)
    r = c.post("/admin/entrar", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    cookies = r.headers.getlist("Set-Cookie")
    assert len(cookies) == 1 and cookies[0].startswith(f"{COOKIE_SESION_ADMIN}=") and "HttpOnly" in cookies[0]
    r = c.post("/cuenta/entrar", json={"email": CLIENTE["email"], "password": CLIENTE["password"]})
    cookies = r.headers.getlist("Set-Cookie")
    assert len(cookies) == 1 and cookies[0].startswith(f"{COOKIE_SESION}=") and "HttpOnly" in cookies[0]


def test_las_cookies_no_son_intercambiables(s):
    """Copiar el token del cliente a la cookie de admin (o al revés) no da acceso."""
    c = navegador(s)
    entrar_cliente(c)
    entrar_admin(c)
    token_cliente = c.get_cookie(COOKIE_SESION).value
    token_admin = c.get_cookie(COOKIE_SESION_ADMIN).value
    intruso = navegador(s)
    intruso.set_cookie(COOKIE_SESION_ADMIN, token_cliente)
    assert intruso.get("/admin/yo").get_json()["admin"] is False
    assert intruso.get("/admin/catalogo/datos").status_code in (401, 403)
    intruso2 = navegador(s)
    intruso2.set_cookie(COOKIE_SESION, token_admin)
    assert intruso2.get("/cuenta/yo").get_json()["sesion"] is False


def test_un_turno_pedido_con_sesion_de_admin_no_se_liga_a_ninguna_cuenta(s):
    c = navegador(s)
    entrar_admin(c)
    assert c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).status_code == 200
    entrar_cliente(c)
    assert c.post("/turnos/api/pedir", json={"servicio_id": 1, "motivo": "Estado de mi pedido"}).status_code == 200
    conn = sqlite3.connect(s["ruta"])
    cuentas = [f[0] for f in conn.execute("SELECT cuenta_id FROM turnos ORDER BY numero")]
    conn.close()
    assert cuentas[0] is None and cuentas[1] is not None      # el admin pide como visitante; el cliente, con su cuenta
