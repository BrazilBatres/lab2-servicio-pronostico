"""Política ante datos defectuosos: catálogo E01-E10 y casos que no están en el catálogo."""
import copy

import pytest

from novamercado.cleaning import (Rechazo, clean_observaciones, clean_prediccion, estado_calidad,
                                  parse_number)
from conftest import valid_obs, valid_pred


def acciones(notas):
    return [n.accion for n in notas]


def test_solicitud_valida_sin_observaciones(cat):
    limpio, notas = clean_prediccion(valid_pred(), cat)
    assert notas == [] and estado_calidad(notas) == "ok"
    assert limpio["tienda_id"] == 1 and len(limpio["familias"]) == 12


@pytest.mark.parametrize("v", ["T01", "1", "T-1", "tienda 1", 1.0, "Prueba Centro"])
def test_E01_tienda_como_texto_se_repara(cat, v):
    raw = valid_pred()
    raw["tienda_id"] = v
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["tienda_id"] == 1
    assert acciones(notas) == ["reparar"] and notas[0].campo == "tienda_id"


@pytest.mark.parametrize("v", [99, "T99", "abc", True, None, 1.5])
def test_tienda_inexistente_o_ininteligible_se_rechaza(cat, v):
    raw = valid_pred()
    raw["tienda_id"] = v
    with pytest.raises(Rechazo):
        clean_prediccion(raw, cat)


@pytest.mark.parametrize("v,esperado", [("LÁCTEOS", "lacteos"), ("lacteos ", "lacteos"),
                                         ("Frutas y Verduras", "frutas_verduras"), ("FRUTAS-VERDURAS", "frutas_verduras"),
                                         ("Snacks y Dulces", "snacks"), ("cuidado personal", "cuidado_personal")])
def test_E02_familia_otra_forma_se_repara(cat, v, esperado):
    raw = valid_pred()
    raw["familias"][2]["familia_id"] = v
    if esperado != "lacteos":
        raw["familias"] = [f for f in raw["familias"] if f["familia_id"] != esperado]
    limpio, notas = clean_prediccion(raw, cat)
    assert esperado in [f["familia_id"] for f in limpio["familias"]]
    rep = [n for n in notas if n.accion == "reparar"]
    assert rep and rep[0].campo == "familias[2].familia_id"


def test_familia_parecida_se_advierte_y_desconocida_se_ignora(cat):
    raw = valid_pred()
    raw["familias"][0]["familia_id"] = "abarotes"  # error de dedo
    raw["familias"].append({"familia_id": "electronica", "en_promocion": 1})
    limpio, notas = clean_prediccion(raw, cat)
    assert "abarrotes" in [f["familia_id"] for f in limpio["familias"]]
    assert "electronica" not in [f["familia_id"] for f in limpio["familias"]]
    assert estado_calidad(notas) == "advertencia"


@pytest.mark.parametrize("v,esperado,accion", [("3", 3, "reparar"), (2.0, 2, "reparar"), (" 4 ", 4, "reparar"),
                                                (2.6, 3, "advertir"), (-1, 0, "advertir"), ("muchos", 0, "advertir")])
def test_E03_promocion(cat, v, esperado, accion):
    raw = valid_pred()
    raw["familias"][1]["en_promocion"] = v
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["familias"][1]["en_promocion"] == esperado
    assert accion in acciones(notas)


@pytest.mark.parametrize("v,accion", [("2024-05-20T00:00:00", "reparar"), ("20/05/2024", "reparar"),
                                       ("2024/05/20", "reparar"), ("05/20/2024", "reparar"),
                                       ("20240520", "reparar"), (" 2024-05-20 ", "reparar")])
def test_E04_fecha_otro_formato_se_repara(cat, v, accion):
    raw = valid_pred()
    raw["fecha"] = v
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["fecha"] == "2024-05-20" and acciones(notas) == [accion]


def test_fecha_ambigua_se_advierte_dia_mes(cat):
    raw = valid_pred(fecha="03/05/2024")
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["fecha"] == "2024-05-03" and acciones(notas) == ["advertir"]


@pytest.mark.parametrize("v", ["2024-02-30", "mañana", 20240520, "2023-01-01"])
def test_fecha_invalida_o_fuera_de_rango_se_rechaza(cat, v):
    with pytest.raises(Rechazo):
        clean_prediccion(valid_pred(fecha=v), cat)


