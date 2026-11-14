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
import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from cargar_datos import CSV_POR_DEFECTO, cargar
from colas import analizar, mensaje
from envio import cotizar
from pasarela import cobrar, validar_tarjeta
from esquema import COLUMNAS_PEDIDO, COLUMNAS_PERFIL, COLUMNAS_SERVICIO, SCHEMA
from ubicaciones import CODIGO_POSTAL, CODIGO_POSTAL_GENERAL, UBICACIONES, como_lista

MODOS_ENVIO = {"Standard Class", "Second Class", "First Class", "Same Day"}
SEGMENTOS = {"Consumer", "Corporate", "Home Office"}
REGIONES = {"East", "West", "Central", "South"}
# Rutas sin clave: las páginas (no traen datos) y el catálogo de la tienda (sin datos personales).
RUTAS_PUBLICAS = {"salud", "favicon", "pagina_tienda", "pagina_admin", "pagina_catalogo",
                  "tienda_productos", "tienda_producto_detalle", "tienda_categorias", "tienda_resumen", "tienda_config",
                  "tienda_ubicaciones", "tienda_envio", "cuenta_registro", "cuenta_entrar", "cuenta_salir", "cuenta_yo",
                  "cuenta_direccion", "cuenta_perfil", "cuenta_pago", "cuenta_pedido", "cuenta_pedidos",
                  "cuenta_pedido_cancelar", "cuenta_pedido_modificar",
                  "turnos_estado", "pedir_turno", "turnos_motivos", "pagina_turnos", "llamar_turno", "finalizar_turno",
                  "admin_turnos_lista", "configurar_servicio",
                  "editar_categoria", "editar_subcategoria",
                  # Sección de administradores: no usan X-API-Key, usan cuenta con rol admin + sesión.
                  # Las que cambian datos llaman a requiere_admin() y rechazan a quien no lo sea.
                  "admin_registro", "admin_entrar", "admin_salir", "admin_yo",
                  "catalogo_datos", "catalogo_categoria", "catalogo_subcategoria", "catalogo_producto",
                  "catalogo_productos_lista", "catalogo_producto_ver", "catalogo_producto_editar",
                  "catalogo_producto_borrar", "catalogo_descuento_general"}

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

MOTIVOS_TURNO = {
    "Recogida de Pedidos web": [
        "Estado de mi pedido",
        "Cambiar o cancelar un pedido",
        "Garantía o devolución",
        "Problema con un pago",
        "Información de un producto",
        "Otra consulta",
    ],
    "Garantías y Devoluciones": [
        "Garantía o devolución",
        "Estado de mi pedido",
        "Problema con un pago",
        "Otra consulta",
    ],
}
# Todos los motivos únicos (compatibilidad hacia atrás con turnos ya guardados)
TODOS_LOS_MOTIVOS = sorted(set(m for ms in MOTIVOS_TURNO.values() for m in ms))

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,185}\.[^@\s]{2,}$")
COOKIE_SESION = "me_sesion"                 # sesión del cliente de la tienda
COOKIE_SESION_ADMIN = "me_sesion_admin"     # sesión del administrador (independiente de la del cliente)
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

    # Bases creadas con la primera versión de los turnos: les faltan columnas de capacidad.
    cols_servicio = {f[1] for f in conn.execute("PRAGMA table_info(turnos_servicios)")}
    for columna, definicion in COLUMNAS_SERVICIO.items():
        if columna not in cols_servicio:
            conn.execute(f"ALTER TABLE turnos_servicios ADD COLUMN {columna} {definicion}")
    if not conn.execute("SELECT 1 FROM turnos_servicios").fetchone():
        conn.executemany("INSERT INTO turnos_servicios (nombre) VALUES (?)",
                         [("Recogida de Pedidos web",), ("Garantías y Devoluciones",)])

    cols = lambda tabla: {f[1] for f in conn.execute(f"PRAGMA table_info({tabla})")}
    if "precio" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN precio REAL CHECK (precio IS NULL OR precio > 0)")
    if "activo" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1))")
    if "stock" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)")
    for col_prod in ("marca", "descripcion", "ficha"):
        if col_prod not in cols("productos"):
            conn.execute(f"ALTER TABLE productos ADD COLUMN {col_prod} TEXT")
    if "descuento" not in cols("productos"):
        conn.execute("ALTER TABLE productos ADD COLUMN descuento REAL NOT NULL DEFAULT 0 CHECK (descuento >= 0 AND descuento <= 100)")
    conn.execute("CREATE TABLE IF NOT EXISTS configuracion (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO configuracion (clave, valor) VALUES ('descuento_general', '0')")
    if "cuenta_id" not in cols("turnos"):
        conn.execute("ALTER TABLE turnos ADD COLUMN cuenta_id INTEGER REFERENCES cuentas(id)")
    # Motivo del turno (Tarea 1): bases que ya tenían la tabla sin estas columnas
    for col_turno in ("motivo", "detalle", "pedido_id"):
        if col_turno not in cols("turnos"):
            conn.execute(f"ALTER TABLE turnos ADD COLUMN {col_turno} TEXT")
    if "rol" not in cols("cuentas"):
        conn.execute("ALTER TABLE cuentas ADD COLUMN rol TEXT NOT NULL DEFAULT 'cliente' "
                     "CHECK (rol IN ('cliente','admin'))")
    for columna, definicion in COLUMNAS_PERFIL.items():     # apellido, teléfono y dirección
        if columna not in cols("cuentas"):
            conn.execute(f"ALTER TABLE cuentas ADD COLUMN {columna} {definicion}")
    for columna, definicion in COLUMNAS_PEDIDO.items():     # creado, envio_cop, cancelado_en
        if columna not in cols("pedidos"):
            conn.execute(f"ALTER TABLE pedidos ADD COLUMN {columna} {definicion}")
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


