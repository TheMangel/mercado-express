# -*- coding: utf-8 -*-
"""Cotización de envío: costo, tiempo de alistamiento, despacho y llegada estimada.

Todo se calcula en días HÁBILES (lunes a viernes, sin festivos de Colombia). Son fechas estimadas.
Los precios están en pesos colombianos (COP); la tienda los convierte a la moneda que el cliente eligió.
Funciones puras (reciben la fecha y la hora), para poder probarlas sin depender del reloj.
"""
import unicodedata
from datetime import date, datetime, timedelta, timezone

ORIGEN = {"ciudad": "Montería", "region": "Córdoba", "pais": "Colombia"}     # de dónde sale el pedido
ENVIO_GRATIS_DESDE = 200_000     # COP: el envío estándar dentro de Colombia no cuesta desde este subtotal
HORA_DE_CORTE = 14               # el envío "mismo día" solo se ofrece si el pedido es antes de las 2:00 p. m.
ALISTAMIENTO_DIAS = 1            # días hábiles para preparar y empacar el pedido

CARIBE = {"Atlántico", "Bolívar", "Cesar", "La Guajira", "Magdalena", "Sucre"}
ZONAS_ESPECIALES = {"Amazonas", "Guainía", "Guaviare", "Vaupés", "Vichada", "Putumayo", "Caquetá", "Chocó",
                    "San Andrés y Providencia"}

# zona -> [(id, costo COP, tránsito mínimo, tránsito máximo)]   (tránsito = días hábiles desde el despacho)
TARIFAS = {
    "local":         [("mismo_dia", 16_900, 0, 0), ("express", 12_900, 1, 1), ("estandar", 8_900, 1, 2)],
    "regional":      [("express", 19_900, 1, 2), ("estandar", 12_900, 1, 3)],
    "costa":         [("express", 22_900, 1, 2), ("estandar", 14_900, 2, 4)],
    "nacional":      [("express", 27_900, 2, 3), ("estandar", 17_900, 3, 6)],
    "especial":      [("express", 59_900, 4, 6), ("estandar", 39_900, 6, 10)],
    "internacional": [("internacional", 120_000, 8, 15)],
}


def _sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn").strip().lower()


def zona_de(pais, region, ciudad):
    if pais != "Colombia":
        return "internacional"
    if region == ORIGEN["region"]:
        return "local" if _sin_tildes(ciudad) == _sin_tildes(ORIGEN["ciudad"]) else "regional"
    if region in CARIBE:
        return "costa"
    if region in ZONAS_ESPECIALES:
        return "especial"
    return "nacional"


HORA_COLOMBIA = timezone(timedelta(hours=-5))      # Colombia no tiene horario de verano


def _pascua(año):
    """Domingo de Pascua (algoritmo de Gauss/Meeus para el calendario gregoriano)."""
    a, b, c = año % 19, año // 100, año % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(año, mes, dia + 1)


def _al_lunes(d):
    """Ley Emiliani: ciertos festivos se pasan al lunes siguiente (si ya es lunes, se queda)."""
    return d + timedelta(days=(7 - d.weekday()) % 7)


_FESTIVOS = {}


def festivos(año):
    """Festivos de Colombia de un año (fijos, trasladados al lunes y los de Semana Santa)."""
    if año not in _FESTIVOS:
        pascua = _pascua(año)
        lista = [date(año, 1, 1), date(año, 5, 1), date(año, 7, 20), date(año, 8, 7), date(año, 12, 8), date(año, 12, 25),
                 pascua - timedelta(days=3), pascua - timedelta(days=2)]                    # jueves y viernes santo
        lista += [_al_lunes(d) for d in (date(año, 1, 6), date(año, 3, 19), date(año, 6, 29), date(año, 8, 15),
                                         date(año, 10, 12), date(año, 11, 1), date(año, 11, 11))]
        lista += [_al_lunes(pascua + timedelta(days=n)) for n in (39, 60, 68)]              # Ascensión, Corpus Christi, Sagrado Corazón
        _FESTIVOS[año] = frozenset(lista)
    return _FESTIVOS[año]


def es_habil(d):
    return d.weekday() < 5 and d not in festivos(d.year)


def _habil(d):
    """Si cae en fin de semana o festivo, pasa al siguiente día hábil."""
    while not es_habil(d):
        d += timedelta(days=1)
    return d


def sumar_habiles(d, n):
    d = _habil(d)
    while n > 0:
        d += timedelta(days=1)
        if es_habil(d):
            n -= 1
    return d


def cotizar(pais, region, ciudad, subtotal_cop, ahora=None):
    ahora = ahora or datetime.now(HORA_COLOMBIA).replace(tzinfo=None)     # siempre hora de Colombia
    hoy = ahora.date()
    zona = zona_de(pais, region, ciudad)
    opciones = []
    for id_, costo, t_min, t_max in TARIFAS[zona]:
        if id_ == "mismo_dia":
            # Solo en la ciudad de la bodega, en día hábil y antes de la hora de corte.
            if not es_habil(hoy) or ahora.hour >= HORA_DE_CORTE:
                continue
            despacho, prep = hoy, 0
        else:
            despacho, prep = sumar_habiles(hoy, ALISTAMIENTO_DIAS), ALISTAMIENTO_DIAS
        gratis = id_ == "estandar" and subtotal_cop >= ENVIO_GRATIS_DESDE
        opciones.append({
            "id": id_,
            "costo_cop": 0 if gratis else costo,
            "gratis": gratis,
            "alistamiento_dias": prep,
            "transito_min": t_min, "transito_max": t_max,
            "despacho": despacho.isoformat(),
            "entrega_desde": sumar_habiles(despacho, t_min).isoformat(),
            "entrega_hasta": sumar_habiles(despacho, t_max).isoformat(),
        })
    opciones.sort(key=lambda o: (o["entrega_hasta"], o["costo_cop"]))     # la más rápida primero
    return {
        "origen": f"{ORIGEN['ciudad']}, {ORIGEN['region']}",
        "zona": zona,
        "hora_de_corte": HORA_DE_CORTE,
        "envio_gratis_desde_cop": ENVIO_GRATIS_DESDE if zona != "internacional" else None,
        "opciones": opciones,
    }
