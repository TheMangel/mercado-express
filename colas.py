# -*- coding: utf-8 -*-
"""Modelo de colas M/M/c (fórmula de Erlang C) para estimar esperas y recomendar capacidad.

Se lee así:
  - llegadas (lambda): clientes que llegan por hora
  - servicio (mu): clientes que UN asesor atiende por hora (= 3600 / duración promedio en segundos)
  - c: cantidad de asesores atendiendo a la vez

Todo se calcula en horas y se devuelve en minutos. Son funciones puras (sin base de datos) para poder probarlas.
"""
import math

MAX_ASESORES = 50   # tope para la búsqueda de la recomendación


def erlang_c(llegadas, servicio, c):
    """Probabilidad de que un cliente que llega tenga que esperar. None si la fila crece sin límite."""
    if llegadas <= 0:
        return 0.0
    carga = llegadas / servicio              # "erlangs": asesores ocupados en promedio
    rho = carga / c                          # utilización de cada asesor
    if rho >= 1:
        return None
    # Se acumula el término A^k/k! de forma incremental para no desbordar con números grandes.
    termino, suma = 1.0, 1.0
    for k in range(1, c):
        termino *= carga / k
        suma += termino
    termino *= carga / c                     # A^c / c!
    espera = termino / (1 - rho)
    return espera / (suma + espera)


def espera_promedio_min(llegadas, servicio, c):
    """Minutos que espera en promedio un cliente (en fila, sin contar su atención). None si es inestable."""
    p = erlang_c(llegadas, servicio, c)
    if p is None:
        return None
    if llegadas <= 0:
        return 0.0
    return p / (c * servicio - llegadas) * 60


def recomendar_asesores(llegadas, servicio, meta_espera_min):
    """Menor cantidad de asesores con la que la espera promedio queda dentro de la meta."""
    for c in range(1, MAX_ASESORES + 1):
        espera = espera_promedio_min(llegadas, servicio, c)
        if espera is not None and espera <= meta_espera_min:
            return c
    return MAX_ASESORES


def analizar(llegadas_por_hora, duracion_seg, asesores, meta_espera_min, en_espera):
    """Resumen completo de una fila: lo que muestra la pantalla de turnos."""
    servicio = 3600.0 / duracion_seg                       # clientes por hora por asesor
    rho = llegadas_por_hora / (asesores * servicio)
    espera_modelo = espera_promedio_min(llegadas_por_hora, servicio, asesores)
    recomendados = recomendar_asesores(llegadas_por_hora, servicio, meta_espera_min)
    # Espera de quien llega AHORA: lo que hay en fila repartido entre los asesores.
    espera_actual = en_espera * (duracion_seg / 60) / asesores
    return {
        "utilizacion": round(rho, 3),
        "estable": rho < 1,
        "espera_actual_min": round(espera_actual, 1),
        "espera_modelo_min": None if espera_modelo is None else round(espera_modelo, 1),
        "prob_esperar": None if rho >= 1 else round(erlang_c(llegadas_por_hora, servicio, asesores), 3),
        "asesores_recomendados": recomendados,
    }


def _asesores(n):
    return f"{n} asesor" if n == 1 else f"{n} asesores"


def mensaje(a, asesores, meta_espera_min):
    """Texto de la recomendación (o None si la capacidad actual está bien)."""
    if not a["estable"]:
        return (f"⚠️ Los clientes llegan más rápido de lo que se atiende: la fila crece sin parar. "
                f"Se necesitan al menos {_asesores(a['asesores_recomendados'])} (hoy hay {asesores}).")
    if a["espera_modelo_min"] > meta_espera_min:
        return (f"⚠️ Espera promedio esperada de {a['espera_modelo_min']} min, por encima de la meta de "
                f"{meta_espera_min} min. Se recomiendan {_asesores(a['asesores_recomendados'])} (hoy hay {asesores}).")
    if asesores > a["asesores_recomendados"]:
        return (f"ℹ️ Hay capacidad de sobra: con {_asesores(a['asesores_recomendados'])} se cumple la meta "
                f"de {meta_espera_min} min.")
    return None
