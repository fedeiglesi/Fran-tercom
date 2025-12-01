import os
from pathlib import Path

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("CATALOG_URL", "https://example.com/catalog.csv")
os.environ.setdefault("OPENAI_MODEL", "gpt-test")

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import orquestar_fran


def process_message(texto, estado):
    phone = estado.get("phone", "+5491100000000")
    estado["phone"] = phone
    return orquestar_fran(texto, phone)


@pytest.fixture
def app_client():
    return True


def test_dialogo_01(app_client):
    mensajes = [
        {"role": "user", "content": "hola fran, tenes pastillas para honda wave 110?"},
        {"role": "assistant", "content": "Hola, confirmo stock"},
        {"role": "user", "content": "pasame codigo tercom si hay"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_02(app_client):
    mensajes = [
        {"role": "user", "content": "buen dia, necesito kit de transmicion yamha fz16"},
        {"role": "assistant", "content": "Te paso opciones"},
        {"role": "user", "content": "dame precio en pesos"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_03(app_client):
    mensajes = [
        {"role": "user", "content": "che fran, buscame filtro de aire para keller knk 150 y de paso bujia"},
        {"role": "assistant", "content": "Reviso ambos"},
        {"role": "user", "content": "mandame los dos juntos"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_04(app_client):
    mensajes = [
        {"role": "user", "content": "hola, recien te pedi cadena para wave, ahora necesito piñon para la misma"},
        {"role": "assistant", "content": "Quedó la wave 110"},
        {"role": "user", "content": "si, pasame combo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_05(app_client):
    mensajes = [
        {"role": "user", "content": "necesito codigo tercom 1234/56789-000 tenes?"},
        {"role": "assistant", "content": "Busco por código"},
        {"role": "user", "content": "mandame precio"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_06(app_client):
    mensajes = [
        {"role": "user", "content": "quiero pastillas para cg pero la moto es yamaha fz 2.0"},
        {"role": "assistant", "content": "Aclarar modelo"},
        {"role": "user", "content": "es la fz"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_07(app_client):
    mensajes = [
        {"role": "user", "content": "holaa"},
        {"role": "assistant", "content": "Hola"},
        {"role": "user", "content": "necesito amortiguadores motomel blitz"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_08(app_client):
    mensajes = [
        {"role": "user", "content": "che, me cotizas cubiertas 90/90-18 y bujia ngk"},
        {"role": "assistant", "content": "Veo ambas"},
        {"role": "user", "content": "para yamaha ybr"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_09(app_client):
    mensajes = [
        {"role": "user", "content": "hola, me decis si tenes tapa cilindro suzuki ax100"},
        {"role": "assistant", "content": "Reviso AX100"},
        {"role": "user", "content": "apurate que cierra el reparto"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_10(app_client):
    mensajes = [
        {"role": "user", "content": "precio kit arrastre honda titan 150 2014"},
        {"role": "assistant", "content": "Consulto"},
        {"role": "user", "content": "lo quiero reforzado"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_11(app_client):
    mensajes = [
        {"role": "user", "content": "la otra vez me pasaste filtro de aceite para nc, ahora quiero para suzuki gn125"},
        {"role": "assistant", "content": "Anoto GN125"},
        {"role": "user", "content": "modelo 2017"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_12(app_client):
    mensajes = [
        {"role": "user", "content": "dame precio de algo para moto voladora"},
        {"role": "assistant", "content": "No existe"},
        {"role": "user", "content": "ok entoces para yamaha xj6 tenes?"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_13(app_client):
    mensajes = [
        {"role": "user", "content": "hola crack"},
        {"role": "assistant", "content": "Hola"},
        {"role": "user", "content": "ahora si, pasame kit de transmisión zanella ztt 250"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_14(app_client):
    mensajes = [
        {"role": "user", "content": "necesito cubre carter para honda pero la moto es suzuki gixxer"},
        {"role": "assistant", "content": "Confirmo modelo"},
        {"role": "user", "content": "es la gixxer 150"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_15(app_client):
    mensajes = [
        {"role": "user", "content": "me repetis el precio que me diste recien de la batería para motomel blitz"},
        {"role": "assistant", "content": "Te recuerdo"},
        {"role": "user", "content": "era la de gel"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_16(app_client):
    mensajes = [
        {"role": "user", "content": "cg ok pastillas freno?"},
        {"role": "assistant", "content": "Necesito aclarar"},
        {"role": "user", "content": "es honda cg 150 2018"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_17(app_client):
    mensajes = [
        {"role": "user", "content": "hola, tenes un regalito?"},
        {"role": "assistant", "content": "Respuesta social"},
        {"role": "user", "content": "gracias genio, ahora filtro de aire yamaha fz"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_18(app_client):
    mensajes = [
        {"role": "user", "content": "hola, pedido: 2 kit arrastre y 1 carburador suzuki ax 100"},
        {"role": "assistant", "content": "Confirmo cantidades"},
        {"role": "user", "content": "todo para la ax"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_19(app_client):
    mensajes = [
        {"role": "user", "content": "ok, perdoname me colgue. necesito espejo izquierdo honda wave"},
        {"role": "assistant", "content": "Busco espejo"},
        {"role": "user", "content": "mandalo con el pedido anterior"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_20(app_client):
    mensajes = [
        {"role": "user", "content": "pasame lo mismo que ayer"},
        {"role": "assistant", "content": "Necesito recordar"},
        {"role": "user", "content": "era bujia ngk para ybr125"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_21(app_client):
    mensajes = [
        {"role": "user", "content": "tenes kit distribucion para motomel sxr"},
        {"role": "assistant", "content": "Consulto"},
        {"role": "user", "content": "si hay, precio y codigo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_22(app_client):
    mensajes = [
        {"role": "user", "content": "buenas, necesito 3 cosas: pastillas fz, filtro aceite wave y foco led"},
        {"role": "assistant", "content": "Confirmo items"},
        {"role": "user", "content": "dale, todo junto"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_23(app_client):
    mensajes = [
        {"role": "user", "content": "ok hola, soy juli. tenes algo para suzuki ax100?"},
        {"role": "assistant", "content": "Hola Juli"},
        {"role": "user", "content": "necesito disco de freno"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_24(app_client):
    mensajes = [
        {"role": "user", "content": "corto: pastillas cg"},
        {"role": "assistant", "content": "Aclarar"},
        {"role": "user", "content": "cg 150 ok"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_25(app_client):
    mensajes = [
        {"role": "user", "content": "necesito cubiertas 120/80-18 para yamaha xtz?"},
        {"role": "assistant", "content": "Reviso medida"},
        {"role": "user", "content": "es 2016"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_26(app_client):
    mensajes = [
        {"role": "user", "content": "te consulto por un repuesto raro: turbina para honda jet"},
        {"role": "assistant", "content": "No disponible"},
        {"role": "user", "content": "entonces bomba de nafta wave"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_27(app_client):
    mensajes = [
        {"role": "user", "content": "que onda fran! tenes pastillas para zanella zb y ademas para yamaha fz?"},
        {"role": "assistant", "content": "Dos motos"},
        {"role": "user", "content": "pasame las dos"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_28(app_client):
    mensajes = [
        {"role": "user", "content": "buen dia, me conseguis rodamiento delantero para keller kronos?"},
        {"role": "assistant", "content": "Consulto rodamiento"},
        {"role": "user", "content": "ok dale"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_29(app_client):
    mensajes = [
        {"role": "user", "content": "me tiras precio de valvulas para honda twister 250"},
        {"role": "assistant", "content": "Busco"},
        {"role": "user", "content": "si hay kit completo mejor"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_30(app_client):
    mensajes = [
        {"role": "user", "content": "hola! necesito kit de transmision para suzuki ax100 y pastillas para motomel blitz"},
        {"role": "assistant", "content": "Dos pedidos"},
        {"role": "user", "content": "si genio, pasame todo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_31(app_client):
    mensajes = [
        {"role": "user", "content": "me pasas otra vez el precio de la optica para ybr?"},
        {"role": "assistant", "content": "Busco óptica"},
        {"role": "user", "content": "es la yamaha ybr 125 ed"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_32(app_client):
    mensajes = [
        {"role": "user", "content": "che necesito algo urgente, precio de kit cadena yamaha xtz 250"},
        {"role": "assistant", "content": "Consulto"},
        {"role": "user", "content": "dale rapidoo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_33(app_client):
    mensajes = [
        {"role": "user", "content": "dame precio de manubrio y espejo para honda cb190r"},
        {"role": "assistant", "content": "Dos accesorios"},
        {"role": "user", "content": "ok, mandalo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_34(app_client):
    mensajes = [
        {"role": "user", "content": "busco carburador para motomel skua 150, puede ser adaptable?"},
        {"role": "assistant", "content": "Reviso"},
        {"role": "user", "content": "si no hay original, algo que sirva"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_35(app_client):
    mensajes = [
        {"role": "user", "content": "hey fran, estoy indeciso, necesito o un kit freno o pastillas, que recomendas para keller mirage"},
        {"role": "assistant", "content": "Te sugiero"},
        {"role": "user", "content": "dame la opcion mas comun"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_36(app_client):
    mensajes = [
        {"role": "user", "content": "me pasas los cables de bujia para dos motos, una honda wave y una yamaha crypton"},
        {"role": "assistant", "content": "Confirmo ambos modelos"},
        {"role": "user", "content": "si, mandame"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_37(app_client):
    mensajes = [
        {"role": "user", "content": "hola de nuevo, el cliente quiere pastillas suzuki ax, pero recien me pidio para zanella zb"},
        {"role": "assistant", "content": "Aclaro prioridad"},
        {"role": "user", "content": "mandame las dos"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_38(app_client):
    mensajes = [
        {"role": "user", "content": "che, conseguime termostato"},
        {"role": "assistant", "content": "Necesito modelo"},
        {"role": "user", "content": "perdon, es para honda nc700"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_39(app_client):
    mensajes = [
        {"role": "user", "content": "ok, dale. tengo lista larga: 1) corona ybr, 2) piñon fz, 3) cadena cg"},
        {"role": "assistant", "content": "Procesando lista"},
        {"role": "user", "content": "sumale aceite yamaha"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_40(app_client):
    mensajes = [
        {"role": "user", "content": "hola, necesitaba volante magnetico para yamaha factor"},
        {"role": "assistant", "content": "Reviso factor"},
        {"role": "user", "content": "gracias maestro"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_41(app_client):
    mensajes = [
        {"role": "user", "content": "hay tapa embrague para keller miracle 200?"},
        {"role": "assistant", "content": "Busco"},
        {"role": "user", "content": "si hay enviame codigo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_42(app_client):
    mensajes = [
        {"role": "user", "content": "che me colgue, pedime dos cosas: carburador suzuki ax y ruleman para honda cbx"},
        {"role": "assistant", "content": "Confirmo doble pedido"},
        {"role": "user", "content": "ok me sirve"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_43(app_client):
    mensajes = [
        {"role": "user", "content": "te pedi filtro aceite para fz pero la moto del cliente es honda xr"},
        {"role": "assistant", "content": "Corrijo modelo"},
        {"role": "user", "content": "ok entonces xr 150"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_44(app_client):
    mensajes = [
        {"role": "user", "content": "hola, che, tenes algun cubre puño lindo?"},
        {"role": "assistant", "content": "Respondo social"},
        {"role": "user", "content": "dale, si sirve para yamaha fz mejor"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_45(app_client):
    mensajes = [
        {"role": "user", "content": "consulta: escape deportivo para yamaha fz 16 y tambien para honda tornado"},
        {"role": "assistant", "content": "Dos escapes"},
        {"role": "user", "content": "si tenes algo en promo"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_46(app_client):
    mensajes = [
        {"role": "user", "content": "suzuki ax100 kit arrastre, ya"},
        {"role": "assistant", "content": "Voy"},
        {"role": "user", "content": "apurate que cierra el flete"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_47(app_client):
    mensajes = [
        {"role": "user", "content": "me quedo la duda, la cadena que me pasaste sirve para motomel blitz 110 o era para honda?"},
        {"role": "assistant", "content": "Aclaro"},
        {"role": "user", "content": "confirmame para blitz"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_48(app_client):
    mensajes = [
        {"role": "user", "content": "hola fran, dame numero tercom de pastilla yamaha fz"},
        {"role": "assistant", "content": "Busco código"},
        {"role": "user", "content": "ok"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_49(app_client):
    mensajes = [
        {"role": "user", "content": "buenas! tengo un cliente indeciso, pide o valvulas o kit distribucion para honda wave"},
        {"role": "assistant", "content": "Recomiendo"},
        {"role": "user", "content": "mandame lo mas pedido"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()


def test_dialogo_50(app_client):
    mensajes = [
        {"role": "user", "content": "ok, ultimo: filtro aire, aceite yamalube y bujia para yamaha ybr"},
        {"role": "assistant", "content": "Tres productos"},
        {"role": "user", "content": "dale, cerramos"},
    ]

    estado = {}
    for m in mensajes:
        if m["role"] == "user":
            respuesta = process_message(m["content"], estado)
            assert respuesta is not None
            assert isinstance(respuesta, str)
            assert len(respuesta) > 1
            assert "error" not in respuesta.lower()
            assert "traceback" not in respuesta.lower()
