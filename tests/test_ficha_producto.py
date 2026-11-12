# -*- coding: utf-8 -*-
"""Pruebas de la Tarea 5: Marca, descripción y ficha técnica de productos."""
import json
import sqlite3
import pytest

from app import create_app
from sembrar_datos import sembrar

ADM = {
    "nombre": "Admin Ficha",
    "email": "admin_ficha@example.com",
    "password": "clave-admin-123",
    "codigo": "clave-admin",
    "autorizacion_datos": True,
}


@pytest.fixture()
def app_adm(tmp_path):
    app = create_app({
        "CARGAR_DATASET": True,
        "DB_PATH": str(tmp_path / "test_ficha.db"),
        "API_KEY": "clave-usuario",
        "ADMIN_KEY": "clave-admin",
        "TESTING": True,
    })
    cli = app.test_client()
    r = cli.post("/admin/registro", json=ADM)
    assert r.status_code == 201
    return app, cli


def test_crear_producto_con_ficha_valida(app_adm):
    _, cli = app_adm
    ficha = [
        {"atributo": "Marca", "valor": "NovaTech"},
        {"atributo": "Almacenamiento", "valor": "256 GB"},
        {"atributo": "Pantalla", "valor": "6.7 pulgadas AMOLED"},
        {"atributo": "Garantía", "valor": "24 meses"},
    ]
    r = cli.post("/admin/catalogo/productos", json={
        "nombre": "Smartphone NovaTech Pro",
        "categoria": "Technology",
        "subcategoria": "Phones",
        "precio": 499.99,
        "stock": 25,
        "marca": "NovaTech",
        "descripcion": "Smartphone de última generación con cámara triple y carga ultrarrápida.",
        "ficha": ficha,
    })
    assert r.status_code == 201
    datos = r.get_json()
    assert datos["marca"] == "NovaTech"
    assert "Smartphone de última generación" in datos["descripcion"]
    assert datos["ficha"] == ficha

    # Crear con formato de pares [atributo, valor] también es válido
    ficha_pares = [
        ["Color", "Negro Titanio"],
        ["Material", "Aluminio y cristal"],
    ]
    r2 = cli.post("/admin/catalogo/productos", json={
        "nombre": "Funda NovaTech Shield",
        "categoria": "Technology",
        "subcategoria": "Accessories",
        "precio": 29.99,
        "stock": 50,
        "marca": "NovaTech",
        "descripcion": "Funda de protección contra caídas.",
        "ficha": ficha_pares,
    })
    assert r2.status_code == 201
    datos2 = r2.get_json()
    assert datos2["ficha"] == [
        {"atributo": "Color", "valor": "Negro Titanio"},
        {"atributo": "Material", "valor": "Aluminio y cristal"},
    ]


def test_crear_producto_con_ficha_invalida_422(app_adm):
    _, cli = app_adm
    base = {
        "nombre": "Producto Invalido",
        "categoria": "Technology",
        "subcategoria": "Phones",
        "precio": 99.99,
        "stock": 10,
    }

    # Ficha no es lista ni JSON válido
    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha="no-es-json"))
    assert r.status_code == 422
    assert "ficha" in str(r.get_json()["detalles"])

    # Ficha como número o booleano
    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha=12345))
    assert r.status_code == 422

    # Ficha con más de 20 atributos
    mas_de_20 = [{"atributo": f"Attr{i}", "valor": f"Val{i}"} for i in range(21)]
    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha=mas_de_20))
    assert r.status_code == 422
    assert any("máximo 20" in e for e in r.get_json()["detalles"])

    # Ficha con elementos que no son pares válidos
    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha=[{"atributo": "SoloAttr"}]))
    assert r.status_code == 422

    # Ficha con valores vacíos o no-strings
    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha=[{"atributo": "Color", "valor": "   "}]))
    assert r.status_code == 422

    r = cli.post("/admin/catalogo/productos", json=dict(base, ficha=[{"atributo": "Color", "valor": 123}]))
    assert r.status_code == 422

    # Descripción con más de 2000 caracteres
    r = cli.post("/admin/catalogo/productos", json=dict(base, descripcion="A" * 2001))
    assert r.status_code == 422
    assert any("máximo 2000" in e for e in r.get_json()["detalles"])


def test_editar_producto_marca_descripcion_ficha(app_adm):
    _, cli = app_adm
    # Crear producto inicial
    r = cli.post("/admin/catalogo/productos", json={
        "nombre": "Silla Ergonómica Nova",
        "categoria": "Furniture",
        "subcategoria": "Chairs",
        "precio": 149.99,
        "stock": 15,
        "marca": "NovaCasa",
        "descripcion": "Silla inicial.",
        "ficha": [{"atributo": "Material", "valor": "Malla"}],
    })
    assert r.status_code == 201
    pid = r.get_json()["product_id"]

    # Editar marca, descripción y ficha
    nueva_ficha = [
        {"atributo": "Material", "valor": "Malla transpirable y marco de acero"},
        {"atributo": "Garantía", "valor": "3 años"},
        {"atributo": "Carga máxima", "valor": "130 kg"},
    ]
    r_put = cli.put(f"/admin/catalogo/productos/{pid}", json={
        "marca": "ConfortLine",
        "descripcion": "Silla ergonómica de alto soporte lumbar para largas jornadas de trabajo.",
        "ficha": nueva_ficha,
    })
    assert r_put.status_code == 200
    datos = r_put.get_json()
    assert datos["marca"] == "ConfortLine"
    assert "alto soporte lumbar" in datos["descripcion"]
    assert datos["ficha"] == nueva_ficha

    # Consultar detalle en endpoint de admin
    r_ver = cli.get(f"/admin/catalogo/productos/{pid}")
    assert r_ver.status_code == 200
    assert r_ver.get_json()["marca"] == "ConfortLine"
    assert r_ver.get_json()["ficha"] == nueva_ficha


