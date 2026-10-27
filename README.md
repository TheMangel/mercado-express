# Mercado Express · API

API REST en Python (Flask) sobre la base de datos SQLite del Momento Evaluativo 1.
Pone en práctica lo investigado en el M2-A3: validación + transacciones, consultas con índices, y la Ley 1581 de 2012.

## Cómo correrla (local)

```bash
pip install -r requirements.txt

# Define tus claves (si no lo haces, la app genera unas al azar y las imprime al arrancar)
export API_KEY=mi-clave-de-usuario
export ADMIN_KEY=mi-clave-de-admin

python app.py          # http://127.0.0.1:5000
```

Luego abre en el navegador:

- **http://127.0.0.1:5000** → la **tienda pública** (no pide clave): catálogo con búsqueda, categorías, orden, detalle de producto y carrito (se guarda en el navegador). Estilo de cristal (como Apple) y animaciones suaves; tema, idioma y moneda se cambian en la rueda de configuración. "Finalizar compra" llega hasta un aviso: el pago aún no está hecho.
- **http://127.0.0.1:5000/admin/catalogo** → la **sección de administradores** (otro enlace, solo para cuentas de administrador): crear categorías, subcategorías y productos nuevos. Aparecen al instante en la tienda.
- **http://127.0.0.1:5000/admin** → el **panel de gestión** (pide clave de usuario o de administrador): resumen, clientes, productos, pedidos (tiquete de venta), reportes y, con la clave de administrador, datos completos y auditoría.

### Cuenta de cliente y dirección de envío

Para crear una cuenta, la tienda pide **nombre, apellido, correo, teléfono, contraseña y una dirección** (país → departamento/región → ciudad o municipio, dirección y código postal). Las listas de países, regiones y ciudades salen de `ubicaciones.py` (Colombia con sus 33 departamentos y ciudades principales; Estados Unidos con sus estados; otros países con región y ciudad escritas a mano). El servidor valida el país, la región, el teléfono y el formato del código postal (6 dígitos en Colombia, 5 en EE. UU.).

Al **finalizar la compra** con sesión iniciada, la persona elige entre su **dirección registrada** o **enviar a otra dirección** (editable, y con la opción de guardarla como dirección principal). Esta parte llega hasta confirmar la dirección: todavía no se crea el pedido ni hay pago.

Ley 1581: la autorización incluye el teléfono y la dirección, que solo se usan para gestionar envíos; cada persona solo ve y cambia los de su propia cuenta.

### Cómo entrar a la sección de administradores

1. Abre `/admin/catalogo` → **Crear cuenta de administrador**.
2. Llena nombre, correo y contraseña (mínimo 8 caracteres). En **código de administrador** escribe el valor de `ADMIN_KEY` (si no la definiste, el servidor imprime una al arrancar). Sin ese código nadie puede volverse administrador desde la web.
3. Las siguientes veces solo usa **Iniciar sesión** con tu correo y contraseña.

Solo una sesión de administrador puede crear categorías o productos; una cuenta de cliente o una persona sin sesión recibe error (401/403). Cada creación queda en la auditoría.

**Aviso de pocas unidades:** la tienda muestra cuántas unidades quedan solo cuando el stock es de 50 o menos («Quedan N unidades»); con 10 o menos el aviso se resalta («¡Últimas N unidades!») y con 0 dice «Agotado». Con más de 50 no se muestra nada y la API ni siquiera envía el número. Los límites son `STOCK_AVISO` (50) y `STOCK_CRITICO` (10) al inicio de `app.py`.

Los productos del dataset no traen precio: se calcula del historial como `ventas / (cantidad × (1 − descuento))`, promediado por producto. Los productos creados desde `/admin/catalogo` sí guardan su **precio propio en USD** (columna `productos.precio`) y ese precio manda. Las imágenes son ilustraciones propias (color + dibujo por subcategoría), no fotos.

