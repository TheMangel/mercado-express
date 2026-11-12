# -*- coding: utf-8 -*-
"""Carga el catálogo de ejemplo de Mercado Express: 4 categorías y 120 productos (30 por categoría).

Uso (con la carpeta del proyecto abierta):   python sembrar_datos.py
  - Se puede repetir sin problema: lo que ya existe no se duplica ni se cambia.
  - Usa la misma base que la app (variable DB_PATH, o mercado_express.db junto a este archivo).
  - Los precios están pensados en pesos colombianos (COP). La base los guarda en USD, que es lo que
    la tienda espera, usando la tasa TASA_COP (3250 por defecto). Se guardan con 6 decimales para que
    al pasar la tienda a COP salga el precio exacto de la lista, sin centavos de diferencia.
"""
import json
import os
import random
import re
import sqlite3
from datetime import datetime

from app import migrar

# categoría -> (código, tono de color, {subcategoría: (ícono, [(producto, precio en COP), ...])})
CATALOGO = {
    "Hogar": ("HOG", "mue", {
        "Sala y comedor": ("chair", [
            ("Sofá de 3 puestos en tela gris", 1899900), ("Sillón reclinable individual", 1249900),
            ("Mesa de centro de madera", 389900), ("Comedor de 4 puestos con sillas", 1390000),
            ("Silla de comedor acolchada", 189900), ("Mueble para televisor hasta 55 pulgadas", 649900)]),
        "Dormitorio": ("box", [
            ("Cama doble con cabecero", 1099900), ("Colchón semiortopédico queen", 1549900),
            ("Almohada viscoelástica", 89900), ("Juego de sábanas de algodón queen", 149900),
            ("Closet de 3 puertas", 1299900), ("Mesa de noche con cajón", 219900)]),
        "Cocina y mesa": ("table", [
            ("Set de ollas antiadherentes 10 piezas", 329900), ("Juego de vasos de vidrio x12", 69900),
            ("Vajilla de cerámica 24 piezas", 259900), ("Set de cuchillos de cocina x6", 119900),
            ("Tabla de picar de bambú", 49900), ("Juego de cubiertos de acero x24", 99900)]),
        "Iluminación y decoración": ("lamp", [
            ("Lámpara de pie minimalista", 179900), ("Lámpara de mesa con pantalla", 99900),
            ("Cortina blackout de 2 paneles", 139900), ("Espejo decorativo redondo 60 cm", 159900),
            ("Tapete para sala 160 x 230 cm", 349900), ("Cojín decorativo x2", 59900)]),
        "Organización y baño": ("shelf", [
            ("Estantería de 5 niveles", 249900), ("Organizador de zapatos para 12 pares", 129900),
            ("Cesto de ropa plegable", 69900), ("Juego de toallas x4", 129900),
            ("Cortina de baño antihongos", 54900), ("Cajas organizadoras con tapa x3", 79900)]),
    }),
    "Electrodomésticos": ("ELE", "ofi", {
        "Cocina pequeña": ("table", [
            ("Licuadora de 1,5 L con 10 velocidades", 189900), ("Freidora de aire de 4 L", 359900),
            ("Cafetera de goteo 12 tazas", 139900), ("Tostadora de 2 ranuras", 89900),
            ("Olla arrocera de 1,8 L", 129900), ("Horno microondas de 20 L", 399900),
            ("Batidora de mano", 99900), ("Hervidor eléctrico de 1,7 L", 79900)]),
        "Grandes electrodomésticos": ("box", [
            ("Nevera no frost de 300 L", 1999900), ("Nevera de dos puertas de 420 L", 3049900),
            ("Lavadora automática de 15 kg", 1799900), ("Estufa a gas de 4 puestos", 899900),
            ("Horno empotrable eléctrico", 1349900), ("Campana extractora de 60 cm", 549900),
            ("Lavavajillas de 12 servicios", 2299900)]),
        "Cuidado de la ropa y del hogar": ("printer", [
            ("Plancha de vapor 1.600 W", 119900), ("Aspiradora con bolsa 1.800 W", 329900),
            ("Aspiradora robot", 849900), ("Secadora de ropa de 10 kg", 1899900),
            ("Planchador vertical de vapor", 189900), ("Purificador de aire", 549900),
            ("Secador de cabello 2.000 W", 119900), ("Cortadora de cabello recargable", 109900)]),
        "Climatización": ("lamp", [
            ("Ventilador de pedestal de 16 pulgadas", 139900), ("Ventilador de torre", 109900),
            ("Aire acondicionado de 12.000 BTU", 1749900), ("Calentador de agua a gas de 12 L", 699900),
            ("Humidificador ultrasónico", 129900), ("Deshumidificador de 20 L", 799900),
            ("Calefactor eléctrico de cerámica", 149900)]),
    }),
    "Tecnología": ("TEC", "tec", {
        "Celulares y tablets": ("phone", [
            ("Smartphone 128 GB pantalla de 6,5 pulgadas", 899900), ("Smartphone 256 GB gama alta", 3299900),
            ("Smartphone 64 GB económico", 549900), ("Tablet de 10 pulgadas 64 GB", 699900),
            ("Tablet de 8 pulgadas para niños con estuche", 379900), ("Reloj inteligente con GPS", 449900)]),
        "Computadores": ("mouse", [
            ("Portátil 15,6 pulgadas 8 GB RAM 512 GB SSD", 2299900), ("Portátil 14 pulgadas ultraligero 16 GB RAM", 3399900),
            ("Computador todo en uno de 24 pulgadas", 2699900), ("Monitor de 24 pulgadas Full HD", 549900),
            ("Monitor curvo de 27 pulgadas 75 Hz", 849900), ("Disco duro externo de 1 TB", 239900)]),
        "Audio y video": ("box", [
            ("Televisor Smart de 43 pulgadas 4K", 1399900), ("Televisor Smart de 55 pulgadas 4K", 1899900),
            ("Barra de sonido 2.1 con bluetooth", 399900), ("Audífonos inalámbricos con cancelación de ruido", 349900),
            ("Parlante bluetooth portátil", 159900), ("Dispositivo de streaming 4K", 189900)]),
        "Accesorios y gaming": ("mouse", [
            ("Mouse inalámbrico ergonómico", 59900), ("Teclado mecánico retroiluminado", 219900),
            ("Audífonos gamer con micrófono", 179900), ("Silla gamer reclinable", 699900),
            ("Cargador rápido USB-C de 65 W", 89900), ("Memoria USB de 128 GB", 49900)]),
        "Impresión y oficina": ("printer", [
            ("Impresora multifuncional de tanque de tinta", 799900), ("Impresora láser monocromática", 649900),
            ("Router Wi-Fi 6 doble banda", 249900), ("Cámara web Full HD", 129900),
            ("Soporte ajustable para portátil", 79900), ("Regulador de voltaje de 8 tomas", 99900)]),
    }),
    "Moda": ("MOD", "mar", {
        "Ropa de mujer": ("box", [
            ("Blusa de manga larga en satín", 89900), ("Jean tiro alto skinny", 139900),
            ("Vestido midi estampado", 159900), ("Chaqueta de cuero sintético para mujer", 249900),
            ("Buso con capota para mujer", 119900)]),
        "Calzado de mujer": ("box", [
            ("Tenis blancos casuales para mujer", 189900), ("Sandalias de plataforma", 149900),
            ("Botines de tacón medio", 229900), ("Zapatos de tacón de 8 cm", 199900),
            ("Zapatillas deportivas para correr de mujer", 259900)]),
        "Accesorios de mujer": ("box", [
            ("Bolso de mano de cuero sintético", 169900), ("Mochila urbana para mujer", 139900),
            ("Gafas de sol con filtro UV para mujer", 79900), ("Cinturón de cuero delgado para mujer", 59900),
            ("Reloj análogo para mujer", 189900)]),
        "Ropa de hombre": ("box", [
            ("Camisa de lino de manga larga", 119900), ("Jean clásico corte recto", 149900),
            ("Camisetas básicas de algodón x3", 99900), ("Chaqueta impermeable para hombre", 279900),
            ("Pantalón jogger deportivo", 109900)]),
        "Calzado de hombre": ("box", [
            ("Tenis deportivos de running para hombre", 269900), ("Zapatos formales de cuero", 289900),
            ("Botas de trabajo con puntera", 329900), ("Sandalias deportivas para hombre", 99900),
            ("Mocasines casuales", 199900)]),
        "Accesorios de hombre": ("box", [
            ("Billetera de cuero", 89900), ("Cinturón de cuero reversible", 79900),
            ("Gorra ajustable", 49900), ("Reloj digital deportivo", 159900), ("Morral de viaje", 189900)]),
    }),
}


