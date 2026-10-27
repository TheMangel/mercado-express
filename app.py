# -*- coding: utf-8 -*-
"""API REST de Mercado Express (Flask + SQLite).

Ideas del proyecto que se ven aquí:
  1. Validación en la app (rápida) + reglas en la base de datos (última barrera).
  2. Registrar un pedido = una transacción (COMMIT si todo sale bien, ROLLBACK si algo falla).
  3. Consultas de negocio del E1 como reportes.
  4. Ley 1581 de 2012: nombres enmascarados por defecto, datos completos solo con clave
     de administrador, autorización del cliente al crearlo y registro de cada acceso.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from cargar_datos import CSV_POR_DEFECTO, cargar
from esquema import COLUMNAS_PERFIL, SCHEMA
from ubicaciones import CODIGO_POSTAL, CODIGO_POSTAL_GENERAL, UBICACIONES, como_lista

MODOS_ENVIO = {"Standard Class", "Second Class", "First Class", "Same Day"}
SEGMENTOS = {"Consumer", "Corporate", "Home Office"}
REGIONES = {"East", "West", "Central", "South"}
# Rutas sin clave: las páginas (no traen datos) y el catálogo de la tienda (sin datos personales).
RUTAS_PUBLICAS = {"salud", "favicon", "pagina_tienda", "pagina_admin", "pagina_catalogo",
                  "tienda_productos", "tienda_categorias", "tienda_resumen", "tienda_config",
                  "tienda_ubicaciones", "cuenta_registro", "cuenta_entrar", "cuenta_salir", "cuenta_yo",
                  "cuenta_direccion",
                  # Sección de administradores: no usan X-API-Key, usan cuenta con rol admin + sesión.
                  # Las que cambian datos llaman a requiere_admin() y rechazan a quien no lo sea.
                  "admin_registro", "admin_entrar", "admin_yo",
                  "catalogo_datos", "catalogo_categoria", "catalogo_subcategoria", "catalogo_producto",
                  "catalogo_productos_lista", "catalogo_producto_ver", "catalogo_producto_editar",
                  "catalogo_producto_borrar"}

# Opciones de apariencia que puede elegir un administrador (la base también las valida con CHECK)
TONOS = {"mue", "tec", "ofi", "nat", "mar"}
ICONOS = {"chair", "shelf", "table", "lamp", "phone", "mouse", "printer", "binder", "box"}
TONO_POR_CATEGORIA = {"Furniture": "mue", "Technology": "tec"}          # el resto del dataset: "ofi"
ICONO_POR_SUB = {"Chairs": "chair", "Bookcases": "shelf", "Tables": "table", "Furnishings": "lamp",
                 "Phones": "phone", "Accessories": "mouse", "Machines": "printer", "Copiers": "printer",
                 "Storage": "box", "Appliances": "box"}                  # el resto: "binder"
PRECIO_MAXIMO = 1_000_000                                               # en USD
STOCK_MAXIMO = 1_000_000                                                # unidades por producto
STOCK_AVISO = 50      # desde aquí (50 o menos) la tienda le muestra al cliente cuántas unidades quedan
STOCK_CRITICO = 10    # con 10 o menos el aviso se resalta más

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,185}\.[^@\s]{2,}$")
COOKIE_SESION = "me_sesion"
DURACION_SESION = 7 * 24 * 3600      # 7 días
MAX_INTENTOS, VENTANA_INTENTOS = 5, 600   # 5 intentos fallidos cada 10 minutos

# Orden permitido en el catálogo (lista blanca: nunca se arma el ORDER BY con texto del usuario)
ORDENES_TIENDA = {
    "vendidos": "vendidos DESC, p.product_name",
    "precio_asc": "precio ASC, p.product_name",
    "precio_desc": "precio DESC, p.product_name",
    "nombre": "p.product_name",
    "recientes": "p.rowid DESC, p.product_name",
}


def migrar(conn):
    """Deja al día una base que ya existía (idempotente, se puede repetir sin problema).

    CREATE TABLE IF NOT EXISTS no agrega columnas a tablas viejas, así que se agregan aquí,
    y las categorías que ya están en el dataset se copian a las tablas nuevas."""
    conn.executescript(SCHEMA)
    cols = lambda tabla: {f[1] for f in conn.execute(f"PRAGMA table_info({tabla})")}
    if "precio" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN precio REAL CHECK (precio IS NULL OR precio > 0)")
    if "activo" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1))")
    if "stock" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)")
    if "rol" not in cols("cuentas"):
        conn.execute("ALTER TABLE cuentas ADD COLUMN rol TEXT NOT NULL DEFAULT 'cliente' "
                     "CHECK (rol IN ('cliente','admin'))")
    for columna, definicion in COLUMNAS_PERFIL.items():     # apellido, teléfono y dirección
        if columna not in cols("cuentas"):
            conn.execute(f"ALTER TABLE cuentas ADD COLUMN {columna} {definicion}")
    ahora = datetime.now().isoformat(timespec="seconds")
    for (cat,) in conn.execute("SELECT DISTINCT category FROM productos WHERE category IS NOT NULL").fetchall():
        conn.execute("INSERT OR IGNORE INTO categorias (nombre, tono, creada) VALUES (?,?,?)",
                     (cat, TONO_POR_CATEGORIA.get(cat, "ofi"), ahora))
    for cat, sub in conn.execute("SELECT DISTINCT category, sub_category FROM productos "
                                 "WHERE category IS NOT NULL AND sub_category IS NOT NULL").fetchall():
        conn.execute("INSERT OR IGNORE INTO subcategorias (categoria, nombre, icono) VALUES (?,?,?)",
                     (cat, sub, ICONO_POR_SUB.get(sub, "binder")))
    conn.commit()


class ErrorApi(Exception):
    def __init__(self, codigo, mensaje, detalles=None):
        self.codigo, self.mensaje, self.detalles = codigo, mensaje, detalles


# ----------------------------------------------------------------- utilidades
def nivel_stock(stock):
    """Cómo se avisa el stock al cliente: 'agotado' (0), 'critico' (1-10), 'bajo' (11-50) o None (más de 50)."""
    if stock <= 0:
        return "agotado"
    if stock <= STOCK_CRITICO:
        return "critico"
    if stock <= STOCK_AVISO:
        return "bajo"
    return None


def _es_numero(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _es_entero(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _texto(valor, campo, errores, maximo=120, obligatorio=True):
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if obligatorio:
            errores.append(f"{campo}: es obligatorio")
        return None
    if not isinstance(valor, str):
        errores.append(f"{campo}: debe ser texto")
        return None
    valor = valor.strip()
    if len(valor) > maximo:
        errores.append(f"{campo}: máximo {maximo} caracteres")
        return None
    return valor


def _fecha(valor, campo, errores):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        errores.append(f"{campo}: debe tener formato AAAA-MM-DD")
        return None


def enmascarar_nombre(nombre):
    """'Claire Gute' -> 'Claire G.' (minimización de datos personales)."""
    partes = nombre.split()
    if len(partes) < 2:
        return partes[0] if partes else ""
    return f"{partes[0]} {partes[-1][0]}."


def _json_cliente(fila, completo):
    d = {
        "customer_id": fila["customer_id"],
        "nombre": fila["customer_name"] if completo else enmascarar_nombre(fila["customer_name"]),
        "segmento": fila["segment"],
        "pais": fila["country"],
        "estado": fila["state"],
        "region": fila["region"],
    }
    if completo:
        d["ciudad"] = fila["city"]
        d["codigo_postal"] = fila["postal_code"]
        d["autorizacion_datos"] = bool(fila["autorizacion_datos"]) if fila["autorizacion_datos"] is not None else None
    return d


def _paginacion(limite_defecto=20):
    try:
        pagina = int(request.args.get("pagina", 1))
        limite = int(request.args.get("limite", limite_defecto))
    except ValueError:
        raise ErrorApi(400, "pagina y limite deben ser números enteros")
    if pagina < 1 or not 1 <= limite <= 100:
        raise ErrorApi(400, "pagina debe ser >= 1 y limite debe estar entre 1 y 100")
    return pagina, limite, (pagina - 1) * limite


def nombre_repetido(conn, categoria, nombre, excepto=None):
    """¿Ya hay un producto con ese nombre en la categoría? Se compara en Python con casefold(): el lower()
    de SQLite solo entiende letras ASCII y no vería que «BALÓN» y «Balón» son el mismo nombre.
    `excepto` = (product_id, nombre) del producto que se está editando, para que no choque consigo mismo."""
    buscado = nombre.casefold()
    for pid, nom in conn.execute("SELECT product_id, product_name FROM productos WHERE category = ?", (categoria,)):
        if nom.casefold() == buscado and (pid, nom) != excepto:
            return True
    return False


def _like(texto):
    seguro = texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{seguro}%"


def _limite_reporte(por_defecto=10):
    try:
        limite = int(request.args.get("limite", por_defecto))
    except ValueError:
        raise ErrorApi(400, "limite debe ser un número entero")
    if not 1 <= limite <= 100:
        raise ErrorApi(400, "limite debe estar entre 1 y 100")
    return limite


# ------------------------------------------------------------------ la fábrica
def create_app(config=None):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    aqui = os.path.dirname(os.path.abspath(__file__))
    app.config.update(
        DB_PATH=os.environ.get("DB_PATH", os.path.join(aqui, "mercado_express.db")),
        CSV_PATH=os.environ.get("CSV_PATH", CSV_POR_DEFECTO),
        API_KEY=os.environ.get("API_KEY"),
        ADMIN_KEY=os.environ.get("ADMIN_KEY"),
        # Tasa de referencia para mostrar precios en COP (octubre de 2026: ~3.200 a 3.300).
        # Se cambia con la variable de entorno TASA_COP, sin tocar el código.
        TASA_COP=float(os.environ.get("TASA_COP", "3250")),
    )
    if config:
        app.config.update(config)

    # Seguridad por defecto: si no hay claves configuradas, se generan al azar.
    for nombre in ("API_KEY", "ADMIN_KEY"):
        if not app.config[nombre]:
            app.config[nombre] = secrets.token_urlsafe(24)
            print(f"[AVISO] {nombre} no estaba definida. Clave temporal de esta ejecución: "
                  f"{app.config[nombre]}", flush=True)

    # Si la base no existe, se crea desde el CSV original.
    if not os.path.exists(app.config["DB_PATH"]):
        conteos = cargar(app.config["CSV_PATH"], app.config["DB_PATH"])
        print(f"[INFO] Base creada: {conteos}", flush=True)
    conn = sqlite3.connect(app.config["DB_PATH"])
    migrar(conn)  # idempotente: agrega tablas/columnas nuevas y las categorías del dataset
    conn.close()

    # ------------------------------------------------------------ conexión
    def db():
        if "db" not in g:
            # isolation_level=None -> nosotros controlamos BEGIN/COMMIT/ROLLBACK explícitamente
            g.db = sqlite3.connect(app.config["DB_PATH"], isolation_level=None)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")  # sin esto SQLite ignora las FOREIGN KEY
        return g.db

    @app.teardown_appcontext
    def cerrar(_exc):
        conexion = g.pop("db", None)
        if conexion is not None:
            conexion.close()

    # ------------------------------------------------------------ seguridad
    def _coincide(a, b):
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    @app.before_request
    def autenticar():
        if request.endpoint in RUTAS_PUBLICAS:
            return None
        clave = request.headers.get("X-API-Key", "")
        if _coincide(clave, app.config["ADMIN_KEY"]):
            g.nivel = "admin"
        elif _coincide(clave, app.config["API_KEY"]):
            g.nivel = "usuario"
        else:
            raise ErrorApi(401, "Falta la cabecera X-API-Key o la clave no es válida")

    def datos_completos():
        """True solo si piden ?completo=1 Y tienen clave de administrador."""
        if request.args.get("completo") != "1":
            return False
        if g.nivel != "admin":
            raise ErrorApi(403, "Los datos personales completos requieren clave de administrador")
        return True

    def auditar(detalle):
        db().execute(
            "INSERT INTO auditoria (fecha, nivel, endpoint, detalle) VALUES (?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), g.nivel, request.path, detalle))

    # ------------------------------------------------------------ errores
    @app.errorhandler(ErrorApi)
    def _api(e):
        cuerpo = {"error": e.mensaje}
        if e.detalles:
            cuerpo["detalles"] = e.detalles
        return jsonify(cuerpo), e.codigo

    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"error": "Ruta no encontrada"}), 404

    @app.errorhandler(405)
    def _405(_e):
        return jsonify({"error": "Método no permitido en esta ruta"}), 405

    @app.errorhandler(500)
    def _500(_e):
        return jsonify({"error": "Error interno del servidor"}), 500

    def json_del_cuerpo():
        datos = request.get_json(silent=True)
        if not isinstance(datos, dict):
            raise ErrorApi(400, "El cuerpo debe ser un JSON (objeto)")
        return datos

    # ------------------------------------------------------------ pantallas web
    @app.get("/")
    def pagina_tienda():
        return send_from_directory(os.path.join(aqui, "interfaz"), "tienda.html")

    @app.get("/admin")
    def pagina_admin():
        return send_from_directory(os.path.join(aqui, "interfaz"), "admin.html")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204     # sin ícono propio; así el navegador no recibe un error en cada página

    @app.get("/admin/catalogo")
    def pagina_catalogo():
        # Página aparte para administradores. La página en sí no trae datos: todo lo que
        # muestra y guarda pasa por rutas que exigen sesión de administrador.
        return send_from_directory(os.path.join(aqui, "interfaz"), "catalogo.html")

    @app.after_request
    def cabeceras_seguras(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.endpoint not in ("pagina_tienda", "pagina_admin", "pagina_catalogo"):
            resp.headers.setdefault("Cache-Control", "no-store")  # no guardar datos en caché
        return resp

    # ------------------------------------------------------------ tienda pública (sin datos personales)
    # El dataset no trae precio unitario: se deduce del historial como
    # ventas / (cantidad x (1 - descuento)), promediado por producto.
    # Los productos creados desde /admin/catalogo sí traen precio propio (productos.precio),
    # que tiene prioridad. Por eso es LEFT JOIN: un producto nuevo aún no tiene ventas.
    UNION_PRECIO = ("FROM productos p "
                    "LEFT JOIN detalle_pedido d ON d.product_id = p.product_id AND d.product_name = p.product_name "
                    "LEFT JOIN categorias c ON c.nombre = p.category "
                    "LEFT JOIN subcategorias s ON s.categoria = p.category AND s.nombre = p.sub_category")

    @app.get("/tienda/productos")
    def tienda_productos():
        pagina, limite, offset = _paginacion(12)
        filtros, params = ["p.activo = 1"], []      # los productos ocultos por un admin no salen en la tienda
        if request.args.get("categoria"):
            filtros.append("p.category = ?")
            params.append(request.args["categoria"])
        if request.args.get("subcategoria"):
            filtros.append("p.sub_category = ?")
            params.append(request.args["subcategoria"])
        if request.args.get("q"):
            filtros.append("p.product_name LIKE ? ESCAPE '\\'")
            params.append(_like(request.args["q"][:80]))
        orden = request.args.get("orden", "vendidos")
        if orden not in ORDENES_TIENDA:
            raise ErrorApi(400, f"orden debe ser uno de {sorted(ORDENES_TIENDA)}")
        donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
        base = f"{UNION_PRECIO} {donde} GROUP BY p.product_id, p.product_name, c.tono, s.icono"
        total = db().execute(f"SELECT COUNT(*) FROM (SELECT 1 {base})", params).fetchone()[0]
        filas = db().execute(f"""
            SELECT p.product_id, p.product_name, p.category, p.sub_category,
                   COALESCE(p.precio, ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)) AS precio,
                   COALESCE(SUM(d.quantity), 0) AS vendidos,
                   COALESCE(MAX(d.discount), 0) AS descuento_max, p.stock AS stock,
                   c.tono AS tono, s.icono AS icono
            {base} ORDER BY {ORDENES_TIENDA[orden]} LIMIT ? OFFSET ?""", params + [limite, offset]).fetchall()
        return jsonify({"pagina": pagina, "limite": limite, "total": total, "resultados": [
            {"product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
             "subcategoria": r["sub_category"], "precio": r["precio"], "vendidos": r["vendidos"],
             "descuento_max": r["descuento_max"], "tono": r["tono"], "icono": r["icono"],
             # El stock exacto solo se publica cuando ya quedan pocas unidades; con más, va null
             # (así la tienda no regala el inventario completo a quien consulte la API).
             "stock": r["stock"] if r["stock"] <= STOCK_AVISO else None,
             "stock_nivel": nivel_stock(r["stock"])} for r in filas]})

    def arbol_categorias(solo_activos=True):
        """Categorías con sus subcategorías y cuántos productos tiene cada una (incluye las vacías).
        La tienda cuenta solo los productos activos; el panel de administración los cuenta todos."""
        filtro_activo = "AND p.activo = 1" if solo_activos else ""
        filas = db().execute(f"""
            SELECT c.nombre AS cat, c.tono, s.nombre AS sub, s.icono,
                   (SELECT COUNT(*) FROM productos p WHERE p.category = c.nombre AND p.sub_category = s.nombre {filtro_activo}) AS n
            FROM categorias c LEFT JOIN subcategorias s ON s.categoria = c.nombre
            ORDER BY c.nombre, s.nombre""").fetchall()
        arbol = {}
        for r in filas:
            grupo = arbol.setdefault(r["cat"], {"categoria": r["cat"], "tono": r["tono"],
                                                "productos": 0, "subcategorias": []})
            if r["sub"] is not None:
                grupo["subcategorias"].append({"nombre": r["sub"], "icono": r["icono"], "productos": r["n"]})
                grupo["productos"] += r["n"]
        return list(arbol.values())

    @app.get("/tienda/categorias")
    def tienda_categorias():
        return jsonify(arbol_categorias())

    @app.get("/tienda/config")
    def tienda_config():
        return jsonify({"monedas": ["USD", "COP"], "tasa_cop": app.config["TASA_COP"]})

    # ------------------------------------------------------------ cuentas de cliente (tienda)
    intentos = {}                                   # (ip, correo) -> momentos de intentos fallidos
    hash_falso = generate_password_hash("no-existe")  # para que "correo no existe" tarde lo mismo

    def _hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _ahora():
        return datetime.now().isoformat(timespec="seconds")

    def _abrir_sesion(cuenta_id, respuesta):
        conn = db()
        conn.execute("DELETE FROM sesiones WHERE expira < ?", (_ahora(),))   # limpieza
        token = secrets.token_urlsafe(32)
        expira = (datetime.now() + timedelta(seconds=DURACION_SESION)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO sesiones (token_hash, cuenta_id, expira) VALUES (?,?,?)",
                     (_hash_token(token), cuenta_id, expira))
        seguro = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        respuesta.set_cookie(COOKIE_SESION, token, max_age=DURACION_SESION,
                             httponly=True, samesite="Lax", secure=seguro)
        return respuesta

    def _cuenta_actual():
        token = request.cookies.get(COOKIE_SESION)
        if not token:
            return None
        fila = db().execute(
            "SELECT c.*, s.expira FROM sesiones s JOIN cuentas c ON c.id = s.cuenta_id "
            "WHERE s.token_hash = ?", (_hash_token(token),)).fetchone()
        return fila if fila and fila["expira"] >= _ahora() else None

    def _texto_cuenta(datos, campo):
        valor = datos.get(campo)
        return valor if isinstance(valor, str) else ""

    def _validar_telefono(valor, errores):
        v = valor.strip() if isinstance(valor, str) else ""
        digitos = re.sub(r"\D", "", v)
        if not re.fullmatch(r"\+?[0-9 ()-]{7,25}", v) or not 7 <= len(digitos) <= 15:
            errores.append("telefono: escribe un número válido (de 7 a 15 dígitos)")
            return None
        return v

    def _validar_direccion(datos, errores):
        """País y región se validan contra la lista de ubicaciones.py; el código postal según el país."""
        pais = _texto(datos.get("pais"), "pais", errores, 60)
        region = _texto(datos.get("region"), "region", errores, 60)
        ciudad = _texto(datos.get("ciudad"), "ciudad", errores, 80)
        linea = _texto(datos.get("direccion"), "direccion", errores, 120)
        cp = _texto(datos.get("codigo_postal"), "codigo_postal", errores, 10)
        if pais is not None:
            if pais not in UBICACIONES:
                errores.append(f"pais: «{pais}» no está disponible")
            elif region is not None and UBICACIONES[pais] and region not in UBICACIONES[pais]:
                errores.append(f"region: «{region}» no es una región de {pais}")
        if ciudad is not None and len(ciudad) < 2:
            errores.append("ciudad: mínimo 2 caracteres")
        if linea is not None and len(linea) < 5:
            errores.append("direccion: escríbela completa (mínimo 5 caracteres)")
        if cp is not None and not re.fullmatch(CODIGO_POSTAL.get(pais or "", CODIGO_POSTAL_GENERAL), cp):
            errores.append(f"codigo_postal: formato no válido para {pais or 'ese país'}")
        return {"pais": pais, "region": region, "ciudad": ciudad, "direccion": linea, "codigo_postal": cp}

    def _datos_registro(datos, cliente=True):
        """Valida el registro. Un cliente debe dar nombre, apellido, teléfono y dirección completa;
        un administrador solo nombre, correo y contraseña. Devuelve (nombre, email, clave, perfil)."""
        nombre = _texto_cuenta(datos, "nombre").strip()
        email = _texto_cuenta(datos, "email").strip().lower()
        clave = _texto_cuenta(datos, "password")
        errores, perfil = [], {}
        if cliente:
            apellido = _texto_cuenta(datos, "apellido").strip()
            if not 2 <= len(nombre) <= 60:
                errores.append("nombre: debe tener entre 2 y 60 caracteres")
            if not 2 <= len(apellido) <= 60:
                errores.append("apellido: debe tener entre 2 y 60 caracteres")
            perfil = {"apellido": apellido, "telefono": _validar_telefono(datos.get("telefono"), errores)}
            perfil.update(_validar_direccion(datos, errores))
        elif not 2 <= len(nombre) <= 80:
            errores.append("nombre: debe tener entre 2 y 80 caracteres")
        if len(email) > 254 or not EMAIL_RE.match(email):
            errores.append("email: no es un correo válido")
        if not 8 <= len(clave) <= 128:
            errores.append("password: debe tener entre 8 y 128 caracteres")
        if datos.get("autorizacion_datos") is not True:
            errores.append("autorizacion_datos: debe aceptar el tratamiento de datos personales (Ley 1581 de 2012)")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        return nombre, email, clave, perfil

    def _crear_cuenta(nombre, email, clave, rol, perfil=None):
        extra = perfil or {}
        columnas = ["email", "nombre", "password_hash", "rol", "autorizacion_datos", "fecha_autorizacion", "creada"] + list(extra)
        valores = [email, nombre, generate_password_hash(clave), rol, 1, _ahora(), _ahora()] + list(extra.values())
        try:
            cur = db().execute(f"INSERT INTO cuentas ({', '.join(columnas)}) VALUES ({', '.join('?' * len(columnas))})", valores)
        except sqlite3.IntegrityError:
            raise ErrorApi(409, "Ya existe una cuenta con ese correo")
        return cur.lastrowid

    def _perfil(f):
        """Lo que la persona ve de su propia cuenta (nunca el hash de la contraseña)."""
        con_dir = f["pais"] and f["direccion"]
        return {"nombre": f["nombre"], "apellido": f["apellido"], "email": f["email"], "rol": f["rol"],
                "telefono": f["telefono"],
                "direccion": {"pais": f["pais"], "region": f["region"], "ciudad": f["ciudad"],
                              "direccion": f["direccion"], "codigo_postal": f["codigo_postal"]} if con_dir else None}

    def _frenar_si_abusa(llave):
        """Máximo MAX_INTENTOS fallos cada VENTANA_INTENTOS segundos por (ip, correo). Devuelve los fallos recientes."""
        ahora = time.time()
        recientes = [t for t in intentos.get(llave, []) if ahora - t < VENTANA_INTENTOS]
        if len(recientes) >= MAX_INTENTOS:
            raise ErrorApi(429, "Demasiados intentos fallidos. Espera unos minutos e inténtalo de nuevo")
        return recientes

    def _iniciar_sesion(solo_admin):
        datos = json_del_cuerpo()
        email = _texto_cuenta(datos, "email").strip().lower()
        clave = _texto_cuenta(datos, "password")
        llave = (request.remote_addr, email)
        recientes = _frenar_si_abusa(llave)
        fila = db().execute("SELECT * FROM cuentas WHERE email = ?", (email,)).fetchone()
        correcta = check_password_hash(fila["password_hash"] if fila else hash_falso, clave)
        # Mismo mensaje si el correo no existe, la clave falla o la cuenta no es de admin: no se filtra cuál fue.
        if not fila or not correcta or (solo_admin and fila["rol"] != "admin"):
            intentos[llave] = recientes + [time.time()]
            raise ErrorApi(401, "Correo o contraseña incorrectos")
        intentos.pop(llave, None)
        return _abrir_sesion(fila["id"], jsonify(_perfil(fila)))

    @app.get("/tienda/ubicaciones")
    def tienda_ubicaciones():
        return jsonify(como_lista())

    @app.post("/cuenta/registro")
    def cuenta_registro():
        nombre, email, clave, perfil = _datos_registro(json_del_cuerpo())
        cuenta_id = _crear_cuenta(nombre, email, clave, "cliente", perfil)
        fila = db().execute("SELECT * FROM cuentas WHERE id = ?", (cuenta_id,)).fetchone()
        resp = jsonify(_perfil(fila))
        resp.status_code = 201
        return _abrir_sesion(cuenta_id, resp)

    @app.post("/cuenta/entrar")
    def cuenta_entrar():
        return _iniciar_sesion(solo_admin=False)

    @app.put("/cuenta/direccion")
    def cuenta_direccion():
        """Guarda como dirección principal la que la persona eligió (y su teléfono). Solo para su propia cuenta."""
        cuenta = _cuenta_actual()
        if not cuenta:
            raise ErrorApi(401, "Inicia sesión para guardar tu dirección")
        datos = json_del_cuerpo()
        errores = []
        telefono = _validar_telefono(datos.get("telefono", cuenta["telefono"]), errores)
        d = _validar_direccion(datos, errores)
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        db().execute("UPDATE cuentas SET telefono=?, pais=?, region=?, ciudad=?, direccion=?, codigo_postal=? WHERE id=?",
                     (telefono, d["pais"], d["region"], d["ciudad"], d["direccion"], d["codigo_postal"], cuenta["id"]))
        fila = db().execute("SELECT * FROM cuentas WHERE id = ?", (cuenta["id"],)).fetchone()
        return jsonify(_perfil(fila))

    @app.post("/cuenta/salir")
    def cuenta_salir():
        token = request.cookies.get(COOKIE_SESION)
        if token:
            db().execute("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(token),))
        resp = jsonify({"mensaje": "Sesión cerrada"})
        resp.delete_cookie(COOKIE_SESION, samesite="Lax")
        return resp

    @app.get("/cuenta/yo")
    def cuenta_yo():
        c = _cuenta_actual()
        return jsonify({"sesion": bool(c), "cuenta": _perfil(c) if c else None})

    # ------------------------------------------------------------ sección de administradores
    # Un administrador es una cuenta con rol 'admin'. Para crearla hay que conocer el código de
    # administrador (el mismo valor de ADMIN_KEY): así nadie se hace admin solo desde la web.
    def requiere_admin():
        c = _cuenta_actual()
        if not c:
            raise ErrorApi(401, "Inicia sesión como administrador para continuar")
        if c["rol"] != "admin":
            raise ErrorApi(403, "Esta sección es solo para administradores")
        g.nivel = "admin"          # así la auditoría queda registrada como acción de administrador
        return c

    @app.post("/admin/registro")
    def admin_registro():
        datos = json_del_cuerpo()
        nombre, email, clave, _ = _datos_registro(datos, cliente=False)
        llave = (request.remote_addr, "#codigo-admin")
        recientes = _frenar_si_abusa(llave)
        if not _coincide(_texto_cuenta(datos, "codigo"), app.config["ADMIN_KEY"]):
            intentos[llave] = recientes + [time.time()]
            raise ErrorApi(403, "El código de administrador no es correcto")
        intentos.pop(llave, None)
        cuenta_id = _crear_cuenta(nombre, email, clave, "admin")
        g.nivel = "admin"
        auditar(f"Nueva cuenta de administrador: {email}")
        resp = jsonify({"nombre": nombre, "email": email, "rol": "admin"})
        resp.status_code = 201
        return _abrir_sesion(cuenta_id, resp)

    @app.post("/admin/entrar")
    def admin_entrar():
        return _iniciar_sesion(solo_admin=True)

    @app.get("/admin/yo")
    def admin_yo():
        c = _cuenta_actual()
        return jsonify({"sesion": bool(c), "admin": bool(c and c["rol"] == "admin"),
                        "cuenta": {"nombre": c["nombre"], "email": c["email"]} if c else None})

    @app.get("/admin/catalogo/datos")
    def catalogo_datos():
        requiere_admin()
        arbol = arbol_categorias(solo_activos=False)
        creados = db().execute("""
            SELECT p.product_id, p.product_name, p.category, p.sub_category, p.precio, c.tono, s.icono
            FROM productos p
            LEFT JOIN categorias c ON c.nombre = p.category
            LEFT JOIN subcategorias s ON s.categoria = p.category AND s.nombre = p.sub_category
            WHERE p.precio IS NOT NULL ORDER BY p.rowid DESC LIMIT 50""").fetchall()
        return jsonify({
            "arbol": arbol,
            "totales": {"categorias": len(arbol), "subcategorias": sum(len(c["subcategorias"]) for c in arbol),
                        "productos": db().execute("SELECT COUNT(*) FROM productos WHERE activo = 1").fetchone()[0],
                        "ocultos": db().execute("SELECT COUNT(*) FROM productos WHERE activo = 0").fetchone()[0],
                        "creados": db().execute("SELECT COUNT(*) FROM productos WHERE precio IS NOT NULL").fetchone()[0]},
            "creados": [{"product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
                         "subcategoria": r["sub_category"], "precio": r["precio"], "tono": r["tono"],
                         "icono": r["icono"]} for r in creados]})

    @app.post("/admin/catalogo/categorias")
    def catalogo_categoria():
        cuenta = requiere_admin()
        datos = json_del_cuerpo()
        errores = []
        nombre = _texto(datos.get("nombre"), "nombre", errores, 40)
        if nombre is not None and len(nombre) < 2:
            errores.append("nombre: mínimo 2 caracteres")
        tono = datos.get("tono", "ofi")
        if tono not in TONOS:
            errores.append(f"tono: debe ser uno de {sorted(TONOS)}")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        if db().execute("SELECT 1 FROM categorias WHERE lower(nombre) = lower(?)", (nombre,)).fetchone():
            raise ErrorApi(409, f"Ya existe la categoría «{nombre}»")
        db().execute("INSERT INTO categorias (nombre, tono, creada) VALUES (?,?,?)", (nombre, tono, _ahora()))
        auditar(f"Categoría creada: {nombre} (por {cuenta['email']})")
        return jsonify({"categoria": nombre, "tono": tono, "mensaje": "Categoría creada"}), 201

    @app.post("/admin/catalogo/subcategorias")
    def catalogo_subcategoria():
        cuenta = requiere_admin()
        datos = json_del_cuerpo()
        errores = []
        categoria = _texto(datos.get("categoria"), "categoria", errores, 40)
        nombre = _texto(datos.get("nombre"), "nombre", errores, 40)
        if nombre is not None and len(nombre) < 2:
            errores.append("nombre: mínimo 2 caracteres")
        icono = datos.get("icono", "box")
        if icono not in ICONOS:
            errores.append(f"icono: debe ser uno de {sorted(ICONOS)}")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        if not db().execute("SELECT 1 FROM categorias WHERE nombre = ?", (categoria,)).fetchone():
            raise ErrorApi(422, "Datos inválidos", [f"categoria: no existe «{categoria}»"])
        if db().execute("SELECT 1 FROM subcategorias WHERE categoria = ? AND lower(nombre) = lower(?)",
                        (categoria, nombre)).fetchone():
            raise ErrorApi(409, f"«{categoria}» ya tiene una subcategoría «{nombre}»")
        db().execute("INSERT INTO subcategorias (categoria, nombre, icono) VALUES (?,?,?)", (categoria, nombre, icono))
        auditar(f"Subcategoría creada: {categoria} / {nombre} (por {cuenta['email']})")
        return jsonify({"categoria": categoria, "subcategoria": nombre, "icono": icono,
                        "mensaje": "Subcategoría creada"}), 201

    @app.post("/admin/catalogo/productos")
    def catalogo_producto():
        cuenta = requiere_admin()
        datos = json_del_cuerpo()
        errores = []
        nombre = _texto(datos.get("nombre"), "nombre", errores, 200)
        if nombre is not None and len(nombre) < 2:
            errores.append("nombre: mínimo 2 caracteres")
        categoria = _texto(datos.get("categoria"), "categoria", errores, 40)
        subcategoria = _texto(datos.get("subcategoria"), "subcategoria", errores, 40)
        precio = datos.get("precio")
        if not _es_numero(precio) or not 0 < precio <= PRECIO_MAXIMO:
            errores.append(f"precio: debe ser un número mayor que 0 y hasta {PRECIO_MAXIMO:,} (en USD)")
        stock = datos.get("stock")
        if not _es_entero(stock) or not 0 <= stock <= STOCK_MAXIMO:
            errores.append(f"stock: es obligatorio y debe ser un número entero entre 0 y {STOCK_MAXIMO:,}")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        if not db().execute("SELECT 1 FROM subcategorias WHERE categoria = ? AND nombre = ?",
                            (categoria, subcategoria)).fetchone():
            raise ErrorApi(422, "Datos inválidos",
                           [f"subcategoria: «{subcategoria}» no existe dentro de «{categoria}»"])
        if nombre_repetido(db(), categoria, nombre):
            raise ErrorApi(409, f"Ya hay un producto llamado «{nombre}» en «{categoria}»")
        pid = "ME-P-" + secrets.token_hex(3).upper()
        try:
            db().execute("INSERT INTO productos (product_id, product_name, category, sub_category, precio, stock) "
                         "VALUES (?,?,?,?,?,?)", (pid, nombre, categoria, subcategoria, round(float(precio), 2), stock))
        except sqlite3.IntegrityError as e:
            raise ErrorApi(422, "La base de datos rechazó el producto", [str(e)])
        auditar(f"Producto creado: {pid} / {nombre} · stock inicial {stock} (por {cuenta['email']})")
        return jsonify({"product_id": pid, "nombre": nombre, "categoria": categoria, "subcategoria": subcategoria,
                        "precio": round(float(precio), 2), "stock": stock, "mensaje": "Producto creado"}), 201

    # ---- Leer, editar y borrar productos (el CRUD completo del catálogo) --------------------------
    # Un producto se identifica con (product_id, nombre): en el dataset un mismo ID lo usan dos
    # productos. Si el ID es único basta con él; si no, hay que mandar ?nombre_actual=<nombre>.
    def _producto_de_ruta(product_id):
        sql, params = "SELECT * FROM productos WHERE product_id = ?", [product_id]
        if request.args.get("nombre_actual"):
            sql += " AND product_name = ?"
            params.append(request.args["nombre_actual"])
        filas = db().execute(sql, params).fetchall()
        if not filas:
            raise ErrorApi(404, f"No existe el producto {product_id}")
        if len(filas) > 1:
            raise ErrorApi(422, f"El código {product_id} lo usan {len(filas)} productos distintos",
                           ["Indica cuál con ?nombre_actual=<nombre exacto del producto>"])
        return filas[0]

    CONSULTA_ADMIN = f"""
        SELECT p.product_id, p.product_name, p.category, p.sub_category, p.activo, p.stock,
               p.precio AS precio_propio,
               COALESCE(p.precio, ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)) AS precio,
               COALESCE(SUM(d.quantity), 0) AS vendidos, COUNT(d.detalle_id) AS lineas,
               c.tono AS tono, s.icono AS icono
        {UNION_PRECIO} {{donde}} GROUP BY p.product_id, p.product_name, c.tono, s.icono"""

    def _json_producto_admin(r):
        return {"product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
                "subcategoria": r["sub_category"], "precio": r["precio"],
                "precio_propio": r["precio_propio"] is not None,   # False = precio deducido del historial
                "activo": bool(r["activo"]), "stock": r["stock"], "vendidos": r["vendidos"], "con_ventas": r["lineas"] > 0,
                "tono": r["tono"], "icono": r["icono"]}

    @app.get("/admin/catalogo/productos")
    def catalogo_productos_lista():
        """Lista paginada de TODOS los productos (también los ocultos). Filtros: q, categoria,
        subcategoria, estado (todos | activos | ocultos), orden (los mismos que la tienda)."""
        requiere_admin()
        pagina, limite, offset = _paginacion(20)
        filtros, params = [], []
        if request.args.get("categoria"):
            filtros.append("p.category = ?")
            params.append(request.args["categoria"])
        if request.args.get("subcategoria"):
            filtros.append("p.sub_category = ?")
            params.append(request.args["subcategoria"])
        if request.args.get("q"):
            filtros.append("p.product_name LIKE ? ESCAPE '\\'")
            params.append(_like(request.args["q"][:80]))
        estado = request.args.get("estado", "todos")
        if estado not in ("todos", "activos", "ocultos"):
            raise ErrorApi(400, "estado debe ser uno de ['activos', 'ocultos', 'todos']")
        if estado != "todos":
            filtros.append("p.activo = ?")
            params.append(1 if estado == "activos" else 0)
        orden = request.args.get("orden", "recientes")
        if orden not in ORDENES_TIENDA:
            raise ErrorApi(400, f"orden debe ser uno de {sorted(ORDENES_TIENDA)}")
        donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
        base = CONSULTA_ADMIN.format(donde=donde)
        total = db().execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]
        filas = db().execute(f"{base} ORDER BY {ORDENES_TIENDA[orden]} LIMIT ? OFFSET ?",
                             params + [limite, offset]).fetchall()
        return jsonify({"pagina": pagina, "limite": limite, "total": total,
                        "resultados": [_json_producto_admin(r) for r in filas]})

    @app.get("/admin/catalogo/productos/<product_id>")
    def catalogo_producto_ver(product_id):
        requiere_admin()
        p = _producto_de_ruta(product_id)
        fila = db().execute(CONSULTA_ADMIN.format(donde="WHERE p.product_id = ? AND p.product_name = ?"),
                            (p["product_id"], p["product_name"])).fetchone()
        return jsonify(_json_producto_admin(fila))

    @app.route("/admin/catalogo/productos/<product_id>", methods=["PUT", "PATCH"])
    def catalogo_producto_editar(product_id):
        """Cambia solo los campos que lleguen: nombre, categoria, subcategoria, precio (USD), activo y stock.
        Stock: `stock` fija la cantidad exacta; `ajuste_stock` suma (positivo) o resta (negativo) a lo que ya hay."""
        cuenta = requiere_admin()
        actual = _producto_de_ruta(product_id)
        datos = json_del_cuerpo()
        campos = {"nombre", "categoria", "subcategoria", "precio", "activo", "stock", "ajuste_stock"}
        errores = []
        if set(datos) - campos:
            errores.append(f"campos desconocidos: {sorted(set(datos) - campos)}")
        if not set(datos) & campos:
            errores.append(f"no hay nada que cambiar: envía al menos uno de {sorted(campos)}")
        nombre, categoria, subcategoria = actual["product_name"], actual["category"], actual["sub_category"]
        precio, activo = actual["precio"], actual["activo"]
        nuevo_stock, ajuste = actual["stock"], None
        if "nombre" in datos:
            v = _texto(datos["nombre"], "nombre", errores, 200)
            if v is not None and len(v) < 2:
                errores.append("nombre: mínimo 2 caracteres")
            elif v is not None:
                nombre = v
        if "categoria" in datos:
            categoria = _texto(datos["categoria"], "categoria", errores, 40) or categoria
        if "subcategoria" in datos:
            subcategoria = _texto(datos["subcategoria"], "subcategoria", errores, 40) or subcategoria
        if "precio" in datos:
            if not _es_numero(datos["precio"]) or not 0 < datos["precio"] <= PRECIO_MAXIMO:
                errores.append(f"precio: debe ser un número mayor que 0 y hasta {PRECIO_MAXIMO:,} (en USD)")
            else:
                precio = round(float(datos["precio"]), 2)
        if "activo" in datos:
            if not isinstance(datos["activo"], bool):
                errores.append("activo: debe ser true o false")
            else:
                activo = 1 if datos["activo"] else 0
        if "stock" in datos and "ajuste_stock" in datos:
            errores.append("stock y ajuste_stock: envía solo uno de los dos")
        elif "stock" in datos:
            if not _es_entero(datos["stock"]) or not 0 <= datos["stock"] <= STOCK_MAXIMO:
                errores.append(f"stock: debe ser un número entero entre 0 y {STOCK_MAXIMO:,}")
            else:
                nuevo_stock = datos["stock"]
        elif "ajuste_stock" in datos:
            a = datos["ajuste_stock"]
            if not _es_entero(a) or a == 0 or abs(a) > STOCK_MAXIMO:
                errores.append("ajuste_stock: debe ser un entero distinto de 0 (positivo agrega, negativo quita)")
            elif actual["stock"] + a < 0:
                errores.append(f"ajuste_stock: no puedes quitar {-a} unidades, solo hay {actual['stock']}")
            elif actual["stock"] + a > STOCK_MAXIMO:
                errores.append(f"ajuste_stock: el stock no puede pasar de {STOCK_MAXIMO:,} unidades")
            else:
                ajuste, nuevo_stock = a, actual["stock"] + a
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)

        conn = db()
        if (categoria, subcategoria) != (actual["category"], actual["sub_category"]) and not conn.execute(
                "SELECT 1 FROM subcategorias WHERE categoria = ? AND nombre = ?", (categoria, subcategoria)).fetchone():
            raise ErrorApi(422, "Datos inválidos",
                           [f"subcategoria: «{subcategoria}» no existe dentro de «{categoria}»"
                            + (" (si cambias la categoría, elige también una subcategoría de ella)"
                               if "subcategoria" not in datos else "")])
        if (nombre, categoria) != (actual["product_name"], actual["category"]) and nombre_repetido(
                conn, categoria, nombre, excepto=(actual["product_id"], actual["product_name"])):
            raise ErrorApi(409, f"Ya hay un producto llamado «{nombre}» en «{categoria}»")

        cambios = [f"{etq}: {antes} → {ahora}" for etq, antes, ahora in (
            ("nombre", actual["product_name"], nombre), ("categoría", actual["category"], categoria),
            ("subcategoría", actual["sub_category"], subcategoria),
            ("precio", actual["precio"], precio), ("activo", actual["activo"], activo),
            ("stock", actual["stock"], nuevo_stock)) if antes != ahora]
        lineas = 0
        conn.execute("BEGIN")
        try:
            if nombre != actual["product_name"]:
                # El nombre es parte de la llave compuesta que usan las líneas de pedido: se cambia en
                # ambas tablas dentro de la misma transacción y la base revisa las llaves al hacer COMMIT.
                conn.execute("PRAGMA defer_foreign_keys = ON")
                lineas = conn.execute("UPDATE detalle_pedido SET product_name = ? "
                                      "WHERE product_id = ? AND product_name = ?",
                                      (nombre, actual["product_id"], actual["product_name"])).rowcount
            # Un ajuste se aplica en la propia base (stock = stock + n): si dos admins ajustan a la vez, no se pisan.
            stock_sql = "stock + ?" if ajuste is not None else "?"
            conn.execute(f"UPDATE productos SET product_name = ?, category = ?, sub_category = ?, precio = ?, "
                         f"activo = ?, stock = {stock_sql} WHERE product_id = ? AND product_name = ?",
                         (nombre, categoria, subcategoria, precio, activo,
                          ajuste if ajuste is not None else nuevo_stock, actual["product_id"], actual["product_name"]))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK")
            raise ErrorApi(409, "La base de datos rechazó el cambio; no se guardó nada", [str(e)])
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if cambios:
            auditar(f"Producto editado: {actual['product_id']} · " + "; ".join(cambios) + f" (por {cuenta['email']})")
        fila = conn.execute(CONSULTA_ADMIN.format(donde="WHERE p.product_id = ? AND p.product_name = ?"),
                            (actual["product_id"], nombre)).fetchone()
        d = _json_producto_admin(fila)
        d["mensaje"] = "Producto actualizado" if cambios else "No había cambios que guardar"
        d["lineas_de_pedido_actualizadas"] = lineas
        return jsonify(d)

    @app.delete("/admin/catalogo/productos/<product_id>")
    def catalogo_producto_borrar(product_id):
        """Borra el producto. Si ya tiene ventas NO se borra (rompería el historial): se oculta de la tienda."""
        cuenta = requiere_admin()
        p = _producto_de_ruta(product_id)
        conn = db()

        def ocultar():
            conn.execute("UPDATE productos SET activo = 0 WHERE product_id = ? AND product_name = ?",
                         (p["product_id"], p["product_name"]))
            auditar(f"Producto ocultado (tiene ventas): {p['product_id']} / {p['product_name']} (por {cuenta['email']})")
            return jsonify({"resultado": "oculto", "product_id": p["product_id"], "nombre": p["product_name"],
                            "mensaje": "Tiene ventas registradas, así que no se borró: quedó oculto en la tienda "
                                       "y se puede volver a mostrar cuando quieras"})

        ventas = conn.execute("SELECT COUNT(*) FROM detalle_pedido WHERE product_id = ? AND product_name = ?",
                              (p["product_id"], p["product_name"])).fetchone()[0]
        if ventas:
            return ocultar()
        try:
            conn.execute("DELETE FROM productos WHERE product_id = ? AND product_name = ?",
                         (p["product_id"], p["product_name"]))
        except sqlite3.IntegrityError:        # alguien lo vendió justo ahora: la base no deja borrarlo
            return ocultar()
        auditar(f"Producto eliminado: {p['product_id']} / {p['product_name']} (por {cuenta['email']})")
        return jsonify({"resultado": "eliminado", "product_id": p["product_id"], "nombre": p["product_name"],
                        "mensaje": "Producto eliminado"})

    @app.get("/tienda/resumen")
    def tienda_resumen():
        f = db().execute("""SELECT (SELECT COUNT(*) FROM productos WHERE activo = 1) AS productos,
                                   (SELECT COUNT(*) FROM categorias) AS categorias,
                                   (SELECT COUNT(*) FROM pedidos) AS pedidos,
                                   (SELECT MAX(discount) FROM detalle_pedido) AS descuento_max""").fetchone()
        return jsonify(dict(f))

    # ------------------------------------------------------------ general
    @app.get("/salud")
    def salud():
        return jsonify({"estado": "ok", "servicio": "Mercado Express API"})

    @app.get("/resumen")
    def resumen():
        f = db().execute("""
            SELECT (SELECT COUNT(*) FROM clientes)       AS clientes,
                   (SELECT COUNT(*) FROM productos)      AS productos,
                   (SELECT COUNT(*) FROM pedidos)        AS pedidos,
                   (SELECT COUNT(*) FROM detalle_pedido) AS lineas_de_pedido,
                   (SELECT ROUND(SUM(sales), 2)  FROM detalle_pedido) AS ventas_totales,
                   (SELECT ROUND(SUM(profit), 2) FROM detalle_pedido) AS ganancia_total
        """).fetchone()
        return jsonify(dict(f))

    # ------------------------------------------------------------ clientes
    @app.get("/clientes")
    def listar_clientes():
        pagina, limite, offset = _paginacion()
        completo = datos_completos()
        filtros, params = [], []
        if request.args.get("region"):
            filtros.append("region = ?")
            params.append(request.args["region"])
        if request.args.get("segmento"):
            filtros.append("segment = ?")
            params.append(request.args["segmento"])
        if request.args.get("q"):
            filtros.append("customer_name LIKE ? ESCAPE '\\'")
            params.append(_like(request.args["q"]))
        donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
        total = db().execute(f"SELECT COUNT(*) FROM clientes {donde}", params).fetchone()[0]
        filas = db().execute(
            f"SELECT * FROM clientes {donde} ORDER BY customer_id LIMIT ? OFFSET ?",
            params + [limite, offset]).fetchall()
        if completo:
            auditar(f"Listado completo de clientes (página {pagina}, {len(filas)} registros)")
        return jsonify({"pagina": pagina, "limite": limite, "total": total,
                        "resultados": [_json_cliente(r, completo) for r in filas]})

    @app.get("/clientes/<customer_id>")
    def ver_cliente(customer_id):
        completo = datos_completos()
        fila = db().execute("SELECT * FROM clientes WHERE customer_id = ?", (customer_id,)).fetchone()
        if not fila:
            raise ErrorApi(404, f"No existe el cliente {customer_id}")
        n_pedidos = db().execute(
            "SELECT COUNT(*) FROM pedidos WHERE customer_id = ?", (customer_id,)).fetchone()[0]
        if completo:
            auditar(f"Datos completos del cliente {customer_id}")
        d = _json_cliente(fila, completo)
        d["total_pedidos"] = n_pedidos
        return jsonify(d)

    @app.post("/clientes")
    def crear_cliente():
        datos = json_del_cuerpo()
        errores = []
        nombre = _texto(datos.get("nombre"), "nombre", errores)
        pais = _texto(datos.get("pais"), "pais", errores, 60)
        ciudad = _texto(datos.get("ciudad"), "ciudad", errores, 60)
        estado = _texto(datos.get("estado"), "estado", errores, 60)
        cp = _texto(datos.get("codigo_postal"), "codigo_postal", errores, 12)
        if datos.get("segmento") not in SEGMENTOS:
            errores.append(f"segmento: debe ser uno de {sorted(SEGMENTOS)}")
        if datos.get("region") not in REGIONES:
            errores.append(f"region: debe ser una de {sorted(REGIONES)}")
        # Ley 1581: sin autorización expresa del titular no se guardan sus datos.
        if datos.get("autorizacion_datos") is not True:
            errores.append("autorizacion_datos: debe ser true (el cliente debe autorizar "
                           "el tratamiento de sus datos personales, Ley 1581 de 2012)")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)

        nuevo_id = "ME-C-" + secrets.token_hex(3).upper()
        try:
            db().execute(
                "INSERT INTO clientes (customer_id, customer_name, segment, country, city, state, "
                "postal_code, region, autorizacion_datos, fecha_autorizacion) "
                "VALUES (?,?,?,?,?,?,?,?,1,?)",
                (nuevo_id, nombre, datos["segmento"], pais, ciudad, estado, cp, datos["region"],
                 datetime.now().isoformat(timespec="seconds")))
        except sqlite3.IntegrityError as e:
            raise ErrorApi(422, "La base de datos rechazó el cliente", [str(e)])
        return jsonify({"customer_id": nuevo_id, "mensaje": "Cliente creado"}), 201

    # ------------------------------------------------------------ productos
    @app.get("/productos")
    def listar_productos():
        pagina, limite, offset = _paginacion()
        filtros, params = [], []
        if request.args.get("categoria"):
            filtros.append("category = ?")
            params.append(request.args["categoria"])
        if request.args.get("q"):
            filtros.append("product_name LIKE ? ESCAPE '\\'")
            params.append(_like(request.args["q"]))
        donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
        total = db().execute(f"SELECT COUNT(*) FROM productos {donde}", params).fetchone()[0]
        filas = db().execute(
            f"SELECT * FROM productos {donde} ORDER BY product_id, product_name LIMIT ? OFFSET ?",
            params + [limite, offset]).fetchall()
        return jsonify({"pagina": pagina, "limite": limite, "total": total, "resultados": [
            {"product_id": r["product_id"], "nombre": r["product_name"],
             "categoria": r["category"], "subcategoria": r["sub_category"]} for r in filas]})

    # ------------------------------------------------------------ pedidos
    @app.get("/pedidos")
    def listar_pedidos():
        pagina, limite, offset = _paginacion()
        filtros, params = [], []
        if request.args.get("cliente_id"):
            filtros.append("customer_id = ?")
            params.append(request.args["cliente_id"])
        donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
        total = db().execute(f"SELECT COUNT(*) FROM pedidos {donde}", params).fetchone()[0]
        filas = db().execute(
            f"SELECT * FROM pedidos {donde} ORDER BY order_date DESC, order_id LIMIT ? OFFSET ?",
            params + [limite, offset]).fetchall()
        return jsonify({"pagina": pagina, "limite": limite, "total": total, "resultados": [
            {"order_id": r["order_id"], "fecha_pedido": r["order_date"], "fecha_envio": r["ship_date"],
             "modo_envio": r["ship_mode"], "customer_id": r["customer_id"]} for r in filas]})

    @app.get("/pedidos/<order_id>")
    def ver_pedido(order_id):
        p = db().execute("SELECT * FROM pedidos WHERE order_id = ?", (order_id,)).fetchone()
        if not p:
            raise ErrorApi(404, f"No existe el pedido {order_id}")
        lineas = db().execute(
            "SELECT * FROM detalle_pedido WHERE order_id = ? ORDER BY detalle_id", (order_id,)).fetchall()
        items = [{"product_id": r["product_id"], "producto": r["product_name"], "ventas": r["sales"],
                  "cantidad": r["quantity"], "descuento": r["discount"], "ganancia": r["profit"]}
                 for r in lineas]
        return jsonify({
            "order_id": p["order_id"], "fecha_pedido": p["order_date"], "fecha_envio": p["ship_date"],
            "modo_envio": p["ship_mode"], "customer_id": p["customer_id"], "items": items,
            "total_ventas": round(sum(i["ventas"] for i in items), 2)})

    def validar_pedido(datos):
        """Primera barrera (rápida): revisa lo que se puede revisar sin tocar la base."""
        errores = []
        customer_id = _texto(datos.get("customer_id"), "customer_id", errores, 40)
        if datos.get("modo_envio") not in MODOS_ENVIO:
            errores.append(f"modo_envio: debe ser uno de {sorted(MODOS_ENVIO)}")
        hoy = date.today().isoformat()
        fecha_pedido = _fecha(datos.get("fecha_pedido", hoy), "fecha_pedido", errores)
        fecha_envio = None
        if datos.get("fecha_envio") is not None:
            fecha_envio = _fecha(datos["fecha_envio"], "fecha_envio", errores)
            if fecha_envio and fecha_pedido and fecha_envio < fecha_pedido:
                errores.append("fecha_envio: no puede ser anterior a fecha_pedido")
        order_id = None
        if datos.get("order_id") is not None:
            order_id = _texto(datos["order_id"], "order_id", errores, 40)

        items_limpios = []
        items = datos.get("items")
        if not isinstance(items, list) or not items:
            errores.append("items: debe ser una lista con al menos un producto")
        elif len(items) > 100:
            errores.append("items: máximo 100 productos por pedido")
        else:
            for n, it in enumerate(items, start=1):
                pref = f"items[{n}]"
                if not isinstance(it, dict):
                    errores.append(f"{pref}: debe ser un objeto")
                    continue
                pid = _texto(it.get("product_id"), f"{pref}.product_id", errores, 40)
                pnombre = _texto(it.get("product_name"), f"{pref}.product_name", errores, 250)
                ventas, cant = it.get("sales"), it.get("quantity")
                desc, ganancia = it.get("discount", 0), it.get("profit")
                if not _es_numero(ventas) or ventas <= 0:
                    errores.append(f"{pref}.sales: debe ser un número mayor que 0")
                if not isinstance(cant, int) or isinstance(cant, bool) or cant <= 0:
                    errores.append(f"{pref}.quantity: debe ser un entero mayor que 0")
                if not _es_numero(desc) or not 0 <= desc <= 1:
                    errores.append(f"{pref}.discount: debe estar entre 0 y 1")
                if ganancia is not None and not _es_numero(ganancia):
                    errores.append(f"{pref}.profit: debe ser un número")
                items_limpios.append((pid, pnombre, ventas, cant, desc, ganancia))
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        return customer_id, datos["modo_envio"], fecha_pedido, fecha_envio, order_id, items_limpios

    @app.post("/pedidos")
    def crear_pedido():
        datos = json_del_cuerpo()
        customer_id, modo, f_pedido, f_envio, order_id, items = validar_pedido(datos)
        conn = db()

        # Comprobaciones amables antes de abrir la transacción
        if not conn.execute("SELECT 1 FROM clientes WHERE customer_id = ?", (customer_id,)).fetchone():
            raise ErrorApi(422, f"El cliente {customer_id} no existe")
        faltan = [f"{pid} / {nom}" for pid, nom, *_ in items if not conn.execute(
            "SELECT 1 FROM productos WHERE product_id = ? AND product_name = ?", (pid, nom)).fetchone()]
        if faltan:
            raise ErrorApi(422, "Hay productos que no existen en el catálogo", faltan)
        ocultos = [f"{pid} / {nom}" for pid, nom, *_ in items if not conn.execute(
            "SELECT 1 FROM productos WHERE product_id = ? AND product_name = ? AND activo = 1", (pid, nom)).fetchone()]
        if ocultos:
            raise ErrorApi(422, "Hay productos que ya no están a la venta", ocultos)

        if order_id is None:
            order_id = f"ME-{f_pedido[:4]}-{secrets.token_hex(3).upper()}"

        # Transacción: el pedido y todas sus líneas se guardan juntos o no se guarda nada.
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO pedidos (order_id, order_date, ship_date, ship_mode, customer_id) "
                "VALUES (?,?,?,?,?)", (order_id, f_pedido, f_envio, modo, customer_id))
            for pid, nom, ventas, cant, desc, ganancia in items:
                conn.execute(
                    "INSERT INTO detalle_pedido (order_id, product_id, product_name, sales, quantity, "
                    "discount, profit) VALUES (?,?,?,?,?,?,?)",
                    (order_id, pid, nom, ventas, cant, desc, ganancia))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK")  # no queda ningún pedido "fantasma"
            if "UNIQUE" in str(e).upper() or "PRIMARY KEY" in str(e).upper():
                raise ErrorApi(409, f"Ya existe un pedido con id {order_id}")
            raise ErrorApi(422, "La base de datos rechazó el pedido; no se guardó nada", [str(e)])
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"order_id": order_id, "lineas_guardadas": len(items),
                        "mensaje": "Pedido registrado"}), 201

    # ------------------------------------------------------------ reportes
    @app.get("/reportes/ventas-por-categoria")
    def rep_categorias():
        filas = db().execute("""
            SELECT p.category, p.sub_category, ROUND(SUM(d.sales), 2) AS total_ventas
            FROM detalle_pedido d
            JOIN productos p ON d.product_id = p.product_id AND d.product_name = p.product_name
            GROUP BY p.category, p.sub_category
            ORDER BY total_ventas DESC
            LIMIT ?""", (_limite_reporte(),)).fetchall()
        return jsonify([{"categoria": r["category"], "subcategoria": r["sub_category"],
                         "total_ventas": r["total_ventas"]} for r in filas])

    @app.get("/reportes/ganancia-por-cliente")
    def rep_clientes():
        completo = datos_completos()
        filas = db().execute("""
            SELECT c.customer_id, c.customer_name, ROUND(SUM(d.profit), 2) AS ganancia_total
            FROM detalle_pedido d
            JOIN pedidos pe ON d.order_id = pe.order_id
            JOIN clientes c ON pe.customer_id = c.customer_id
            GROUP BY c.customer_id, c.customer_name
            ORDER BY ganancia_total DESC
            LIMIT ?""", (_limite_reporte(),)).fetchall()
        if completo:
            auditar(f"Reporte ganancia por cliente con nombres completos ({len(filas)} filas)")
        return jsonify([{"customer_id": r["customer_id"],
                         "nombre": r["customer_name"] if completo else enmascarar_nombre(r["customer_name"]),
                         "ganancia_total": r["ganancia_total"]} for r in filas])

    @app.get("/reportes/envio-por-region")
    def rep_envios():
        filas = db().execute("""
            SELECT pe.ship_mode, c.region,
                   ROUND(AVG(julianday(pe.ship_date) - julianday(pe.order_date)), 2) AS dias_promedio_envio
            FROM pedidos pe
            JOIN clientes c ON pe.customer_id = c.customer_id
            GROUP BY pe.ship_mode, c.region
            ORDER BY dias_promedio_envio DESC""").fetchall()
        return jsonify([{"modo_envio": r["ship_mode"], "region": r["region"],
                         "dias_promedio_envio": r["dias_promedio_envio"]} for r in filas])

    @app.get("/admin/auditoria")
    def ver_auditoria():
        if g.nivel != "admin":
            raise ErrorApi(403, "Solo el administrador puede ver la auditoría")
        filas = db().execute("SELECT * FROM auditoria ORDER BY id DESC LIMIT 100").fetchall()
        return jsonify([dict(r) for r in filas])

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