def test_fecha_no_depende_del_reloj(cat):
    # una fecha muy futura es válida (operamos con fechas simuladas); solo se advierte si no hay feriados
    limpio, notas = clean_prediccion(valid_pred(fecha="2031-01-05"), cat)
    assert limpio["fecha"] == "2031-01-05" and estado_calidad(notas) == "advertencia"


def test_tienda_antes_de_su_apertura_se_rechaza(cat):
    with pytest.raises(Rechazo):
        clean_prediccion(valid_pred(tienda=3, fecha="2024-02-01"), cat)


def test_E05_faltan_familias_se_advierte(cat):
    raw = valid_pred()
    raw["familias"] = raw["familias"][:10]
    limpio, notas = clean_prediccion(raw, cat)
    assert len(limpio["familias"]) == 10
    assert acciones(notas) == ["advertir"] and notas[0].campo == "familias"


def test_E06_familia_repetida(cat):
    raw = valid_pred()
    raw["familias"].append({"familia_id": "abarrotes", "en_promocion": 0})
    _, notas = clean_prediccion(copy.deepcopy(raw), cat)
    assert acciones(notas) == ["reparar"]
    raw["familias"][-1]["en_promocion"] = 7
    limpio, notas = clean_prediccion(raw, cat)
    assert acciones(notas) == ["advertir"]
    assert [f for f in limpio["familias"] if f["familia_id"] == "abarrotes"][0]["en_promocion"] == 0


@pytest.mark.parametrize("k", ["tienda", "Tienda_ID", "id_tienda", "tiendaid", "tienda_idd"])
def test_E07_campo_renombrado_se_repara(cat, k):
    raw = valid_pred()
    raw[k] = raw.pop("tienda_id")
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["tienda_id"] == 1 and acciones(notas) == ["reparar"]


def test_campos_renombrados_en_items_y_campo_extra(cat):
    raw = valid_pred()
    raw["familias"][0] = {"familia": "abarrotes", "promocion": 1}
    raw["origen"] = "pos-v2"
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["familias"][0] == {"familia_id": "abarrotes", "en_promocion": 1}
    assert {"reparar", "advertir"} == set(acciones(notas))


@pytest.mark.parametrize("raw", [[], "texto", {"fecha": "2024-05-20", "familias": []},
                                 {"tienda_id": 1, "familias": [{"familia_id": "abarrotes"}]},
                                 {"tienda_id": 1, "fecha": "2024-05-20", "familias": []},
                                 {"tienda_id": 1, "fecha": "2024-05-20", "familias": [{"familia_id": "zzz"}]}])
def test_faltantes_indispensables_se_rechazan(cat, raw):
    with pytest.raises(Rechazo):
        clean_prediccion(raw, cat)


def test_familias_como_objeto_se_repara(cat):
    raw = {"tienda_id": 1, "fecha": "2024-05-20", "familias": {"abarrotes": 1, "bebidas": "2"}}
    limpio, notas = clean_prediccion(raw, cat)
    assert limpio["familias"] == [{"familia_id": "abarrotes", "en_promocion": 1},
                                  {"familia_id": "bebidas", "en_promocion": 2}]


# ------------------------------------------------------------------- observaciones
def test_E08_ventas_negativas_no_se_guardan(cat):
    raw = valid_obs()
    raw["ventas"][0]["ventas"] = -4
    limpio, notas = clean_observaciones(raw, cat)
    assert "abarrotes" not in [v["familia_id"] for v in limpio["ventas"]]
    assert estado_calidad(notas) == "advertencia"


@pytest.mark.parametrize("v,esperado", [("1,234", 1234), ("812,0", 812), ("1.234.567", 1234567),
                                         ("1.234,0", 1234), (" 77 ", 77), (12.0, 12)])
def test_E10_numero_con_formato(cat, v, esperado):
    raw = valid_obs()
    raw["ventas"][0]["ventas"] = v
    limpio, notas = clean_observaciones(raw, cat)
    assert limpio["ventas"][0]["ventas"] == esperado and acciones(notas) == ["reparar"]


def test_numero_no_entero_se_redondea_con_advertencia():
    assert parse_number("812,5").valor == 812.5
    assert parse_number("1,2,3").valor is None


def test_observacion_valida_y_repetida_en_la_lista(cat):
    raw = valid_obs()
    limpio, notas = clean_observaciones(raw, cat)
    assert notas == [] and len(limpio["ventas"]) == 12
    raw["ventas"].append(dict(raw["ventas"][0]))
    _, notas = clean_observaciones(raw, cat)
    assert acciones(notas) == ["reparar"]