La primera vez, la app crea `mercado_express.db` leyendo `data/datos_originales.csv` (el CSV original no se modifica).
Para rehacer la base desde cero: borra `mercado_express.db` y vuelve a arrancar.

Pruebas automáticas: `python -m pytest -q`

## Autenticación

Todas las rutas piden la cabecera `X-API-Key`, excepto las públicas: `/salud`, las páginas (`/`, `/admin`, `/admin/catalogo`), el catálogo `/tienda/*` (solo datos de productos, nada personal), las cuentas `/cuenta/*` y la sección de administradores `/admin/registro|entrar|yo` y `/admin/catalogo/*`. Estas últimas **no usan la clave**: usan una cuenta con rol de administrador y su sesión (cookie `HttpOnly`).

| Clave | Qué puede hacer |
|---|---|
| `API_KEY` (usuario) | Consultar, reportes, registrar pedidos y clientes. Nombres siempre enmascarados. |
| `ADMIN_KEY` (admin) | Todo lo anterior + datos personales completos con `?completo=1` + ver `/admin/auditoria`. |

## Rutas

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Tienda pública |
| GET | `/admin` | Panel de gestión (la página es pública; los datos piden clave) |
| GET | `/tienda/productos` | Catálogo público. Filtros: `q`, `categoria`, `subcategoria`, `orden` (`vendidos`, `recientes`, `precio_asc`, `precio_desc`, `nombre`), `pagina`, `limite` |
| GET | `/tienda/categorias` | Categorías y subcategorías (con color, dibujo y número de productos; incluye las vacías) |
| GET | `/tienda/ubicaciones` | Países, departamentos/regiones y ciudades para los filtros de dirección (`ubicaciones.py`) |
| POST | `/cuenta/registro` | Crea cuenta de cliente: nombre, apellido, correo, teléfono, contraseña, dirección (país, región, ciudad, dirección, código postal) y autorización Ley 1581 |
| POST | `/cuenta/entrar` · `/cuenta/salir` | Inicia / cierra sesión (cookie `HttpOnly`) |
| GET | `/cuenta/yo` | Perfil de la cuenta con sesión (sin la contraseña) |
| PUT | `/cuenta/direccion` | Cambia la dirección principal y el teléfono de la cuenta con sesión |
| GET | `/tienda/resumen` | Cifras generales para la portada |
| GET | `/salud` | Estado del servicio (pública) |
| GET | `/resumen` | Totales: clientes, productos, pedidos, ventas, ganancia |
| GET | `/clientes` | Lista paginada. Filtros: `region`, `segmento`, `q`, `pagina`, `limite` |
| GET | `/clientes/<id>` | Un cliente + su número de pedidos |
| POST | `/clientes` | Crea cliente (exige `"autorizacion_datos": true`) |
| GET | `/productos` | Lista paginada. Filtros: `categoria`, `q` |
| GET | `/pedidos` | Lista paginada. Filtro: `cliente_id` |
| GET | `/pedidos/<order_id>` | Un pedido con sus líneas y total |
| POST | `/pedidos` | **Registra un pedido (transacción)** |
| GET | `/reportes/ventas-por-categoria` | Consulta 1 del E1 |
| GET | `/reportes/ganancia-por-cliente` | Consulta 2 del E1 |
| GET | `/reportes/envio-por-region` | Consulta 3 del E1 (16 filas: 4 modos × 4 regiones) |
| GET | `/admin/auditoria` | Últimos accesos a datos personales completos (solo admin) |
| GET | `/admin/catalogo` | Página de administración del catálogo |
| POST | `/admin/registro` | Crea una cuenta de administrador (exige el código = `ADMIN_KEY`, máx. 5 intentos fallidos cada 10 min) |
| POST | `/admin/entrar` | Inicia sesión de administrador (error genérico si la cuenta no es admin) |
| GET | `/admin/yo` | Dice si hay sesión y si es de administrador |
| GET | `/admin/catalogo/datos` | Categorías, totales y últimos productos creados (**sesión admin**) |
| POST | `/admin/catalogo/categorias` | Crea categoría: `nombre`, `tono` (`mue`,`tec`,`ofi`,`nat`,`mar`) (**sesión admin**) |
| POST | `/admin/catalogo/subcategorias` | Crea subcategoría: `categoria`, `nombre`, `icono` (**sesión admin**) |
| POST | `/admin/catalogo/productos` | Crea producto: `nombre`, `categoria`, `subcategoria`, `precio` en USD (**sesión admin**) |