def _deserializar_ficha(valor_db):
    """Devuelve la ficha técnica como lista de objetos {'atributo': ..., 'valor': ...}."""
    if not valor_db:
        return []
    if isinstance(valor_db, list):
        return valor_db
    try:
        data = json.loads(valor_db)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _validar_ficha(valor, errores):
    """Valida que ficha sea una lista de máx. 20 pares atributo/valor de textos cortos."""
    if valor is None:
        return None
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except Exception:
            errores.append("ficha: debe ser una lista de pares atributo/valor o un JSON válido")
            return None
    if not isinstance(valor, list):
        errores.append("ficha: debe ser una lista")
        return None
    if len(valor) > 20:
        errores.append("ficha: máximo 20 pares de atributos")
        return None
    resultado = []
    for i, item in enumerate(valor):
        if isinstance(item, dict):
            attr = item.get("atributo")
            if attr is None:
                attr = item.get("nombre")
            if attr is None:
                attr = item.get("clave")
            val = item.get("valor")
            if attr is None or val is None:
                errores.append(f"ficha[{i}]: debe tener 'atributo' y 'valor'")
                continue
        elif isinstance(item, (list, tuple)):
            if len(item) != 2:
                errores.append(f"ficha[{i}]: debe ser un par [atributo, valor]")
                continue
            attr, val = item[0], item[1]
        else:
            errores.append(f"ficha[{i}]: formato inválido (debe ser un par atributo/valor)")
            continue
        if not isinstance(attr, str) or not isinstance(val, str):
            errores.append(f"ficha[{i}]: atributo y valor deben ser texto")
            continue
        attr = attr.strip()
        val = val.strip()
        if not attr or not val:
            errores.append(f"ficha[{i}]: atributo y valor no pueden estar vacíos")
            continue
        if len(attr) > 100 or len(val) > 200:
            errores.append(f"ficha[{i}]: textos demasiado largos (atributo máx. 100, valor máx. 200 caracteres)")
            continue
        resultado.append({"atributo": attr, "valor": val})
    return resultado if not any("ficha" in e for e in errores) else None


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
        # Por defecto la base nace vacía. Para cargar el dataset de Superstore: CARGAR_DATASET=1
        CARGAR_DATASET=os.environ.get("CARGAR_DATASET") == "1",
        # Límites de los turnos: por dispositivo (ip) y por fila
        TURNOS_MAX_POR_IP=10, TURNOS_VENTANA_SEG=60, TURNOS_MAX_FILA=100,
        API_KEY=os.environ.get("API_KEY"),
        ADMIN_KEY=os.environ.get("ADMIN_KEY"),
        # Tasa de referencia para mostrar precios en COP (octubre de 2026: ~3.200 a 3.300).
        # Se cambia con la variable de entorno TASA_COP, sin tocar el código.
        TASA_COP=float(os.environ.get("TASA_COP", "3250")),
        # Minutos después de comprar en los que el cliente aún puede cancelar o modificar su pedido (24 horas por defecto).
        PEDIDO_EDITABLE_MIN=int(os.environ.get("PEDIDO_EDITABLE_MIN", "1440")),
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
        if app.config["CARGAR_DATASET"]:
            conteos = cargar(app.config["CSV_PATH"], app.config["DB_PATH"])
            print(f"[INFO] Base creada con el dataset: {conteos}", flush=True)
        else:
            print("[INFO] Base nueva y vacía (las tablas se crean enseguida).", flush=True)
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
    def proteger_csrf():
        """Defensa CSRF: una petición que cambia datos (POST/PUT/PATCH/DELETE) hecha desde un navegador
        solo se acepta si viene de esta misma página. Los navegadores siempre envían Origin (o al menos
        Sec-Fetch-Site) en esas peticiones, así que otra web no puede usar la sesión del usuario.
        Los clientes sin navegador (curl, pruebas) no envían esos datos y no tienen cookie que robar."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origen = request.headers.get("Origin")
        if origen is not None and urlsplit(origen).netloc != request.host:
            raise ErrorApi(403, "Petición rechazada: el origen no coincide con el sitio")
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise ErrorApi(403, "Petición rechazada: viene de otro sitio")
        return None

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

    def _descuento_general(conn=None):
        try:
            c = conn or db()
            row = c.execute("SELECT valor FROM configuracion WHERE clave = 'descuento_general'").fetchone()
            return float(row["valor"]) if row and row["valor"] else 0.0
        except Exception:
            return 0.0

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
        desc_gen = _descuento_general()
        filas = db().execute(f"""
            SELECT p.product_id, p.product_name, p.category, p.sub_category,
                   p.marca, p.descripcion, p.ficha, COALESCE(p.descuento, 0) AS descuento,
                   COALESCE(p.precio, ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)) AS precio,
                   COALESCE(SUM(d.quantity), 0) AS vendidos,
                   COALESCE(MAX(d.discount), 0) AS descuento_max, p.stock AS stock,
                   c.tono AS tono, s.icono AS icono
            {base} ORDER BY {ORDENES_TIENDA[orden]} LIMIT ? OFFSET ?""", params + [limite, offset]).fetchall()
        resultados = []
        for r in filas:
            p_desc = float(r["descuento"] or 0)
            efectivo = max(p_desc, desc_gen)
            p_orig = r["precio"]
            p_fin = round(p_orig * (1.0 - efectivo / 100.0), 2) if p_orig is not None else None
            resultados.append({
                "product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
                "subcategoria": r["sub_category"], "precio": p_orig, "precio_final": p_fin,
                "descuento": p_desc, "descuento_general": desc_gen, "descuento_efectivo": efectivo,
                "vendidos": r["vendidos"],
                "descuento_max": max(r["descuento_max"] or 0, round(efectivo / 100.0, 2)),
                "tono": r["tono"], "icono": r["icono"],
                "marca": r["marca"], "descripcion": r["descripcion"], "ficha": _deserializar_ficha(r["ficha"]),
                # El stock exacto solo se publica cuando ya quedan pocas unidades; con más, va null
                # (así la tienda no regala el inventario completo a quien consulte la API).
                "stock": r["stock"] if r["stock"] <= STOCK_AVISO else None,
                "stock_nivel": nivel_stock(r["stock"])
            })
        return jsonify({"pagina": pagina, "limite": limite, "total": total, "descuento_general": desc_gen, "resultados": resultados})

    @app.get("/tienda/productos/<product_id>")
    def tienda_producto_detalle(product_id):
        desc_gen = _descuento_general()
        sql = f"""
            SELECT p.product_id, p.product_name, p.category, p.sub_category,
                   p.marca, p.descripcion, p.ficha, COALESCE(p.descuento, 0) AS descuento,
                   COALESCE(p.precio, ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)) AS precio,
                   COALESCE(SUM(d.quantity), 0) AS vendidos,
                   COALESCE(MAX(d.discount), 0) AS descuento_max, p.stock AS stock,
                   c.tono AS tono, s.icono AS icono
            {UNION_PRECIO} WHERE p.product_id = ? AND p.activo = 1
            GROUP BY p.product_id, p.product_name, c.tono, s.icono"""
        filas = db().execute(sql, (product_id,)).fetchall()
        if not filas:
            raise ErrorApi(404, f"Producto no encontrado: {product_id}")
        r = filas[0]
        p_desc = float(r["descuento"] or 0)
        efectivo = max(p_desc, desc_gen)
        p_orig = r["precio"]
        p_fin = round(p_orig * (1.0 - efectivo / 100.0), 2) if p_orig is not None else None
        return jsonify({
            "product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
            "subcategoria": r["sub_category"], "precio": p_orig, "precio_final": p_fin,
            "descuento": p_desc, "descuento_general": desc_gen, "descuento_efectivo": efectivo,
            "vendidos": r["vendidos"],
            "descuento_max": max(r["descuento_max"] or 0, round(efectivo / 100.0, 2)),
            "tono": r["tono"], "icono": r["icono"],
            "marca": r["marca"], "descripcion": r["descripcion"], "ficha": _deserializar_ficha(r["ficha"]),
            "stock": r["stock"] if r["stock"] <= STOCK_AVISO else None,
            "stock_nivel": nivel_stock(r["stock"])
        })

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
        return jsonify({"monedas": ["USD", "COP"], "tasa_cop": app.config["TASA_COP"], "descuento_general": _descuento_general()})

    # ------------------------------------------------------------ cuentas de cliente (tienda)
    intentos = {}                                   # (ip, correo) -> momentos de intentos fallidos
    hash_falso = generate_password_hash(secrets.token_urlsafe(16))  # al azar en cada arranque; para que "correo no existe" tarde lo mismo

    def _hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _ahora():
        return datetime.now().isoformat(timespec="seconds")

    def _abrir_sesion(cuenta_id, respuesta, admin=False):
        """Abre la sesión en la cookie de su tipo: así entrar como admin no cierra la sesión del cliente (ni al revés)."""
        conn = db()
        conn.execute("DELETE FROM sesiones WHERE expira < ?", (_ahora(),))   # limpieza
        token = secrets.token_urlsafe(32)
        expira = (datetime.now() + timedelta(seconds=DURACION_SESION)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO sesiones (token_hash, cuenta_id, expira) VALUES (?,?,?)",
                     (_hash_token(token), cuenta_id, expira))
        seguro = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        respuesta.set_cookie(COOKIE_SESION_ADMIN if admin else COOKIE_SESION, token, max_age=DURACION_SESION,
                             httponly=True, samesite="Lax", secure=seguro)
        return respuesta

    def _cuenta_de_sesion(cookie, rol):
        """La cuenta de la sesión guardada en esa cookie, solo si tiene el rol esperado (cliente o admin)."""
        token = request.cookies.get(cookie)
        if not token:
            return None
        fila = db().execute(
            "SELECT c.*, s.expira FROM sesiones s JOIN cuentas c ON c.id = s.cuenta_id "
            "WHERE s.token_hash = ? AND c.rol = ?", (_hash_token(token), rol)).fetchone()
        return fila if fila and fila["expira"] >= _ahora() else None

    def _cuenta_actual():
        """El cliente con sesión en la tienda. Una sesión de administrador no cuenta aquí."""
        return _cuenta_de_sesion(COOKIE_SESION, "cliente")

    def _admin_actual():
        """El administrador con sesión. Una sesión de cliente no cuenta aquí."""
        return _cuenta_de_sesion(COOKIE_SESION_ADMIN, "admin")

    def _cerrar_sesion(cookie, respuesta):
        token = request.cookies.get(cookie)
        if token:
            db().execute("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(token),))
        respuesta.delete_cookie(cookie, samesite="Lax")
        return respuesta

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
        # Mismo mensaje si el correo no existe, la clave falla o la cuenta no es del tipo de este formulario
        # (la tienda solo recibe clientes y el panel solo administradores): no se filtra cuál fue.
        if not fila or not correcta or (fila["rol"] == "admin") != solo_admin:
            intentos[llave] = recientes + [time.time()]
            raise ErrorApi(401, "Correo o contraseña incorrectos")
        intentos.pop(llave, None)
        return _abrir_sesion(fila["id"], jsonify(_perfil(fila)), admin=solo_admin)

    @app.get("/tienda/ubicaciones")
    def tienda_ubicaciones():
        return jsonify(como_lista())

    @app.get("/tienda/envio")
    def tienda_envio():
        """Opciones de envío (costo, despacho y llegada estimada) para una dirección y el subtotal del carrito."""
        pais = request.args.get("pais", "")
        if pais not in UBICACIONES:
            raise ErrorApi(422, "pais: elige un país de la lista")
        try:
            subtotal = int(request.args.get("subtotal_cop", "0"))
        except ValueError:
            raise ErrorApi(422, "subtotal_cop: debe ser un número entero")
        if not 0 <= subtotal <= 10**10:
            raise ErrorApi(422, "subtotal_cop: fuera de rango")
        return jsonify(cotizar(pais, request.args.get("region", "")[:60], request.args.get("ciudad", "")[:80], subtotal))

    # ------------------------------------------------------------ compra: pago (pasarela simulada) + pedido
    # Opción de envío de la tienda -> modo que se guarda en pedidos.ship_mode (cámbialo si prefieres otro).
    MODO_POR_OPCION = {"mismo_dia": "Same Day", "express": "First Class",
                       "estandar": "Standard Class", "internacional": "Second Class"}
    OPCION_POR_MODO = {v: k for k, v in MODO_POR_OPCION.items()}
    PAGO_VALIDO_SEG = 15 * 60       # un pago aprobado se puede usar en un pedido durante 15 minutos

    def _cuenta_cliente():
        cuenta = _cuenta_actual()
        if not cuenta:
            if _admin_actual():
                raise ErrorApi(403, "Los pedidos de la tienda son solo para cuentas de cliente")
            raise ErrorApi(401, "Inicia sesión para continuar con tu compra")
        return cuenta

    def _armar_compra(datos):
        """Revisa el carrito y la opción de envío y recalcula TODO en el servidor (precio, stock, envío, total)."""
        destino = datos.get("destino") if isinstance(datos.get("destino"), dict) else {}
        pais = destino.get("pais")
        if not isinstance(pais, str) or pais not in UBICACIONES:
            raise ErrorApi(422, "destino.pais: elige un país de la lista")
        items = datos.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ErrorApi(422, "El carrito debe tener entre 1 y 100 productos")
        conn = db()
        desc_gen = _descuento_general(conn)
        lineas, errores, vistos = [], [], set()
        for n, it in enumerate(items, 1):
            if not isinstance(it, dict):
                errores.append(f"items[{n}]: datos inválidos")
                continue
            pid, nom, cant = it.get("product_id"), it.get("product_name"), it.get("quantity")
            if (not isinstance(pid, str) or not isinstance(nom, str) or not isinstance(cant, int)
                    or isinstance(cant, bool) or not 1 <= cant <= 99 or (pid, nom) in vistos):
                errores.append(f"items[{n}]: datos inválidos o repetidos")
                continue
            vistos.add((pid, nom))
            f = conn.execute("""
                SELECT p.stock, COALESCE(p.descuento, 0) AS descuento,
                       COALESCE(p.precio, (SELECT ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)
                       FROM detalle_pedido d WHERE d.product_id = p.product_id AND d.product_name = p.product_name)) AS precio
                FROM productos p WHERE p.product_id = ? AND p.product_name = ? AND p.activo = 1""",
                (pid, nom)).fetchone()
            if not f or f["precio"] is None:
                errores.append(f"{nom}: ya no está a la venta")
                continue
            if f["stock"] < cant:
                errores.append(f"{nom}: solo quedan {f['stock']} unidades")
                continue
            desc_efectivo = max(float(f["descuento"] or 0), desc_gen)
            unitario = round(f["precio"] * (1.0 - desc_efectivo / 100.0), 2)
            lineas.append((pid, nom, round(unitario * cant, 2), cant, round(desc_efectivo / 100.0, 2)))
        if errores:
            raise ErrorApi(422, "No se pudo confirmar el pedido", errores)
        subtotal_cop = round(sum(l[2] for l in lineas) * app.config["TASA_COP"])
        cot = cotizar(pais, str(destino.get("region", ""))[:60], str(destino.get("ciudad", ""))[:80], subtotal_cop)
        opcion = next((o for o in cot["opciones"] if o["id"] == datos.get("opcion_envio")), None)
        if not opcion or opcion["id"] not in MODO_POR_OPCION:
            raise ErrorApi(422, "opcion_envio: elige una opción de envío válida")
        return {"lineas": lineas, "subtotal_cop": subtotal_cop, "opcion": opcion,
                "total_cop": subtotal_cop + opcion["costo_cop"]}

    @app.post("/cuenta/pago")
    def cuenta_pago():
        """Paso 1: cobra con la pasarela (simulada). Si se aprueba, devuelve un pago_id para confirmar el pedido."""
        cuenta = _cuenta_cliente()
        datos = json_del_cuerpo()
        llave = ("pago", cuenta["id"])
        recientes = _frenar_si_abusa(llave)                       # máx. 5 rechazos cada 10 min por cuenta
        compra = _armar_compra(datos)                             # el monto lo calcula el servidor, no el navegador
        tarjeta, errores = validar_tarjeta(datos.get("tarjeta"), date.today())
        if errores:
            raise ErrorApi(422, "Revisa los datos de la tarjeta", errores)
        res = cobrar(tarjeta, compra["total_cop"])                # el número y el CVV no salen de esta línea
        pago_id = "PAG-" + secrets.token_hex(5).upper()
        db().execute("INSERT INTO pagos (pago_id, cuenta_id, monto_cop, estado, motivo, marca, ultimos4, creado) "
                     "VALUES (?,?,?,?,?,?,?,?)", (pago_id, cuenta["id"], compra["total_cop"], res["estado"],
                                                  res["motivo"], res["marca"], res["ultimos4"], _ahora()))
        if res["estado"] != "aprobado":
            intentos[llave] = recientes + [time.time()]
            raise ErrorApi(402, f"Pago rechazado: {res['motivo']}")
        return jsonify({"pago_id": pago_id, "estado": "aprobado", "monto_cop": compra["total_cop"],
                        "marca": res["marca"], "ultimos4": res["ultimos4"]}), 201

    @app.post("/cuenta/pedido")
    def cuenta_pedido():
        """Paso 2: con un pago aprobado, guarda pedido + líneas y descuenta stock. Si no se puede, reembolsa."""
        cuenta = _cuenta_cliente()
        datos = json_del_cuerpo()
        pago_id = datos.get("pago_id") if isinstance(datos.get("pago_id"), str) else ""
        conn = db()
        pago = conn.execute("SELECT * FROM pagos WHERE pago_id = ? AND cuenta_id = ?", (pago_id, cuenta["id"])).fetchone()
        if not pago or pago["estado"] != "aprobado" or pago["order_id"]:
            raise ErrorApi(402, "Primero completa el pago de tu pedido")

        def reembolsar(motivo):
            # Solo si el pago todavía no se usó en ningún pedido.
            conn.execute("UPDATE pagos SET estado = 'reembolsado', motivo = ? WHERE pago_id = ? AND order_id IS NULL",
                         (motivo, pago_id))

        if datetime.now() - datetime.fromisoformat(pago["creado"]) > timedelta(seconds=PAGO_VALIDO_SEG):
            reembolsar("El pago venció antes de confirmar el pedido")
            raise ErrorApi(422, "El pago venció. Tu pago fue reembolsado; vuelve a intentarlo.")
        try:
            compra = _armar_compra(datos)
        except ErrorApi as e:
            reembolsar(e.mensaje)
            raise ErrorApi(e.codigo, f"{e.mensaje}. Tu pago fue reembolsado.", e.detalles)
        if compra["total_cop"] != pago["monto_cop"]:
            reembolsar("El total cambió desde que se pagó")
            raise ErrorApi(422, "El total cambió desde que pagaste. Tu pago fue reembolsado; vuelve a intentarlo.")

        opcion, cid = compra["opcion"], f"WEB-{cuenta['id']}"   # pedidos.customer_id apunta a clientes: uno por cuenta web
        nombre = f"{cuenta['nombre']} {cuenta['apellido'] or ''}".strip()
        hoy = date.today().isoformat()
        order_id = f"ME-{hoy[:4]}-{secrets.token_hex(3).upper()}"
        _c = lambda row, k: row[k] if (row and k in row.keys()) else None
        destino = datos.get("destino") if isinstance(datos.get("destino"), dict) else {}
        dest_pais = destino.get("pais") or _c(cuenta, "pais")
        dest_region = destino.get("region") or _c(cuenta, "region")
        dest_ciudad = destino.get("ciudad") or _c(cuenta, "ciudad")
        dest_direccion = destino.get("direccion") or _c(cuenta, "direccion")
        dest_codigo_postal = destino.get("codigo_postal") or _c(cuenta, "codigo_postal")
        dest_telefono = destino.get("telefono") or _c(cuenta, "telefono")
        conn.execute("BEGIN")
        try:
            conn.execute("""INSERT OR IGNORE INTO clientes (customer_id, customer_name, segment, country, city,
                            state, postal_code, autorizacion_datos, fecha_autorizacion)
                            VALUES (?,?,?,?,?,?,?,1,?)""",
                         (cid, nombre, "Consumer", cuenta["pais"], cuenta["ciudad"], cuenta["region"],
                          cuenta["codigo_postal"], cuenta["fecha_autorizacion"]))
            conn.execute("INSERT INTO pedidos (order_id, order_date, ship_date, ship_mode, customer_id, creado, envio_cop, "
                         "dest_pais, dest_region, dest_ciudad, dest_direccion, dest_codigo_postal, dest_telefono) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, hoy, opcion["despacho"], MODO_POR_OPCION[opcion["id"]],
                          cid, _ahora(), opcion["costo_cop"],
                          dest_pais, dest_region, dest_ciudad, dest_direccion, dest_codigo_postal, dest_telefono))
            for pid, nom, ventas, cant, dto in compra["lineas"]:
                conn.execute("INSERT INTO detalle_pedido (order_id, product_id, product_name, sales, quantity, "
                             "discount, profit) VALUES (?,?,?,?,?,?,NULL)", (order_id, pid, nom, ventas, cant, dto))
                conn.execute("UPDATE productos SET stock = stock - ? WHERE product_id = ? AND product_name = ?",
                             (cant, pid, nom))          # el CHECK (stock >= 0) frena una venta doble
            usado = conn.execute("UPDATE pagos SET order_id = ? WHERE pago_id = ? AND order_id IS NULL "
                                 "AND estado = 'aprobado'", (order_id, pago_id))
            if usado.rowcount != 1:                     # otra petición ya gastó este pago: un pago = un pedido
                raise ErrorApi(409, "Este pago ya se usó en otro pedido")
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            reembolsar("El stock cambió mientras se confirmaba el pedido")
            raise ErrorApi(409, "El stock cambió mientras comprabas. Tu pago fue reembolsado; revisa tu carrito.")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"order_id": order_id, "pago_id": pago_id, "opcion_envio": opcion["id"],
                        "despacho": opcion["despacho"], "entrega_desde": opcion["entrega_desde"],
                        "entrega_hasta": opcion["entrega_hasta"], "subtotal_cop": compra["subtotal_cop"],
                        "envio_cop": opcion["costo_cop"], "total_cop": compra["total_cop"],
                        "marca": pago["marca"], "ultimos4": pago["ultimos4"],
                        "mensaje": "Pedido confirmado"}), 201

    # ---- Mis pedidos: lo que el cliente logueado ve en "Mi Actividad" ----------------------------
    # La tabla pedidos no guarda un estado: se deduce de la fecha de despacho y del modo de envío.
    #   hoy <= despacho                          -> preparando
    #   despacho < hoy <= despacho + tránsito    -> en camino
    #   después                                  -> entregado
    DIAS_TRANSITO = {"Same Day": 1, "First Class": 2, "Standard Class": 5, "Second Class": 10}
    TEXTO_ENVIO = {"Same Day": "Mismo día", "First Class": "Express",
                   "Standard Class": "Estándar", "Second Class": "Internacional"}
    TEXTO_ESTADO = {"preparando": "Preparando tu pedido", "en_camino": "En camino", "entregado": "Entregado"}

    def _estado_pedido(ship_date, ship_mode):
        try:
            despacho = date.fromisoformat(str(ship_date or "")[:10])
        except ValueError:
            return "preparando"
        hoy = date.today()
        if hoy <= despacho:
            return "preparando"
        if hoy <= despacho + timedelta(days=DIAS_TRANSITO.get(ship_mode, 5)):
            return "en_camino"
        return "entregado"

    def _ventana_edicion(pedido):
        """Devuelve (se_puede_editar, editable_hasta). Solo mientras dure la ventana, sin cancelar y sin despachar."""
        if pedido["cancelado_en"] or not pedido["creado"]:
            return False, None
        limite = datetime.fromisoformat(pedido["creado"]) + timedelta(minutes=app.config["PEDIDO_EDITABLE_MIN"])
        abierto = datetime.now() <= limite and _estado_pedido(pedido["ship_date"], pedido["ship_mode"]) == "preparando"
        return abierto, limite.isoformat(timespec="seconds")

    def _pedido_de_la_cuenta(conn, cuenta, order_id):
        p = conn.execute("SELECT * FROM pedidos WHERE order_id = ? AND customer_id = ?",
                         (order_id, f"WEB-{cuenta['id']}")).fetchone()
        if not p:
            raise ErrorApi(404, "No encontramos ese pedido en tu cuenta")
        return p

    def _pedido_json(conn, f):
        """Un pedido de la tienda con sus productos, valores y si todavía se puede cambiar."""
        estado = "cancelado" if f["cancelado_en"] else _estado_pedido(f["ship_date"], f["ship_mode"])
        editable, hasta = _ventana_edicion(f)
        # El reloj lo pone el servidor: así no importa en qué zona horaria esté el navegador.
        segundos = max(0, int((datetime.fromisoformat(hasta) - datetime.now()).total_seconds())) if editable else 0
        tasa = app.config["TASA_COP"]
        items = [{"product_id": r["product_id"], "producto": r["product_name"], "cantidad": r["quantity"],
                  "valor_cop": round(r["sales"] * tasa)}
                 for r in conn.execute("SELECT product_id, product_name, quantity, sales FROM detalle_pedido "
                                       "WHERE order_id = ? ORDER BY detalle_id", (f["order_id"],))]
        pago = conn.execute("SELECT monto_cop, marca, ultimos4, estado, motivo FROM pagos WHERE order_id = ?",
                            (f["order_id"],)).fetchone()
        dest_pais = f["dest_pais"] if "dest_pais" in f.keys() else None
        dest_region = f["dest_region"] if "dest_region" in f.keys() else None
        dest_ciudad = f["dest_ciudad"] if "dest_ciudad" in f.keys() else None
        dest_direccion = f["dest_direccion"] if "dest_direccion" in f.keys() else None
        dest_codigo_postal = f["dest_codigo_postal"] if "dest_codigo_postal" in f.keys() else None
        dest_telefono = f["dest_telefono"] if "dest_telefono" in f.keys() else None
        if not dest_pais:
            cli = conn.execute("SELECT country, state, city, postal_code FROM clientes WHERE customer_id = ?",
                               (f["customer_id"],)).fetchone()
            if cli:
                dest_pais = dest_pais or cli["country"] or ""
                dest_region = dest_region or cli["state"] or ""
                dest_ciudad = dest_ciudad or cli["city"] or ""
                dest_codigo_postal = dest_codigo_postal or cli["postal_code"] or ""

        return {"order_id": f["order_id"], "fecha_pedido": f["order_date"], "fecha_envio": f["ship_date"],
                "ship_mode": f["ship_mode"], "opcion_envio": OPCION_POR_MODO.get(f["ship_mode"], "estandar"),
                "envio": TEXTO_ENVIO.get(f["ship_mode"], f["ship_mode"]), "envio_cop": f["envio_cop"],
                "estado": estado, "estado_texto": "Cancelado" if estado == "cancelado" else TEXTO_ESTADO[estado],
                "editable": editable, "editable_hasta": hasta, "segundos_restantes": segundos, "cancelado_en": f["cancelado_en"],
                "dest_pais": dest_pais or "", "dest_region": dest_region or "", "dest_ciudad": dest_ciudad or "",
                "dest_direccion": dest_direccion or "", "dest_codigo_postal": dest_codigo_postal or "",
                "dest_telefono": dest_telefono or "",
                "destino": {
                    "pais": dest_pais or "", "region": dest_region or "", "ciudad": dest_ciudad or "",
                    "direccion": dest_direccion or "", "codigo_postal": dest_codigo_postal or "",
                    "telefono": dest_telefono or ""
                },
                "total_cop": pago["monto_cop"] if pago else None,
                "pago": ({"marca": pago["marca"], "ultimos4": pago["ultimos4"], "estado": pago["estado"],
                          "nota": pago["motivo"]} if pago else None),
                "items": items}

    @app.get("/cuenta/pedidos")
    def cuenta_pedidos():
        """Pedidos de la cuenta con sesión iniciada (los más nuevos primero), con productos, total y estado."""
        cuenta = _cuenta_cliente()
        pagina, limite, offset = _paginacion(10)
        conn = db()
        cid = f"WEB-{cuenta['id']}"          # mismo id de cliente que se crea en cuenta_pedido()
        total = conn.execute("SELECT COUNT(*) FROM pedidos WHERE customer_id = ?", (cid,)).fetchone()[0]
        filas = conn.execute("SELECT * FROM pedidos WHERE customer_id = ? ORDER BY order_date DESC, rowid DESC "
                             "LIMIT ? OFFSET ?", (cid, limite, offset)).fetchall()
        return jsonify({"pagina": pagina, "limite": limite, "total": total,
                        "ventana_minutos": app.config["PEDIDO_EDITABLE_MIN"],
                        "resultados": [_pedido_json(conn, f) for f in filas]})

    @app.post("/cuenta/pedidos/<order_id>/cancelar")
    def cuenta_pedido_cancelar(order_id):
        """Cancela el pedido (dentro de la ventana): devuelve el stock y reembolsa el pago."""
        cuenta = _cuenta_cliente()
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = _pedido_de_la_cuenta(conn, cuenta, order_id)
            if p["cancelado_en"]:
                raise ErrorApi(409, "Este pedido ya estaba cancelado")
            if not _ventana_edicion(p)[0]:
                raise ErrorApi(409, "Ya pasó el tiempo para cancelar este pedido")
            for r in conn.execute("SELECT product_id, product_name, quantity FROM detalle_pedido WHERE order_id = ?",
                                  (order_id,)).fetchall():
                conn.execute("UPDATE productos SET stock = stock + ? WHERE product_id = ? AND product_name = ?",
                             (r["quantity"], r["product_id"], r["product_name"]))
            conn.execute("UPDATE pedidos SET cancelado_en = ? WHERE order_id = ?", (_ahora(), order_id))
            conn.execute("UPDATE pagos SET estado = 'reembolsado', motivo = 'Pedido cancelado por el cliente' "
                         "WHERE order_id = ?", (order_id,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"mensaje": "Pedido cancelado. Tu pago será reembolsado.",
                        "pedido": _pedido_json(conn, _pedido_de_la_cuenta(conn, cuenta, order_id))})

    @app.put("/cuenta/pedidos/<order_id>")
    def cuenta_pedido_modificar(order_id):
        """Cambia las cantidades, agrega productos nuevos, actualiza dirección/teléfono o cambia el tipo de envío
        (dentro de la ventana). Todo es atómico. Si el costo de envío o productos baja, se reembolsa; si sube, da 409."""
        cuenta = _cuenta_cliente()
        datos = json_del_cuerpo()
        items = datos.get("items")
        nuevos_productos = datos.get("nuevos_productos")
        destino_datos = datos.get("destino")
        opcion_envio_deseada = datos.get("opcion_envio") or (OPCION_POR_MODO.get(datos.get("modo_envio")) if datos.get("modo_envio") else None)
        telefono_dato = datos.get("telefono")

        if items is None and nuevos_productos is None and destino_datos is None and opcion_envio_deseada is None and telefono_dato is None:
            raise ErrorApi(422, "No indicaste ningún cambio para el pedido")

        errores = []
        nuevas = {}
        # Validar productos nuevos a agregar
        agregar = []
        if nuevos_productos is not None:
            if not isinstance(nuevos_productos, list) or len(nuevos_productos) > 50:
                raise ErrorApi(422, "nuevos_productos: envía una lista de productos a agregar (máx. 50)")
            vistos_nuevos = set()
            for n, np in enumerate(nuevos_productos, 1):
                if not isinstance(np, dict):
                    errores.append(f"nuevos_productos[{n}]: datos inválidos")
                    continue
                pid = np.get("product_id")
                nom = np.get("product_name")
                cant = np.get("quantity")
                if (not isinstance(pid, str) or not isinstance(nom, str)
                        or not isinstance(cant, int) or isinstance(cant, bool) or cant < 1 or cant > 99
                        or (pid, nom) in vistos_nuevos):
                    errores.append(f"nuevos_productos[{n}]: datos inválidos, repetidos o cantidad no válida")
                    continue
                vistos_nuevos.add((pid, nom))
                agregar.append({"product_id": pid, "product_name": nom, "quantity": cant})
        if items is not None:
            if not isinstance(items, list) or len(items) > 100:
                raise ErrorApi(422, "items: envía la lista de productos con su cantidad")
            for n, it in enumerate(items, 1):
                cant = it.get("quantity") if isinstance(it, dict) else None
                if (not isinstance(it, dict) or not isinstance(it.get("product_id"), str)
                        or not isinstance(it.get("product_name"), str) or not isinstance(cant, int)
                        or isinstance(cant, bool) or not 0 <= cant <= 99
                        or (it["product_id"], it["product_name"]) in nuevas):
                    errores.append(f"items[{n}]: datos inválidos o repetidos")
                else:
                    nuevas[(it["product_id"], it["product_name"])] = cant

        dir_val = None
        tel_val = None
        if destino_datos is not None:
            if not isinstance(destino_datos, dict):
                errores.append("destino: debe ser un objeto con los datos de entrega")
            else:
                dir_val = _validar_direccion(destino_datos, errores)
                tel_en_dest = destino_datos.get("telefono", telefono_dato or "")
                if tel_en_dest:
                    tel_val = _validar_telefono(tel_en_dest, errores)
        elif telefono_dato is not None:
            tel_val = _validar_telefono(telefono_dato, errores)

        if errores:
            raise ErrorApi(422, "Datos inválidos para actualizar el pedido", errores)

        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = _pedido_de_la_cuenta(conn, cuenta, order_id)
            if not _ventana_edicion(p)[0]:
                raise ErrorApi(409, "Ya pasó el tiempo para modificar este pedido" if not p["cancelado_en"]
                               else "Este pedido está cancelado")

            lineas = conn.execute("SELECT * FROM detalle_pedido WHERE order_id = ? ORDER BY detalle_id",
                                  (order_id,)).fetchall()
            clave = lambda r: (r["product_id"], r["product_name"])

            cambios = 0
            sub_cop_antes = round(sum(r["sales"] for r in lineas) * app.config["TASA_COP"])
            quedan = len(lineas)
            reembolso_items = 0

            # 1. Modificar productos si se enviaron
            if items is not None:
                if set(nuevas) - {clave(r) for r in lineas}:
                    raise ErrorApi(422, "Solo puedes cambiar los productos que ya están en el pedido")
                quedan, cambios_items = 0, 0
                for r in lineas:
                    nueva = nuevas.get(clave(r), r["quantity"])
                    if nueva > r["quantity"]:
                        raise ErrorApi(422, f"{r['product_name']}: no puedes aumentar la cantidad; haz otro pedido")
                    if nueva == r["quantity"]:
                        quedan += 1
                        continue
                    cambios_items += 1
                    conn.execute("UPDATE productos SET stock = stock + ? WHERE product_id = ? AND product_name = ?",
                                 (r["quantity"] - nueva, r["product_id"], r["product_name"]))
                    if nueva == 0:
                        conn.execute("DELETE FROM detalle_pedido WHERE detalle_id = ?", (r["detalle_id"],))
                    else:
                        conn.execute("UPDATE detalle_pedido SET quantity = ?, sales = ? WHERE detalle_id = ?",
                                     (nueva, round(r["sales"] / r["quantity"] * nueva, 2), r["detalle_id"]))
                        quedan += 1
                if not quedan:
                    raise ErrorApi(422, "Quitaste todos los productos: mejor cancela el pedido")
                if cambios_items > 0:
                    cambios += cambios_items
                    sub_cop = round(conn.execute("SELECT SUM(sales) FROM detalle_pedido WHERE order_id = ?",
                                                 (order_id,)).fetchone()[0] * app.config["TASA_COP"])
                    reembolso_items = sub_cop_antes - sub_cop

            # 1b. Agregar productos nuevos al pedido
            costo_agregados = 0
            if agregar:
                claves_existentes = {(r["product_id"], r["product_name"]) for r in
                                     conn.execute("SELECT product_id, product_name FROM detalle_pedido WHERE order_id = ?",
                                                  (order_id,))}
                for np_item in agregar:
                    pid, nom, cant = np_item["product_id"], np_item["product_name"], np_item["quantity"]
                    if (pid, nom) in claves_existentes:
                        raise ErrorApi(422, f"{nom}: ese producto ya está en el pedido; usa 'items' para cambiar su cantidad")
                    prod = conn.execute("""
                        SELECT p.stock, COALESCE(p.precio, (SELECT ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)
                               FROM detalle_pedido d WHERE d.product_id = p.product_id AND d.product_name = p.product_name)) AS precio
                        FROM productos p WHERE p.product_id = ? AND p.product_name = ? AND p.activo = 1""",
                        (pid, nom)).fetchone()
                    if not prod or prod["precio"] is None:
                        raise ErrorApi(422, f"{nom}: producto no encontrado o no disponible")
                    if prod["stock"] < cant:
                        raise ErrorApi(422, f"{nom}: solo quedan {prod['stock']} unidades")
                    ventas = round(prod["precio"] * cant, 2)
                    conn.execute("INSERT INTO detalle_pedido (order_id, product_id, product_name, sales, quantity, "
                                 "discount, profit) VALUES (?,?,?,?,?,0,NULL)", (order_id, pid, nom, ventas, cant))
                    conn.execute("UPDATE productos SET stock = stock - ? WHERE product_id = ? AND product_name = ?",
                                 (cant, pid, nom))
                    costo_agregados += round(ventas * app.config["TASA_COP"])
                    claves_existentes.add((pid, nom))
                cambios += len(agregar)

            sub_cop_actual = round(conn.execute("SELECT SUM(sales) FROM detalle_pedido WHERE order_id = ?",
                                                (order_id,)).fetchone()[0] * app.config["TASA_COP"])

            # 2. Dirección y teléfono
            if destino_datos is not None or telefono_dato is not None:
                _c = lambda row, k: row[k] if (row and k in row.keys()) else None
                dest_pais = dir_val["pais"] if dir_val and dir_val.get("pais") else (_c(p, "dest_pais") or _c(cuenta, "pais"))
                dest_region = dir_val["region"] if dir_val and dir_val.get("region") else (_c(p, "dest_region") or _c(cuenta, "region"))
                dest_ciudad = dir_val["ciudad"] if dir_val and dir_val.get("ciudad") else (_c(p, "dest_ciudad") or _c(cuenta, "ciudad"))
                dest_direccion = dir_val["direccion"] if dir_val and dir_val.get("direccion") else (_c(p, "dest_direccion") or _c(cuenta, "direccion"))
                dest_codigo_postal = dir_val["codigo_postal"] if dir_val and dir_val.get("codigo_postal") else (_c(p, "dest_codigo_postal") or _c(cuenta, "codigo_postal"))
                dest_telefono = tel_val if tel_val else (_c(p, "dest_telefono") or _c(cuenta, "telefono"))

                cambio_destino = (dest_pais != p["dest_pais"] or dest_region != p["dest_region"]
                                  or dest_ciudad != p["dest_ciudad"] or dest_direccion != p["dest_direccion"]
                                  or dest_codigo_postal != p["dest_codigo_postal"] or dest_telefono != p["dest_telefono"])
                if cambio_destino:
                    cambios += 1
            else:
                dest_pais = p["dest_pais"]
                dest_region = p["dest_region"]
                dest_ciudad = p["dest_ciudad"]
                dest_direccion = p["dest_direccion"]
                dest_codigo_postal = p["dest_codigo_postal"]
                dest_telefono = p["dest_telefono"]
                cambio_destino = False

            # 3. Envío (recotizar si cambió la opción o la ubicación)
            nuevo_envio_cop = p["envio_cop"] or 0
            nuevo_ship_mode = p["ship_mode"]
            nuevo_ship_date = p["ship_date"]
            reembolso_envio = 0
            opcion_actual_id = OPCION_POR_MODO.get(p["ship_mode"], "estandar")
            opcion_a_usar = opcion_envio_deseada or opcion_actual_id

            if opcion_envio_deseada is not None or cambio_destino:
                cot = cotizar(dest_pais, str(dest_region or "")[:60], str(dest_ciudad or "")[:80], sub_cop_actual)
                opcion_cotizada = next((o for o in cot["opciones"] if o["id"] == opcion_a_usar), None)
                if not opcion_cotizada:
                    if opcion_envio_deseada is not None:
                        raise ErrorApi(422, f"La opción de envío «{opcion_a_usar}» no está disponible para ese destino")
                    opcion_cotizada = cot["opciones"][0]

                costo_nuevo_envio = opcion_cotizada["costo_cop"]
                costo_antes_envio = p["envio_cop"] or 0
                if costo_nuevo_envio > costo_antes_envio:
                    dif = costo_nuevo_envio - costo_antes_envio
                    raise ErrorApi(409, f"El nuevo tipo de envío cuesta ${dif:,} COP más. No se cobra automáticamente; debes hacer un pedido nuevo si deseas este envío.".replace(",", "."))
                elif costo_nuevo_envio < costo_antes_envio:
                    reembolso_envio = costo_antes_envio - costo_nuevo_envio
                    cambios += 1
                elif opcion_cotizada["id"] != opcion_actual_id:
                    cambios += 1

                nuevo_envio_cop = costo_nuevo_envio
                nuevo_ship_mode = MODO_POR_OPCION[opcion_cotizada["id"]]
                nuevo_ship_date = opcion_cotizada["despacho"]

            if not cambios:
                raise ErrorApi(422, "No cambiaste nada en el pedido")

            reembolso_total = (reembolso_items or 0) + (reembolso_envio or 0)
            # Si se agregaron productos, el costo sube: ajustar el pago
            cargo_extra = costo_agregados if agregar else 0
            ajuste_neto = cargo_extra - reembolso_total  # positivo = cobra más, negativo = reembolsa
            if ajuste_neto > 0:
                conn.execute("UPDATE pagos SET monto_cop = monto_cop + ?, motivo = ? WHERE order_id = ?",
                             (ajuste_neto, f"Cargo adicional por productos agregados al pedido: ${ajuste_neto:,} COP".replace(",", "."),
                              order_id))
            elif ajuste_neto < 0:
                conn.execute("UPDATE pagos SET monto_cop = monto_cop + ?, motivo = ? WHERE order_id = ?",
                             (ajuste_neto, f"Reembolso parcial por modificar el pedido: ${-ajuste_neto:,} COP".replace(",", "."),
                              order_id))

            conn.execute("""
                UPDATE pedidos SET
                    dest_pais = ?, dest_region = ?, dest_ciudad = ?,
                    dest_direccion = ?, dest_codigo_postal = ?, dest_telefono = ?,
                    ship_mode = ?, ship_date = ?, envio_cop = ?
                WHERE order_id = ?
            """, (dest_pais, dest_region, dest_ciudad, dest_direccion, dest_codigo_postal, dest_telefono,
                  nuevo_ship_mode, nuevo_ship_date, nuevo_envio_cop, order_id))

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        return jsonify({"mensaje": "Pedido actualizado",
                        "reembolso_cop": max(0, -ajuste_neto) if ajuste_neto < 0 else 0,
                        "cargo_extra_cop": max(0, ajuste_neto) if ajuste_neto > 0 else 0,
                        "pedido": _pedido_json(conn, _pedido_de_la_cuenta(conn, cuenta, order_id))})

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

    @app.put("/cuenta/perfil")
    def cuenta_perfil():
        """Permite al cliente editar sus datos personales (nombre, apellido, correo, teléfono y dirección).
        Cambiar el correo o la contraseña exige validar la contraseña actual.
        No altera pedidos existentes ni expone datos sensibles en la auditoría."""
        cuenta = _cuenta_actual()
        if not cuenta:
            if _admin_actual():              # un administrador con sesión no tiene perfil de cliente
                raise ErrorApi(403, "Solo los clientes pueden editar su perfil")
            raise ErrorApi(401, "Inicia sesión para editar tu perfil")

        datos = json_del_cuerpo()
        errores = []

        nombre = _texto(datos.get("nombre"), "nombre", errores, 60)
        apellido = _texto(datos.get("apellido"), "apellido", errores, 60)
        if nombre is not None and len(nombre) < 2:
            errores.append("nombre: debe tener entre 2 y 60 caracteres")
        if apellido is not None and len(apellido) < 2:
            errores.append("apellido: debe tener entre 2 y 60 caracteres")

        email_raw = datos.get("email")
        email = _texto(email_raw, "email", errores, 254)
        if email:
            email = email.lower()
            if not EMAIL_RE.match(email):
                errores.append("email: no es un correo válido")

        telefono = _validar_telefono(datos.get("telefono"), errores)
        d = _validar_direccion(datos, errores)

        password_actual = _texto_cuenta(datos, "password_actual") or _texto_cuenta(datos, "clave_actual")
        password_nueva = _texto_cuenta(datos, "password_nueva") or _texto_cuenta(datos, "clave_nueva")

        correo_cambio = bool(email and email != cuenta["email"].lower())
        cambia_clave = bool(password_nueva)

        if password_actual and not check_password_hash(cuenta["password_hash"], password_actual):
            raise ErrorApi(401, "La contraseña actual es incorrecta")

        if correo_cambio or cambia_clave:
            if not password_actual:
                errores.append("password_actual: se requiere tu contraseña actual para confirmar el cambio")

        if cambia_clave:
            if not 8 <= len(password_nueva) <= 128:
                errores.append("password_nueva: debe tener entre 8 y 128 caracteres")

        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)

        conn = db()
        if correo_cambio:
            existente = conn.execute("SELECT id FROM cuentas WHERE lower(email) = ? AND id != ?", (email, cuenta["id"])).fetchone()
            if existente:
                raise ErrorApi(409, "Ya existe una cuenta con ese correo")

        nuevo_hash = generate_password_hash(password_nueva) if cambia_clave else cuenta["password_hash"]

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE cuentas
                SET nombre = ?, apellido = ?, email = ?, password_hash = ?,
                    telefono = ?, pais = ?, region = ?, ciudad = ?, direccion = ?, codigo_postal = ?
                WHERE id = ?
            """, (nombre, apellido, email, nuevo_hash,
                  telefono, d["pais"], d["region"], d["ciudad"], d["direccion"], d["codigo_postal"],
                  cuenta["id"]))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise ErrorApi(409, "Ya existe una cuenta con ese correo")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        g.nivel = getattr(g, "nivel", None) or "usuario"
        auditar(f"Perfil actualizado cuenta_id={cuenta['id']}")

        fila = conn.execute("SELECT * FROM cuentas WHERE id = ?", (cuenta["id"],)).fetchone()
        return jsonify(_perfil(fila))

    @app.post("/cuenta/salir")
    def cuenta_salir():
        """Cierra solo la sesión del cliente; si hay un administrador con sesión, sigue abierta."""
        return _cerrar_sesion(COOKIE_SESION, jsonify({"mensaje": "Sesión cerrada"}))

    @app.get("/cuenta/yo")
    def cuenta_yo():
        c = _cuenta_actual()
        return jsonify({"sesion": bool(c), "cuenta": _perfil(c) if c else None})

    # ------------------------------------------------------------ sección de administradores
    # Un administrador es una cuenta con rol 'admin'. Para crearla hay que conocer el código de
    # administrador (el mismo valor de ADMIN_KEY): así nadie se hace admin solo desde la web.
    def requiere_admin():
        c = _admin_actual()                 # solo cuenta la sesión de administrador, no la del cliente
        if not c:
            if _cuenta_actual():            # hay un cliente con sesión, pero no es administrador
                raise ErrorApi(403, "Esta sección es solo para administradores")
            raise ErrorApi(401, "Inicia sesión como administrador para continuar")
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
        return _abrir_sesion(cuenta_id, resp, admin=True)

    @app.post("/admin/entrar")
    def admin_entrar():
        return _iniciar_sesion(solo_admin=True)

    @app.post("/admin/salir")
    def admin_salir():
        """Cierra solo la sesión del administrador; la del cliente en la tienda sigue abierta."""
        return _cerrar_sesion(COOKIE_SESION_ADMIN, jsonify({"mensaje": "Sesión de administrador cerrada"}))

    @app.get("/admin/yo")
    def admin_yo():
        c = _admin_actual()
        return jsonify({"sesion": bool(c), "admin": bool(c),
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

    def _nuevo_nombre(datos, maximo):
        errores = []
        nuevo = _texto(datos.get("nuevo_nombre"), "nuevo_nombre", errores, maximo)
        if nuevo is not None and len(nuevo) < 2:
            errores.append("nuevo_nombre: mínimo 2 caracteres")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        return nuevo

    @app.put("/admin/catalogo/categorias/<nombre_actual>")
    def editar_categoria(nombre_actual):
        cuenta = requiere_admin()
        nuevo = _nuevo_nombre(json_del_cuerpo(), 40)
        conn = db()
        if not conn.execute("SELECT 1 FROM categorias WHERE nombre = ?", (nombre_actual,)).fetchone():
            raise ErrorApi(404, "Categoría no encontrada")
        if nombre_actual.lower() != nuevo.lower() and conn.execute(
                "SELECT 1 FROM categorias WHERE lower(nombre) = lower(?)", (nuevo,)).fetchone():
            raise ErrorApi(409, f"Ya existe la categoría «{nuevo}»")
        conn.execute("BEGIN")
        try:
            # Las subcategorías apuntan al nombre de la categoría (llave foránea): se deja que la base
            # revise las llaves al hacer COMMIT, cuando ya se cambiaron las dos tablas.
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute("UPDATE categorias SET nombre = ? WHERE nombre = ?", (nuevo, nombre_actual))
            conn.execute("UPDATE subcategorias SET categoria = ? WHERE categoria = ?", (nuevo, nombre_actual))
            conn.execute("UPDATE productos SET category = ? WHERE category = ?", (nuevo, nombre_actual))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        auditar(f"Categoría renombrada: '{nombre_actual}' -> '{nuevo}' (por {cuenta['email']})")
        return jsonify({"mensaje": "Categoría actualizada con éxito", "nuevo_nombre": nuevo})

    @app.put("/admin/catalogo/subcategorias/<categoria>/<nombre_actual>")
    def editar_subcategoria(categoria, nombre_actual):
        cuenta = requiere_admin()
        nuevo = _nuevo_nombre(json_del_cuerpo(), 60)
        conn = db()
        if not conn.execute("SELECT 1 FROM subcategorias WHERE categoria = ? AND nombre = ?",
                            (categoria, nombre_actual)).fetchone():
            raise ErrorApi(404, "Subcategoría no encontrada")
        if nombre_actual.lower() != nuevo.lower() and conn.execute(
                "SELECT 1 FROM subcategorias WHERE categoria = ? AND lower(nombre) = lower(?)",
                (categoria, nuevo)).fetchone():
            raise ErrorApi(409, f"Ya existe la subcategoría «{nuevo}» en «{categoria}»")
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE subcategorias SET nombre = ? WHERE categoria = ? AND nombre = ?",
                         (nuevo, categoria, nombre_actual))
            conn.execute("UPDATE productos SET sub_category = ? WHERE category = ? AND sub_category = ?",
                         (nuevo, categoria, nombre_actual))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        auditar(f"Subcategoría renombrada: '{nombre_actual}' -> '{nuevo}' en '{categoria}' (por {cuenta['email']})")
        return jsonify({"mensaje": "Subcategoría actualizada", "nuevo_nombre": nuevo})

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
        marca = _texto(datos.get("marca"), "marca", errores, 120, obligatorio=False)
        descripcion = _texto(datos.get("descripcion"), "descripcion", errores, 2000, obligatorio=False)
        ficha_lista = _validar_ficha(datos.get("ficha"), errores)
        descuento_raw = datos.get("descuento", 0)
        if descuento_raw == "" or descuento_raw is None:
            descuento = 0.0
        else:
            try:
                descuento = round(float(descuento_raw), 2)
                if not 0 <= descuento <= 100:
                    errores.append("descuento: debe ser un porcentaje entre 0 y 100")
            except (ValueError, TypeError):
                errores.append("descuento: debe ser un número válido")
                descuento = 0.0
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        if not db().execute("SELECT 1 FROM subcategorias WHERE categoria = ? AND nombre = ?",
                            (categoria, subcategoria)).fetchone():
            raise ErrorApi(422, "Datos inválidos",
                           [f"subcategoria: «{subcategoria}» no existe dentro de «{categoria}»"])
        if nombre_repetido(db(), categoria, nombre):
            raise ErrorApi(409, f"Ya hay un producto llamado «{nombre}» en «{categoria}»")
        pid = "ME-P-" + secrets.token_hex(3).upper()
        ficha_json = json.dumps(ficha_lista, ensure_ascii=False) if ficha_lista else None
        try:
            db().execute("INSERT INTO productos (product_id, product_name, category, sub_category, precio, stock, marca, descripcion, ficha, descuento) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (pid, nombre, categoria, subcategoria, round(float(precio), 2), stock, marca, descripcion, ficha_json, descuento))
        except sqlite3.IntegrityError as e:
            raise ErrorApi(422, "La base de datos rechazó el producto", [str(e)])
        auditar(f"Producto creado: {pid} / {nombre} · stock inicial {stock} (por {cuenta['email']})")
        precio_num = round(float(precio), 2)
        return jsonify({"product_id": pid, "nombre": nombre, "categoria": categoria, "subcategoria": subcategoria,
                        "precio": precio_num, "precio_final": round(precio_num * (1.0 - descuento / 100.0), 2),
                        "descuento": descuento, "stock": stock,
                        "marca": marca, "descripcion": descripcion, "ficha": ficha_lista or [],
                        "mensaje": "Producto creado"}), 201

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
               p.marca, p.descripcion, p.ficha, COALESCE(p.descuento, 0) AS descuento,
               p.precio AS precio_propio,
               COALESCE(p.precio, ROUND(AVG(d.sales / (d.quantity * (1 - d.discount))), 2)) AS precio,
               COALESCE(SUM(d.quantity), 0) AS vendidos, COUNT(d.detalle_id) AS lineas,
               c.tono AS tono, s.icono AS icono
        {UNION_PRECIO} {{donde}} GROUP BY p.product_id, p.product_name, c.tono, s.icono"""

    def _json_producto_admin(r):
        desc = float(r["descuento"] or 0)
        p_base = r["precio"]
        p_final = round(p_base * (1.0 - desc / 100.0), 2) if p_base is not None else None
        return {"product_id": r["product_id"], "nombre": r["product_name"], "categoria": r["category"],
                "subcategoria": r["sub_category"], "precio": r["precio"], "precio_final": p_final,
                "descuento": desc,
                "precio_propio": r["precio_propio"] is not None,   # False = precio deducido del historial
                "activo": bool(r["activo"]), "stock": r["stock"], "vendidos": r["vendidos"], "con_ventas": r["lineas"] > 0,
                "tono": r["tono"], "icono": r["icono"],
                "marca": r["marca"], "descripcion": r["descripcion"], "ficha": _deserializar_ficha(r["ficha"])}

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
        campos = {"nombre", "categoria", "subcategoria", "precio", "activo", "stock", "ajuste_stock", "marca", "descripcion", "ficha", "descuento"}
        errores = []
        if set(datos) - campos:
            errores.append(f"campos desconocidos: {sorted(set(datos) - campos)}")
        if not set(datos) & campos:
            errores.append(f"no hay nada que cambiar: envía al menos uno de {sorted(campos)}")
        nombre, categoria, subcategoria = actual["product_name"], actual["category"], actual["sub_category"]
        precio, activo = actual["precio"], actual["activo"]
        nuevo_stock, ajuste = actual["stock"], None
        marca = actual["marca"]
        descripcion = actual["descripcion"]
        ficha_str = actual["ficha"]
        descuento = actual["descuento"] if "descuento" in actual.keys() and actual["descuento"] is not None else 0
        if "descuento" in datos:
            d = datos["descuento"]
            if d == "" or d is None:
                descuento = 0.0
            else:
                try:
                    descuento = round(float(d), 2)
                    if not 0 <= descuento <= 100:
                        errores.append("descuento: debe ser un porcentaje entre 0 y 100")
                except (ValueError, TypeError):
                    errores.append("descuento: debe ser un número válido")
                    descuento = 0.0 # Valor seguro
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
        if "marca" in datos:
            marca = _texto(datos["marca"], "marca", errores, 120, obligatorio=False)
        if "descripcion" in datos:
            descripcion = _texto(datos["descripcion"], "descripcion", errores, 2000, obligatorio=False)
        if "ficha" in datos:
            ficha_lista = _validar_ficha(datos["ficha"], errores)
            ficha_str = json.dumps(ficha_lista, ensure_ascii=False) if ficha_lista else None
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
            ("stock", actual["stock"], nuevo_stock),
            ("marca", actual["marca"], marca)) if antes != ahora]
        # `actual` es una fila de SQLite (no tiene .get): se lee con corchetes, y la columna puede faltar en bases viejas.
        descuento_antes = round(float(actual["descuento"] or 0), 2) if "descuento" in actual.keys() else 0.0
        if "descuento" in datos and descuento_antes != descuento:
            cambios.append(f"descuento: {descuento_antes}% → {descuento}%")
        if "descripcion" in datos and descripcion != actual["descripcion"]:
            cambios.append("descripción actualizada")
        if "ficha" in datos and ficha_str != actual["ficha"]:
            cambios.append("ficha técnica actualizada")
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
                         f"activo = ?, stock = {stock_sql}, marca = ?, descripcion = ?, ficha = ?, descuento = ? "
                         f"WHERE product_id = ? AND product_name = ?",
                         (nombre, categoria, subcategoria, precio, activo,
                          ajuste if ajuste is not None else nuevo_stock,
                          marca, descripcion, ficha_str, descuento, actual["product_id"], actual["product_name"]))
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

    @app.post("/admin/catalogo/descuento-general")
    def catalogo_descuento_general():
        cuenta = requiere_admin()
        datos = json_del_cuerpo()
        d = datos.get("descuento")
        
        if d is None or d == "":
            raise ErrorApi(422, "descuento: porcentaje requerido")
            
        try:
            descuento = round(float(d), 2)
        except (ValueError, TypeError):
            raise ErrorApi(422, "descuento: debe ser un número válido")
            
        if not 0 <= descuento <= 100:
            raise ErrorApi(422, "descuento: debe ser un porcentaje entre 0 y 100")
        aplicar_a_productos = bool(datos.get("aplicar_a_productos", False))
        categoria = datos.get("categoria")

        conn = db()
        conn.execute("INSERT OR REPLACE INTO configuracion (clave, valor) VALUES ('descuento_general', ?)",
                     (str(descuento),))
        filas = 0
        if aplicar_a_productos:
            if categoria:
                c = conn.execute("UPDATE productos SET descuento = ? WHERE category = ? AND activo = 1",
                                 (descuento, categoria))
            else:
                c = conn.execute("UPDATE productos SET descuento = ? WHERE activo = 1", (descuento,))
            filas = c.rowcount
            auditar(f"Descuento de {descuento}% aplicado a {filas} productos (por {cuenta['email']})")
        else:
            auditar(f"Descuento general fijado en {descuento}% (por {cuenta['email']})")

        return jsonify({
            "mensaje": f"Descuento general establecido en {descuento}%" + (f" y aplicado a {filas} productos" if aplicar_a_productos else ""),
            "descuento_general": descuento,
            "productos_afectados": filas
        })

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
        desc_gen = _descuento_general()
        f = db().execute("""SELECT (SELECT COUNT(*) FROM productos WHERE activo = 1) AS productos,
                                   (SELECT COUNT(*) FROM categorias) AS categorias,
                                   (SELECT COUNT(*) FROM pedidos WHERE cancelado_en IS NULL) AS pedidos,
                                   (SELECT MAX(discount) FROM detalle_pedido) AS descuento_hist,
                                   (SELECT MAX(descuento) FROM productos WHERE activo = 1) AS descuento_prod""").fetchone()
        dto_max = max(float(f["descuento_hist"] or 0), float(f["descuento_prod"] or 0) / 100.0, desc_gen / 100.0)
        return jsonify({
            "productos": f["productos"],
            "categorias": f["categorias"],
            "pedidos": f["pedidos"],
            "descuento_max": dto_max
        })

    # ------------------------------------------------------------ general
    @app.get("/salud")
    def salud():
        return jsonify({"estado": "ok", "servicio": "Mercado Express API"})

    @app.get("/resumen")
    def resumen():
        f = db().execute("""
            SELECT (SELECT COUNT(*) FROM clientes)       AS clientes,
                   (SELECT COUNT(*) FROM productos)      AS productos,
                   (SELECT COUNT(*) FROM pedidos WHERE cancelado_en IS NULL) AS pedidos,
                   (SELECT COUNT(*) FROM detalle_pedido d JOIN pedidos pe ON pe.order_id = d.order_id
                     WHERE pe.cancelado_en IS NULL) AS lineas_de_pedido,
                   (SELECT ROUND(SUM(d.sales), 2) FROM detalle_pedido d JOIN pedidos pe ON pe.order_id = d.order_id
                     WHERE pe.cancelado_en IS NULL) AS ventas_totales,
                   (SELECT ROUND(SUM(d.profit), 2) FROM detalle_pedido d JOIN pedidos pe ON pe.order_id = d.order_id
                     WHERE pe.cancelado_en IS NULL) AS ganancia_total
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
             "categoria": r["category"], "subcategoria": r["sub_category"],
             "marca": r["marca"], "descripcion": r["descripcion"],
             "ficha": _deserializar_ficha(r["ficha"])} for r in filas]})

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
            JOIN pedidos pe ON pe.order_id = d.order_id AND pe.cancelado_en IS NULL
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
            SELECT c.customer_id, c.customer_name, ROUND(SUM(COALESCE(d.profit, 0)), 2) AS ganancia_total
            FROM detalle_pedido d
            JOIN pedidos pe ON d.order_id = pe.order_id AND pe.cancelado_en IS NULL
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
            WHERE pe.cancelado_en IS NULL
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

    
    # ------------------------------------------------------------ turnos (actividad 9: filas y capacidad)
    LLEGADAS_VENTANA_MIN = 60       # las llegadas por hora se cuentan en la última hora
    DURACION_POR_DEFECTO = 180      # segundos, mientras no haya turnos terminados para medir
    DURACIONES_A_PROMEDIAR = 20     # últimos turnos terminados que entran al promedio
    MINIMO_PARA_CONFIAR = 5         # con menos turnos terminados, la pantalla avisa que es un estimado
    turnos_ip = {}                  # ip -> momentos en que pidió turno (límite contra abuso)

    def _entero(valor, minimo, maximo):
        """Entero real dentro del rango (True/False y textos no valen)."""
        return isinstance(valor, int) and not isinstance(valor, bool) and minimo <= valor <= maximo

    def _servicio(servicio_id, solo_activos=True):
        if not _entero(servicio_id, 1, 10**9):
            raise ErrorApi(422, "servicio_id debe ser un número entero válido")
        fila = db().execute("SELECT * FROM turnos_servicios WHERE id = ?", (servicio_id,)).fetchone()
        if not fila or (solo_activos and not fila["activo"]):
            raise ErrorApi(404, "Ese servicio no existe")
        return fila

    def _estado_servicio(conn, s):
        terminados = conn.execute(
            "SELECT fecha_atencion, fecha_fin FROM turnos WHERE servicio_id = ? AND estado = 'finalizado' "
            "AND fecha_atencion IS NOT NULL AND fecha_fin IS NOT NULL ORDER BY id DESC LIMIT ?",
            (s["id"], DURACIONES_A_PROMEDIAR)).fetchall()
        duraciones = [(datetime.fromisoformat(t["fecha_fin"]) - datetime.fromisoformat(t["fecha_atencion"])).total_seconds()
                      for t in terminados]
        duraciones = [d for d in duraciones if d > 0]
        duracion = sum(duraciones) / len(duraciones) if duraciones else DURACION_POR_DEFECTO
        desde = (datetime.now() - timedelta(minutes=LLEGADAS_VENTANA_MIN)).isoformat()
        llegadas = conn.execute("SELECT COUNT(*) FROM turnos WHERE servicio_id = ? AND fecha_llegada >= ?",
                                (s["id"], desde)).fetchone()[0] * 60 / LLEGADAS_VENTANA_MIN
        en_espera = conn.execute("SELECT COUNT(*) FROM turnos WHERE servicio_id = ? AND estado = 'espera'",
                                 (s["id"],)).fetchone()[0]
        atendiendo = conn.execute("SELECT numero FROM turnos WHERE servicio_id = ? AND estado = 'atendiendo'",
                                  (s["id"],)).fetchone()
        a = analizar(llegadas, duracion, s["asesores"], s["meta_espera_min"], en_espera)
        return {
            "id": s["id"], "nombre": s["nombre"],
            "en_espera": en_espera,
            "turno_actual": atendiendo["numero"] if atendiendo else "-",
            "asesores": s["asesores"], "meta_espera_min": s["meta_espera_min"],
            "llegadas_por_hora": round(llegadas, 1),
            "duracion_promedio_seg": round(duracion),
            "datos_suficientes": len(duraciones) >= MINIMO_PARA_CONFIAR,
            "tiempo_estimado_minutos": math.ceil(a["espera_actual_min"]) if en_espera else 0,
            **a,
            "alerta": mensaje(a, s["asesores"], s["meta_espera_min"]),
        }

    @app.get("/turnos/api/estado")
    def turnos_estado():
        conn = db()
        servicios = conn.execute("SELECT * FROM turnos_servicios WHERE activo = 1 ORDER BY id").fetchall()
        return jsonify([_estado_servicio(conn, s) for s in servicios])

    @app.get("/turnos/api/motivos")
    def turnos_motivos():
        return jsonify(MOTIVOS_TURNO)

    @app.post("/turnos/api/pedir")
    def pedir_turno():
        datos = json_del_cuerpo()
        servicio = _servicio(datos.get("servicio_id"))

        # ---- Validar motivo según el servicio elegido ----
        motivo = datos.get("motivo")
        if not motivo or not isinstance(motivo, str):
            raise ErrorApi(422, "Motivo inválido", ["motivo: elige una de las opciones permitidas"])
        motivos_del_servicio = MOTIVOS_TURNO.get(servicio["nombre"], [])
        if motivo not in motivos_del_servicio:
            raise ErrorApi(400, "Motivo inválido para este servicio",
                           ["motivo: elige una de las opciones permitidas para el servicio seleccionado"])

        # ---- Validar detalle (texto libre, máx. 500 caracteres, sin HTML) ----
        detalle_raw = datos.get("detalle")
        detalle = None
        if detalle_raw is not None:
            if not isinstance(detalle_raw, str):
                raise ErrorApi(422, "Detalle inválido", ["detalle: debe ser texto"])
            detalle = re.sub(r"<[^>]*>", "", detalle_raw).strip()
            if len(detalle) > 500:
                raise ErrorApi(422, "Detalle demasiado largo", ["detalle: máximo 500 caracteres"])
            if not detalle:
                detalle = None
        if motivo == "Otra consulta" and not detalle:
            raise ErrorApi(422, "El detalle es obligatorio para 'Otra consulta'",
                           ["detalle: describe tu consulta"])

        # ---- Validar pedido_id (opcional, si el cliente tiene sesión) ----
        pedido_id = datos.get("pedido_id")
        cuenta = _cuenta_actual()
        cuenta_id = cuenta["id"] if cuenta else None
        if pedido_id is not None:
            if not isinstance(pedido_id, str):
                raise ErrorApi(422, "Pedido inválido", ["pedido_id: debe ser un texto válido"])
            pedido_id = pedido_id.strip()
            if not pedido_id:
                pedido_id = None
            else:
                if not cuenta:
                    raise ErrorApi(422, "Debes iniciar sesión para asociar un pedido",
                                   ["pedido_id: inicia sesión primero"])
                cid = f"WEB-{cuenta['id']}"
                pedido = db().execute(
                    "SELECT 1 FROM pedidos WHERE order_id = ? AND customer_id = ?",
                    (pedido_id, cid)).fetchone()
                if not pedido:
                    raise ErrorApi(404, "Pedido no encontrado o no te pertenece",
                                   ["pedido_id: verifica el número de pedido"])
        else:
            pedido_id = None

        # Límite por dispositivo: un tótem real no pasa de unas pocas personas por minuto.
        ahora = time.time()
        recientes = [t for t in turnos_ip.get(request.remote_addr, []) if ahora - t < app.config["TURNOS_VENTANA_SEG"]]
        if len(recientes) >= app.config["TURNOS_MAX_POR_IP"]:
            raise ErrorApi(429, "Demasiados turnos pedidos seguidos. Espera un momento e inténtalo de nuevo.")
        conn = db()
        conn.execute("BEGIN IMMEDIATE")          # así dos personas nunca reciben el mismo número
        try:
            if conn.execute("SELECT COUNT(*) FROM turnos WHERE servicio_id = ? AND estado = 'espera'",
                            (servicio["id"],)).fetchone()[0] >= app.config["TURNOS_MAX_FILA"]:
                raise ErrorApi(409, "La fila de este servicio está llena por ahora. Inténtalo más tarde.")
            ultimo = conn.execute("SELECT MAX(numero) FROM turnos WHERE servicio_id = ? AND date(fecha_llegada) = ?",
                                  (servicio["id"], date.today().isoformat())).fetchone()[0]
            numero = (ultimo or 0) + 1
            conn.execute("INSERT INTO turnos (servicio_id, numero, estado, fecha_llegada, cuenta_id, motivo, detalle, pedido_id) "
                         "VALUES (?, ?, 'espera', ?, ?, ?, ?, ?)",
                         (servicio["id"], numero, datetime.now().isoformat(), cuenta_id, motivo, detalle, pedido_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        recientes.append(ahora)
        turnos_ip[request.remote_addr] = recientes
        return jsonify({"mensaje": "Turno asignado", "numero": numero})

    @app.post("/admin/turnos/api/llamar")
    def llamar_turno():
        requiere_admin()
        servicio = _servicio(json_del_cuerpo().get("servicio_id"))
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ahora = datetime.now().isoformat()
            # Termina al que estaba siendo atendido (queda registrada su duración)...
            conn.execute("UPDATE turnos SET estado = 'finalizado', fecha_fin = ? "
                         "WHERE servicio_id = ? AND estado = 'atendiendo'", (ahora, servicio["id"]))
            # ...y empieza a atender al siguiente de la fila.
            siguiente = conn.execute("SELECT id, numero FROM turnos WHERE servicio_id = ? AND estado = 'espera' "
                                     "ORDER BY id LIMIT 1", (servicio["id"],)).fetchone()
            if siguiente:
                conn.execute("UPDATE turnos SET estado = 'atendiendo', fecha_atencion = ? WHERE id = ?",
                             (ahora, siguiente["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if not siguiente:
            return jsonify({"mensaje": "La fila está vacía", "numero": None})
        return jsonify({"mensaje": f"Turno {siguiente['numero']} en atención", "numero": siguiente["numero"]})

    @app.post("/admin/turnos/api/finalizar")
    def finalizar_turno():
        requiere_admin()
        datos = json_del_cuerpo()
        servicio = _servicio(datos.get("servicio_id"))
        turno_id = datos.get("turno_id")
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ahora = datetime.now().isoformat()
            if turno_id:
                cur = conn.execute("UPDATE turnos SET estado = 'finalizado', fecha_fin = ? "
                                   "WHERE id = ? AND servicio_id = ? AND estado = 'atendiendo'",
                                   (ahora, turno_id, servicio["id"]))
            else:
                cur = conn.execute("UPDATE turnos SET estado = 'finalizado', fecha_fin = ? "
                                   "WHERE servicio_id = ? AND estado = 'atendiendo'",
                                   (ahora, servicio["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"mensaje": "Turno finalizado", "finalizados": cur.rowcount})

    def _formatear_turno_admin(t, ahora_dt):
        llegada_dt = datetime.fromisoformat(t["fecha_llegada"])
        if t["estado"] == "espera":
            minutos = max(0, int((ahora_dt - llegada_dt).total_seconds() // 60))
        elif t["fecha_atencion"]:
            atencion_dt = datetime.fromisoformat(t["fecha_atencion"])
            minutos = max(0, int((atencion_dt - llegada_dt).total_seconds() // 60))
        elif t["fecha_fin"]:
            fin_dt = datetime.fromisoformat(t["fecha_fin"])
            minutos = max(0, int((fin_dt - llegada_dt).total_seconds() // 60))
        else:
            minutos = max(0, int((ahora_dt - llegada_dt).total_seconds() // 60))

        nombre_cliente = None
        if t["cuenta_nombre"]:
            nombre_cliente = t["cuenta_nombre"]
            if t["cuenta_apellido"]:
                nombre_cliente += f" {t['cuenta_apellido']}"
            nombre_cliente = nombre_cliente.strip()

        return {
            "id": t["id"],
            "servicio_id": t["servicio_id"],
            "numero": t["numero"],
            "estado": t["estado"],
            "fecha_llegada": t["fecha_llegada"],
            "hora_llegada": llegada_dt.strftime("%H:%M"),
            "minutos_esperando": minutos,
            "cliente_nombre": nombre_cliente,
            "nombre_cliente": nombre_cliente,
            "fecha_atencion": t["fecha_atencion"],
            "fecha_fin": t["fecha_fin"],
            "motivo": t["motivo"],
            "detalle": t["detalle"],
            "pedido_id": t["pedido_id"],
        }

    @app.get("/admin/turnos/api/lista")
    def admin_turnos_lista():
        requiere_admin()
        conn = db()
        servicios = conn.execute("SELECT * FROM turnos_servicios WHERE activo = 1 ORDER BY id").fetchall()
        ahora_dt = datetime.now()
        resultado = []
        for s in servicios:
            info = _estado_servicio(conn, s)
            filas_activos = conn.execute("""
                SELECT t.id, t.servicio_id, t.numero, t.estado, t.fecha_llegada, t.fecha_atencion, t.fecha_fin,
                       t.motivo, t.detalle, t.pedido_id,
                       c.nombre AS cuenta_nombre, c.apellido AS cuenta_apellido
                FROM turnos t
                LEFT JOIN cuentas c ON c.id = t.cuenta_id
                WHERE t.servicio_id = ? AND t.estado IN ('espera', 'atendiendo')
                ORDER BY t.id ASC
            """, (s["id"],)).fetchall()
            activos = [_formatear_turno_admin(t, ahora_dt) for t in filas_activos]

            filas_fin = conn.execute("""
                SELECT t.id, t.servicio_id, t.numero, t.estado, t.fecha_llegada, t.fecha_atencion, t.fecha_fin,
                       t.motivo, t.detalle, t.pedido_id,
                       c.nombre AS cuenta_nombre, c.apellido AS cuenta_apellido
                FROM turnos t
                LEFT JOIN cuentas c ON c.id = t.cuenta_id
                WHERE t.servicio_id = ? AND t.estado = 'finalizado'
                ORDER BY t.id DESC LIMIT 20
            """, (s["id"],)).fetchall()
            finalizados = [_formatear_turno_admin(t, ahora_dt) for t in filas_fin]

            info["turnos"] = activos
            info["activos"] = activos
            info["finalizados"] = finalizados
            resultado.append(info)
        return jsonify(resultado)

    @app.put("/admin/turnos/api/servicio/<int:servicio_id>")
    def configurar_servicio(servicio_id):
        cuenta = requiere_admin()
        servicio = _servicio(servicio_id)
        datos = json_del_cuerpo()
        asesores = datos.get("asesores", servicio["asesores"])
        meta = datos.get("meta_espera_min", servicio["meta_espera_min"])
        errores = []
        if not _entero(asesores, 1, 20):
            errores.append("asesores: debe ser un entero entre 1 y 20")
        if not _entero(meta, 1, 60):
            errores.append("meta_espera_min: debe ser un entero entre 1 y 60")
        if errores:
            raise ErrorApi(422, "Datos inválidos", errores)
        db().execute("UPDATE turnos_servicios SET asesores = ?, meta_espera_min = ? WHERE id = ?",
                     (asesores, meta, servicio_id))
        auditar(f"Servicio '{servicio['nombre']}': {asesores} asesores, meta de espera {meta} min (por {cuenta['email']})")
        return jsonify({"mensaje": "Capacidad actualizada", "asesores": asesores, "meta_espera_min": meta})

    @app.get("/turnos")
    def pagina_turnos():
        return send_from_directory(os.path.join(aqui, "interfaz"), "turnos.html")

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
