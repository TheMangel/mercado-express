# -*- coding: utf-8 -*-
"""Pasarela de pagos SIMULADA (modo de pruebas).

No mueve dinero. Decide si un pago se aprueba o se rechaza según la tarjeta de prueba usada,
igual que hacen los sandbox de Wompi, Mercado Pago o Stripe.

Regla de seguridad: el número completo y el CVV se usan solo dentro de `cobrar()` y NUNCA se
guardan ni se devuelven. De la tarjeta solo salen la marca y los últimos 4 dígitos.

Para pasar a una pasarela real, solo hay que reemplazar `cobrar()`: el resto de la tienda
(la tabla `pagos` y las rutas /cuenta/pago y /cuenta/pedido) no cambia.
"""
import re

# número de prueba -> (estado, motivo del rechazo)
TARJETAS_PRUEBA = {
    "4242424242424242": ("aprobado", None),                                # Visa aprobada
    "5555555555554444": ("aprobado", None),                                # Mastercard aprobada
    "4000000000000002": ("rechazado", "El banco rechazó la tarjeta."),
    "4000000000009995": ("rechazado", "Fondos insuficientes."),
}
MOTIVO_NO_PRUEBA = "Tarjeta no reconocida: en el modo de pruebas usa una de las tarjetas de prueba."


def luhn_valido(numero):
    """Suma de verificación que cumplen todos los números de tarjeta reales y de prueba."""
    total = 0
    for i, d in enumerate(reversed(numero)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def marca_de(numero):
    if numero.startswith("4"):
        return "Visa"
    if re.match(r"^(5[1-5]|2(2[2-9]|[3-6]\d|7[01]|720))", numero):
        return "Mastercard"
    if re.match(r"^3[47]", numero):
        return "American Express"
    return "Tarjeta"


def validar_tarjeta(tarjeta, hoy):
    """Revisa el formato de lo que escribió la persona. Devuelve (tarjeta_limpia, errores)."""
    errores = []
    if not isinstance(tarjeta, dict):
        return None, ["tarjeta: faltan los datos de la tarjeta"]
    numero = re.sub(r"[ -]", "", tarjeta.get("numero") if isinstance(tarjeta.get("numero"), str) else "")
    if not re.fullmatch(r"\d{13,19}", numero) or not luhn_valido(numero):
        errores.append("tarjeta.numero: el número de la tarjeta no es válido")
    nombre = tarjeta.get("nombre").strip() if isinstance(tarjeta.get("nombre"), str) else ""
    if not 2 <= len(nombre) <= 60:
        errores.append("tarjeta.nombre: escribe el nombre como aparece en la tarjeta")
    m = re.fullmatch(r"(\d{2})/(\d{2}|\d{4})", tarjeta.get("vence").strip() if isinstance(tarjeta.get("vence"), str) else "")
    if not m or not 1 <= int(m.group(1)) <= 12:
        errores.append("tarjeta.vence: usa el formato MM/AA")
    else:
        anio = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
        if (anio, int(m.group(1))) < (hoy.year, hoy.month):
            errores.append("tarjeta.vence: la tarjeta está vencida")
    cvv = tarjeta.get("cvv").strip() if isinstance(tarjeta.get("cvv"), str) else ""
    if not re.fullmatch(r"\d{3,4}", cvv):
        errores.append("tarjeta.cvv: el código de seguridad tiene 3 o 4 dígitos")
    if errores:
        return None, errores
    return {"numero": numero, "nombre": nombre, "vence": m.group(0), "cvv": cvv}, []


def cobrar(tarjeta, monto_cop):
    """Simula el cobro. Devuelve solo lo que es seguro guardar: estado, motivo, marca y últimos 4."""
    numero = tarjeta["numero"]
    estado, motivo = TARJETAS_PRUEBA.get(numero, ("rechazado", MOTIVO_NO_PRUEBA))
    return {"estado": estado, "motivo": motivo, "marca": marca_de(numero), "ultimos4": numero[-4:]}
