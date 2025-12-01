import app


def test_validate_and_fix_response_appends_codes_when_missing():
    allowed_products = [
        {
            "code": "1234/56789-000",
            "name": "Filtro de aceite Honda",
            "price_ars": 1000,
        },
        {
            "code": "9999/11111-222",
            "name": "Bujía Iridium",
            "price_ars": 2000,
        },
    ]

    reply = 'Te paso opciones: "Filtro de aceite Honda" y "Bujía Iridium"'
    execution_context = {}

    fixed_reply = app.validate_and_fix_response(reply, allowed_products, "+5491100000000", execution_context)

    assert "Códigos de referencia" in fixed_reply
    assert "1234/56789-000" in fixed_reply
    assert "9999/11111-222" in fixed_reply