def test_detalle_publico_devuelve_campos(app_adm):
    _, cli = app_adm
    ficha = [
        {"atributo": "Color", "valor": "Azul"},
        {"atributo": "Garantía", "valor": "1 año"},
    ]
    r = cli.post("/admin/catalogo/productos", json={
        "nombre": "Lámpara Led Nova",
        "categoria": "Furniture",
        "subcategoria": "Furnishings",
        "precio": 45.00,
        "stock": 20,
        "marca": "DecoHome",
        "descripcion": "Lámpara de luz cálida regulable.",
        "ficha": ficha,
    })
    assert r.status_code == 201
    pid = r.get_json()["product_id"]

    # 1. En listado de la tienda (/tienda/productos)
    r_tienda = cli.get("/tienda/productos?q=L%C3%A1mpara%20Led%20Nova")
    assert r_tienda.status_code == 200
    items = r_tienda.get_json()["resultados"]
    assert len(items) == 1
    prod_t = items[0]
    assert prod_t["marca"] == "DecoHome"
    assert prod_t["descripcion"] == "Lámpara de luz cálida regulable."
    assert prod_t["ficha"] == ficha

    # 2. En detalle público individual (/tienda/productos/<product_id>)
    r_det = cli.get(f"/tienda/productos/{pid}")
    assert r_det.status_code == 200
    det = r_det.get_json()
    assert det["product_id"] == pid
    assert det["marca"] == "DecoHome"
    assert det["descripcion"] == "Lámpara de luz cálida regulable."
    assert det["ficha"] == ficha

    # 3. En listado con API key (/productos)
    r_api = cli.get("/productos?q=L%C3%A1mpara%20Led%20Nova", headers={"X-API-Key": "clave-usuario"})
    assert r_api.status_code == 200
    api_items = r_api.get_json()["resultados"]
    assert len(api_items) == 1
    assert api_items[0]["marca"] == "DecoHome"
    assert api_items[0]["ficha"] == ficha


def test_sembrar_datos_llena_campos_para_todos(tmp_path):
    db_file = str(tmp_path / "sembrado.db")
    agregados = sembrar(db_file)
    assert agregados == 120

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    prods = conn.execute("SELECT product_id, product_name, category, marca, descripcion, ficha FROM productos WHERE product_id LIKE 'ME-%'").fetchall()
    assert len(prods) == 120

    marcas_vistas = set()
    for p in prods:
        # Marca obligatoria y no vacía
        assert p["marca"] and isinstance(p["marca"], str) and len(p["marca"].strip()) > 1
        marcas_vistas.add(p["marca"])

        # Descripción corta obligatoria y máx. 2000
        assert p["descripcion"] and 10 <= len(p["descripcion"]) <= 2000

        # Ficha técnica JSON con entre 3 y 6 atributos
        ficha = json.loads(p["ficha"])
        assert isinstance(ficha, list)
        assert 3 <= len(ficha) <= 6, f"Ficha fuera de rango (3-6) en {p['product_name']}: {len(ficha)}"
        for attr in ficha:
            assert "atributo" in attr and "valor" in attr
            assert attr["atributo"].strip() and attr["valor"].strip()

        # Coherencia por categoría
        nombres_attr = [a["atributo"].lower() for a in ficha]
        if p["category"] == "Tecnología":
            assert any(k in nombres_attr for k in ("marca", "almacenamiento", "pantalla", "garantía", "conectividad", "tipo", "sonido"))
        elif p["category"] == "Moda":
            assert any(k in nombres_attr for k in ("marca", "talla", "material", "color", "género"))
        elif p["category"] == "Electrodomésticos":
            assert any(k in nombres_attr for k in ("marca", "potencia", "capacidad", "garantía", "niveles"))
        elif p["category"] == "Hogar":
            assert any(k in nombres_attr for k in ("marca", "material", "medidas", "medidas / formato", "color", "presentación"))

    # Las marcas son ficticias o genéricas
    marcas_reales_prohibidas = {"samsung", "apple", "sony", "nike", "adidas", "lg", "oster", "ikea", "dell", "hp"}
    for m in marcas_vistas:
        assert m.lower() not in marcas_reales_prohibidas


def test_compatibilidad_productos_existentes_sin_ficha(app_adm):
    """Productos creados antes o sin datos opcionales deben funcionar igual."""
    app, cli = app_adm
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute(
        "INSERT INTO productos (product_id, product_name, category, sub_category, precio, stock, activo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("OLD-001", "Producto Antiguo Sin Ficha", "Furniture", "Chairs", 50.0, 10, 1)
    )
    conn.commit()
    conn.close()

    # En la tienda pública se lista y el detalle funciona sin error
    r_lista = cli.get("/tienda/productos?q=Producto%20Antiguo")
    assert r_lista.status_code == 200
    res = r_lista.get_json()["resultados"]
    assert len(res) == 1
    assert res[0]["marca"] is None
    assert res[0]["descripcion"] is None
    assert res[0]["ficha"] == []

    # En el detalle individual
    r_det = cli.get("/tienda/productos/OLD-001")
    assert r_det.status_code == 200
    assert r_det.get_json()["marca"] is None
    assert r_det.get_json()["ficha"] == []