### Ejemplo: registrar un pedido

```bash
curl -X POST http://127.0.0.1:5000/pedidos \
  -H "X-API-Key: mi-clave-de-usuario" -H "Content-Type: application/json" \
  -d '{
    "customer_id": "CG-12520",
    "modo_envio": "Standard Class",
    "items": [{
      "product_id": "FUR-BO-10001798",
      "product_name": "Bush Somerset Collection Bookcase",
      "sales": 150.0, "quantity": 1, "discount": 0.0, "profit": 30.0
    }]
  }'
```

`modo_envio`: `Standard Class`, `Second Class`, `First Class` o `Same Day`.
`fecha_pedido` (por defecto hoy) y `fecha_envio` (opcional) usan formato `AAAA-MM-DD`.
Si algo falla (cliente o producto inexistente, cantidad negativa...), **no se guarda nada**.

## Dónde está cada idea del proyecto

| Idea | Dónde |
|---|---|
| Validación en la app + reglas en la base | `validar_pedido()` en `app.py` y `CHECK`/`FOREIGN KEY` en `esquema.py` |
| Transacción COMMIT / ROLLBACK | `crear_pedido()` en `app.py` |
| Llave compuesta de productos (ID repetido) | tabla `productos` en `esquema.py` |
| Índice SCAN → SEARCH | `idx_detalle_order_id` en `esquema.py` |
| Pipeline reproducible desde el CSV | `cargar_datos.py` |
| Ley 1581: minimización, autorización, confidencialidad, trazabilidad | nombres enmascarados, `autorizacion_datos`, claves por nivel, tabla `auditoria` |
| Roles y control de acceso | `cuentas.rol` (cliente/admin), `requiere_admin()` en `app.py` |
| Catálogo administrable | tablas `categorias`, `subcategorias`, columna `productos.precio`; `migrar()` actualiza bases viejas sin perder datos |

## Despliegue en Render

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn "app:create_app()"`
- Variables de entorno: `API_KEY` y `ADMIN_KEY` (obligatorio definirlas, nunca las subas a GitHub).
- Ojo: en el plan gratuito el disco es temporal. La base se reconstruye desde el CSV en cada despliegue o reinicio, así que los pedidos nuevos no se conservan. Para que persistan haría falta un disco persistente o una base externa.

## Límites conocidos (versión 1)

- Las ventas (`sales`) de cada línea las envía quien registra el pedido; la app no las calcula a partir de un precio.
- Los clientes históricos del dataset no tienen autorización registrada (`autorizacion_datos` vacío); solo los creados desde la app la guardan.
- No hay límite de peticiones por minuto ni HTTPS propio (Render lo da). Como el catálogo es público, antes de publicarlo en internet conviene agregar un límite de peticiones.
- La compra en la tienda se detiene antes del pago: falta conectar el carrito con `POST /pedidos`.
- Las cuentas creadas antes de esta versión no tienen teléfono ni dirección: la primera vez que compran, la tienda les pide la dirección. Las listas de ciudades son las principales de cada departamento; para otra se elige «Otra (escribir)».
- La sección de administradores permite **crear**, pero todavía no editar ni borrar categorías o productos.
- El código para volverse administrador es `ADMIN_KEY`: quien lo conozca puede crear cuentas de administrador. En producción conviene definirla larga y secreta (y cambiarla si se filtra).
- Las animaciones de apertura y cierre de ventanas usan funciones recientes del navegador (Chrome/Edge 117+, Safari 17.4+, Firefox 129+); en uno más viejo todo funciona, solo que sin ese fundido.
