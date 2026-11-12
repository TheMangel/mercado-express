# -*- coding: utf-8 -*-
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pasarela import cobrar, luhn_valido, marca_de, validar_tarjeta  # noqa: E402

HOY = date(2026, 11, 10)
BUENA = {"numero": "4242 4242 4242 4242", "nombre": "Ana Pérez", "vence": "12/30", "cvv": "123"}


def test_luhn():
    assert luhn_valido("4242424242424242") and luhn_valido("5555555555554444")
    assert not luhn_valido("4242424242424243")


def test_marcas():
    assert marca_de("4242424242424242") == "Visa"
    assert marca_de("5555555555554444") == "Mastercard"
    assert marca_de("2221000000000009") == "Mastercard"
    assert marca_de("378282246310005") == "American Express"


def test_validar_tarjeta_buena_limpia_espacios():
    t, errores = validar_tarjeta(BUENA, HOY)
    assert errores == [] and t["numero"] == "4242424242424242" and t["vence"] == "12/30"


def test_vence_este_mes_todavia_vale_y_el_mes_pasado_no():
    assert validar_tarjeta(dict(BUENA, vence="11/26"), HOY)[1] == []
    assert validar_tarjeta(dict(BUENA, vence="10/26"), HOY)[1] != []
    assert validar_tarjeta(dict(BUENA, vence="12/2031"), HOY)[1] == []


def test_validar_tarjeta_no_revienta_con_basura():
    for malo in (None, "texto", [], {"numero": 123, "nombre": 5, "vence": None, "cvv": []}):
        t, errores = validar_tarjeta(malo, HOY)
        assert t is None and errores


def test_cobrar_solo_devuelve_marca_y_ultimos4():
    ok = cobrar(validar_tarjeta(BUENA, HOY)[0], 100000)
    assert ok == {"estado": "aprobado", "motivo": None, "marca": "Visa", "ultimos4": "4242"}
    assert "4242424242424242" not in str(ok)


def test_tarjetas_de_prueba():
    def estado(num):
        return cobrar(validar_tarjeta(dict(BUENA, numero=num), HOY)[0], 1)["estado"]
    assert estado("5555 5555 5555 4444") == "aprobado"
    assert estado("4000 0000 0000 0002") == "rechazado"
    assert estado("4000 0000 0000 9995") == "rechazado"
    assert estado("4111 1111 1111 1111") == "rechazado"       # válida pero no es de prueba