def _stock(azar):
    """Mezcla realista: pocos agotados, algunos con pocas unidades y la mayoría con buen inventario."""
    r = azar.random()
    if r < 0.05:
        return 0
    if r < 0.17:
        return azar.randint(1, 10)
    if r < 0.35:
        return azar.randint(11, 50)
    return azar.randint(51, 200)


def detalles_producto(categoria, sub, nombre):
    """Genera marca ficticia, descripción corta y ficha técnica (3 a 6 atributos) coherente con la categoría."""
    MARCAS = {
        "Hogar": ["CasaViva", "DecoHogar", "MaderArte", "ConfortHome", "NovaCasa"],
        "Electrodomésticos": ["ElectroMax", "Voltia", "AeroKool", "Klimat", "Sabori", "PureHome"],
        "Tecnología": ["NexTech", "CyberPulse", "AudioWave", "KronoTech", "BytePlus", "VisionView"],
        "Moda": ["AuraModa", "UrbanStyle", "LinoPuro", "Vento", "PasoFirme", "CueroReal"]
    }
    marcas_cat = MARCAS.get(categoria, ["Genérico"])
    indice = sum(ord(c) for c in nombre) % len(marcas_cat)
    marca = marcas_cat[indice]
    ficha = []

    if categoria == "Tecnología":
        ficha.append({"atributo": "Marca", "valor": marca})
        m_alm = re.search(r"(\d+\s*(?:GB|TB)(?:\s*SSD)?)", nombre, re.I)
        if m_alm:
            ficha.append({"atributo": "Almacenamiento", "valor": m_alm.group(1).upper()})
        elif "celular" in sub.lower() or "tablet" in sub.lower() or "smartphone" in nombre.lower():
            ficha.append({"atributo": "Almacenamiento", "valor": "128 GB"})
        elif "computador" in sub.lower() or "portátil" in nombre.lower():
            ficha.append({"atributo": "Almacenamiento", "valor": "512 GB SSD"})
        elif "disco" in nombre.lower() or "memoria" in nombre.lower():
            ficha.append({"atributo": "Almacenamiento", "valor": "1 TB"})
        else:
            ficha.append({"atributo": "Tipo", "valor": sub})

        m_pan = re.search(r"(\d+(?:,\d+)?\s*pulgadas)", nombre, re.I)
        if m_pan:
            ficha.append({"atributo": "Pantalla", "valor": m_pan.group(1)})
        elif "reloj" in nombre.lower():
            ficha.append({"atributo": "Pantalla", "valor": "1.4 pulgadas AMOLED"})
        elif "monitor" in nombre.lower():
            ficha.append({"atributo": "Pantalla", "valor": "24 pulgadas Full HD"})
        elif "audio" in sub.lower() or "auricular" in nombre.lower() or "audífono" in nombre.lower():
            ficha.append({"atributo": "Sonido", "valor": "Estéreo Hi-Fi"})
        else:
            ficha.append({"atributo": "Conectividad", "valor": "USB / Inalámbrico"})

        ficha.append({"atributo": "Garantía", "valor": "12 meses"})
        desc = f"{nombre}. Producto tecnológico de alto rendimiento, confiabilidad y diseño moderno para trabajo o entretenimiento."

    elif categoria == "Moda":
        ficha.append({"atributo": "Marca", "valor": marca})
        if "mujer" in nombre.lower() or "mujer" in sub.lower():
            genero = "Mujer"
        elif "hombre" in nombre.lower() or "hombre" in sub.lower():
            genero = "Hombre"
        else:
            genero = "Unisex"
        ficha.append({"atributo": "Género", "valor": genero})

        if "calzado" in sub.lower() or "tenis" in nombre.lower() or "zapatos" in nombre.lower() or "botas" in nombre.lower() or "sandalias" in nombre.lower() or "mocasines" in nombre.lower():
            talla = "38" if genero == "Mujer" else "41"
        elif "ropa" in sub.lower() or "jean" in nombre.lower() or "camisa" in nombre.lower() or "vestido" in nombre.lower() or "chaqueta" in nombre.lower() or "buso" in nombre.lower() or "pantalón" in nombre.lower():
            talla = "M"
        else:
            talla = "Única"
        ficha.append({"atributo": "Talla", "valor": talla})

        if "lino" in nombre.lower():
            mat = "100% Lino"
        elif "satín" in nombre.lower():
            mat = "Satín suave"
        elif "cuero" in nombre.lower():
            mat = "Cuero genuino" if "billetera" in nombre.lower() or "zapatos formales" in nombre.lower() else "Cuero sintético premium"
        elif "algodón" in nombre.lower() or "camisetas" in nombre.lower():
            mat = "100% Algodón"
        elif "jean" in nombre.lower():
            mat = "Denim elásticada"
        elif "calzado" in sub.lower() or "tenis" in nombre.lower():
            mat = "Malla transpirable y suela EVA"
        else:
            mat = "Material textil de alta durabilidad"
        ficha.append({"atributo": "Material", "valor": mat})

        if "blanco" in nombre.lower():
            col = "Blanco"
        elif "gris" in nombre.lower():
            col = "Gris"
        elif "azul" in nombre.lower():
            col = "Azul"
        elif "negro" in nombre.lower():
            col = "Negro"
        else:
            col = "Multicolor / Clásico"
        ficha.append({"atributo": "Color", "valor": col})
        desc = f"{nombre}. Prenda confeccionada con materiales seleccionados para ofrecer comodidad, estilo y excelente ajuste en el día a día."

    elif categoria == "Electrodomésticos":
        ficha.append({"atributo": "Marca", "valor": marca})
        m_pot = re.search(r"(\d+(?:\.\d+)?\s*(?:W|BTU))", nombre, re.I)
        if m_pot:
            ficha.append({"atributo": "Potencia", "valor": m_pot.group(1).upper()})
        elif "nevera" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "180 W (Bajo consumo A)"})
        elif "lavadora" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "500 W"})
        elif "estufa" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "Gas natural / 4 quemadores"})
        elif "horno" in nombre.lower() or "freidora" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "1500 W"})
        elif "licuadora" in nombre.lower() or "batidora" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "600 W"})
        elif "ventilador" in nombre.lower():
            ficha.append({"atributo": "Potencia", "valor": "65 W"})
        else:
            ficha.append({"atributo": "Potencia", "valor": "1200 W"})

        m_cap = re.search(r"(\d+(?:,\d+)?\s*(?:L|kg|tazas|servicios|puestos|pulgadas))", nombre, re.I)
        if m_cap:
            ficha.append({"atributo": "Capacidad", "valor": m_cap.group(1)})
        elif "aspiradora" in nombre.lower():
            ficha.append({"atributo": "Capacidad", "valor": "2.5 L"})
        elif "purificador" in nombre.lower() or "deshumidificador" in nombre.lower():
            ficha.append({"atributo": "Capacidad", "valor": "Área hasta 30 m²"})
        elif "secador" in nombre.lower() or "cortadora" in nombre.lower() or "plancha" in nombre.lower():
            ficha.append({"atributo": "Niveles", "valor": "2 a 3 velocidades/temperaturas"})
        else:
            ficha.append({"atributo": "Capacidad", "valor": "Estándar doméstico"})

        ficha.append({"atributo": "Garantía", "valor": "2 años" if "nevera" in nombre.lower() or "lavadora" in nombre.lower() else "1 año"})
        desc = f"{nombre}. Electrodoméstico confiable y eficiente diseñado para brindar practicidad, ahorro energético y gran durabilidad en el hogar."

    else:  # Hogar
        ficha.append({"atributo": "Marca", "valor": marca})
        if "madera" in nombre.lower():
            mat = "Madera maciza tratada"
        elif "tela" in nombre.lower() or "algodón" in nombre.lower() or "lino" in nombre.lower():
            mat = "Tejido suave de alta resistencia"
        elif "vidrio" in nombre.lower():
            mat = "Vidrio templado"
        elif "cerámica" in nombre.lower():
            mat = "Cerámica esmaltada"
        elif "acero" in nombre.lower():
            mat = "Acero inoxidable"
        elif "bambú" in nombre.lower():
            mat = "Bambú natural ecológico"
        elif "antiadherente" in nombre.lower() or "ollas" in nombre.lower():
            mat = "Aluminio con recubrimiento antiadherente"
        elif "viscoelástica" in nombre.lower() or "colchón" in nombre.lower():
            mat = "Espuma de alta densidad y tejido transpirable"
        else:
            mat = "Estructura resistente y duradera"
        ficha.append({"atributo": "Material", "valor": mat})

        m_med = re.search(r"(\d+(?:\s*x\s*\d+)*(?:\s*cm|\s*pulgadas|\s*puestos|\s*niveles|\s*piezas))", nombre, re.I)
        if m_med:
            ficha.append({"atributo": "Medidas / Formato", "valor": m_med.group(1)})
        elif "cama" in nombre.lower() or "colchón" in nombre.lower():
            ficha.append({"atributo": "Medidas", "valor": "140 x 190 cm (Doble / Queen)"})
        elif "sofá" in nombre.lower():
            ficha.append({"atributo": "Medidas", "valor": "210 x 85 x 90 cm"})
        elif "mesa" in nombre.lower() or "comedor" in nombre.lower():
            ficha.append({"atributo": "Medidas", "valor": "120 x 80 x 75 cm"})
        elif "closet" in nombre.lower() or "estantería" in nombre.lower():
            ficha.append({"atributo": "Medidas", "valor": "180 x 120 x 50 cm"})
        else:
            ficha.append({"atributo": "Presentación", "valor": "Dimensiones estándar de hogar"})

        if "gris" in nombre.lower():
            col = "Gris"
        elif "blanco" in nombre.lower():
            col = "Blanco"
        else:
            col = "Tonos neutros / Madera"
        ficha.append({"atributo": "Color", "valor": col})
        desc = f"{nombre}. Mueble o artículo de decoración diseñado para optimizar el confort, funcionalidad y estética de tu hogar."

    return marca, desc, ficha


