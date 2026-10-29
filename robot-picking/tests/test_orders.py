import pytest

from picking.language.orders import RuleOrderParser


@pytest.fixture(scope="module")
def p():
    return RuleOrderParser()


@pytest.mark.parametrize(
    "text,obj,dest",
    [
        ("Agarrá la palanca de freno y ponela en la bandeja 3", "brake_lever", "bin_3"),
        ("tomá el rulemán y dejalo en la bandeja", "bearing", "tray"),
        ("Levantá la bujía", "spark_plug", "tray"),
        ("buscá el cubo rojo y llevalo a la bandeja A", "red_cube", "tray"),
        ("pick SKU 18452 and place it in bin 2", "brake_pad", "bin_2"),
        ("agarra la manija y metela en el cajon 1", "brake_lever", "bin_1"),
        ("el piñon al pedido 2", "gear", "bin_2"),
        ("sacá la tapita roja", "red_cap", "tray"),
        ("Pick the Honda CG brake lever and place it in order bin 3", "brake_lever", "bin_3"),
    ],
)
def test_argentine_orders(p, text, obj, dest):
    o = p.parse(text)
    assert o.executable, o.notes
    assert o.object_id == obj and o.destination_id == dest
    assert o.instruction_en.startswith("Pick the ") and o.instruction_en.endswith(".")


def test_unknown_object_is_not_executable(p):
    o = p.parse("agarrá el carburador y ponelo en la bandeja")
    assert not o.executable and o.object_id is None and "catálogo" in o.notes


def test_longest_alias_wins(p):
    assert p.parse("palanca de freno").object_id == "brake_lever"
    assert p.parse("pastilla de freno a la bandeja 1").object_id == "brake_pad"