def sembrar(db_path, tasa_cop=3250.0):
    """Agrega las categorías y los productos que falten. Devuelve cuántos agregó."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    migrar(conn)
    azar = random.Random(2026)                 # mismo stock en cada base nueva
    ahora = datetime.now().isoformat(timespec="seconds")
    antes = conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    try:
        conn.execute("BEGIN")
        for categoria, (codigo, tono, subs) in CATALOGO.items():
            conn.execute("INSERT OR IGNORE INTO categorias (nombre, tono, creada) VALUES (?,?,?)", (categoria, tono, ahora))
            n = 0
            for sub, (icono, productos) in subs.items():
                conn.execute("INSERT OR IGNORE INTO subcategorias (categoria, nombre, icono) VALUES (?,?,?)",
                             (categoria, sub, icono))
                for nombre, cop in productos:
                    n += 1
                    stock = _stock(azar)                     # se saca siempre, así el stock no cambia al repetir
                    marca, desc, ficha = detalles_producto(categoria, sub, nombre)
                    conn.execute(
                        "INSERT INTO productos (product_id, product_name, category, sub_category, precio, stock, marca, descripcion, ficha) "
                        "VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(product_id, product_name) DO UPDATE SET "
                        "marca = excluded.marca, descripcion = excluded.descripcion, ficha = excluded.ficha",
                        (f"ME-{codigo}-{n:03d}", nombre, categoria, sub, round(cop / tasa_cop, 6), stock,
                         marca, desc, json.dumps(ficha, ensure_ascii=False)))
        conn.execute("COMMIT")
        despues = conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
        nuevos = despues - antes
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return nuevos


if __name__ == "__main__":
    aqui = os.path.dirname(os.path.abspath(__file__))
    ruta = os.environ.get("DB_PATH", os.path.join(aqui, "mercado_express.db"))
    tasa = float(os.environ.get("TASA_COP", "3250"))
    agregados = sembrar(ruta, tasa)
    total = sum(len(p) for _, _, subs in CATALOGO.values() for _, p in subs.values())
    print(f"Listo: {agregados} productos nuevos de {total} (categorías: {', '.join(CATALOGO)}).")
    print(f"Base: {ruta}")
