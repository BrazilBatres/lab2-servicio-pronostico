"""Defensa del contrato: normaliza solicitudes crudas aplicando una política explícita.

Política general (ver DECISIONES.md, sección 1):
- REPARAR: la corrección es inequívoca (mismo significado, otra forma de escribirlo).
- ADVERTIR: se responde, pero hubo que suponer algo o descartar un dato (sin inventarlo).
- RECHAZAR (422): falta información indispensable (tienda, fecha, lista) o no es interpretable.

Las funciones son puras (sin FastAPI, sin base de datos) para poder probarlas con pytest.
"""
from __future__ import annotations

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any

from .catalogs import Catalogs

REPARAR = "reparar"
ADVERTIR = "advertir"


class Rechazo(Exception):
    """Solicitud que no puede atenderse sin inventar datos (se responde 422)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Nota:
    campo: str
    accion: str  # reparar | advertir
    detalle: str

    def dict(self) -> dict:
        return asdict(self)


def estado_calidad(notas: list[Nota]) -> str:
    if any(n.accion == ADVERTIR for n in notas):
        return "advertencia"
    if notas:
        return "corregido"
    return "ok"


# ----------------------------------------------------------------------------- texto y claves
def norm_text(s: str) -> str:
    """'  Frutas y Verduras ' -> 'frutas y verduras'; 'LÁCTEOS' -> 'lacteos'."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def norm_key(s: str) -> str:
    return norm_text(s).replace(" ", "_")


ALIAS_PREDICCION = {
    "tienda_id": ["tienda", "id_tienda", "tiendaid", "store", "store_id", "codigo_tienda", "cod_tienda", "sucursal"],
    "fecha": ["date", "dia", "fecha_prediccion", "fecha_pronostico", "fecha_venta", "day"],
    "familias": ["familia", "lista_familias", "productos", "items", "lineas", "detalle"],
}
ALIAS_OBSERVACION = {
    "tienda_id": ALIAS_PREDICCION["tienda_id"],
    "fecha": ALIAS_PREDICCION["fecha"] + ["fecha_observacion"],
    "ventas": ["venta", "observaciones", "ventas_reales", "items", "lineas", "detalle", "registros"],
}
ALIAS_ITEM_PRED = {
    "familia_id": ["familia", "id_familia", "familiaid", "family", "family_id", "categoria", "nombre_familia"],
    "en_promocion": ["promocion", "promociones", "promo", "en_promo", "onpromotion", "productos_en_promocion",
                     "en_promociones", "num_promocion"],
}
ALIAS_ITEM_OBS = {
    "familia_id": ALIAS_ITEM_PRED["familia_id"],
    "ventas": ["venta", "unidades", "cantidad", "sales", "ventas_reales", "unidades_vendidas", "valor"],
}


def _map_key(key: str, aliases: dict[str, list[str]]) -> str | None:
    if key in aliases:
        return key
    nk = norm_key(key)
    for canon, alts in aliases.items():
        if nk == canon or nk in alts or nk.replace("_", "") == canon.replace("_", ""):
            return canon
    universe = {a: c for c, alts in aliases.items() for a in [c, *alts]}
    m = difflib.get_close_matches(nk, list(universe), n=1, cutoff=0.85)
    return universe[m[0]] if m else None


def normalize_keys(obj: dict, aliases: dict[str, list[str]], prefix: str, notas: list[Nota]) -> dict:
    """Renombra campos escritos de otra forma; ignora (y avisa) campos desconocidos."""
    out: dict[str, Any] = {}
    exact = {k for k in obj if k in aliases}
    for key, val in obj.items():
        canon = _map_key(str(key), aliases)
        path = f"{prefix}{key}"
        if canon is None:
            notas.append(Nota(path, ADVERTIR, f"campo desconocido '{key}' ignorado"))
            continue
        if canon == key:
            out[canon] = val
            continue
        if canon in exact or canon in out:
            same = out.get(canon, obj.get(canon)) == val
            notas.append(Nota(path, REPARAR if same else ADVERTIR,
                              f"campo '{key}' duplica a '{canon}'; se usó el valor de '{canon}'"))
            continue
        out[canon] = val
        notas.append(Nota(path, REPARAR, f"campo '{key}' se interpretó como '{canon}'"))
    return out


# ----------------------------------------------------------------------------- números
@dataclass
class Numero:
    valor: float | None
    accion: str | None = None  # None = sin cambios
    detalle: str = ""


def parse_number(v: Any) -> Numero:
    """Interpreta números escritos como texto. Convención guatemalteca: ',' miles y '.' decimal,
    salvo una coma que no separe grupos de 3 dígitos ('812,0'), que se toma como decimal."""
    if isinstance(v, bool) or v is None:
        return Numero(None, detalle=f"valor {v!r} no es numérico")
    if isinstance(v, int):
        return Numero(float(v))
    if isinstance(v, float):
        if not math.isfinite(v):
            return Numero(None, detalle=f"valor {v!r} no es finito")
        return Numero(v)
    if not isinstance(v, str):
        return Numero(None, detalle=f"valor {v!r} no es numérico")
    s = v.strip().replace(" ", "").replace(" ", "").replace("_", "")
    if not s:
        return Numero(None, detalle="valor vacío")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        mil = "." if dec == "," else ","
        s2 = s.replace(mil, "").replace(dec, ".")
    elif "," in s:
        if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", s):
            s2 = s.replace(",", "")
        elif s.count(",") == 1:
            s2 = s.replace(",", ".")
        else:
            return Numero(None, detalle=f"'{v}' no es un número interpretable")
    elif re.fullmatch(r"[-+]?\d{1,3}(\.\d{3}){2,}", s):
        s2 = s.replace(".", "")
    else:
        s2 = s
    try:
        x = float(s2)
    except ValueError:
        return Numero(None, detalle=f"'{v}' no es un número interpretable")
    if not math.isfinite(x):
        return Numero(None, detalle=f"'{v}' no es finito")
    return Numero(x, REPARAR, f"'{v}' se interpretó como {x:g}")


def to_count(v: Any, campo: str, notas: list[Nota]) -> int | None:
    """Entero no negativo. Devuelve None (y una advertencia) si no se puede interpretar."""
    n = parse_number(v)
    if n.valor is None:
        notas.append(Nota(campo, ADVERTIR, n.detalle))
        return None
    x = n.valor
    if x < 0:
        notas.append(Nota(campo, ADVERTIR, f"valor negativo {v!r} no es válido"))
        return None
    if not float(x).is_integer():
        r = int(round(x))
        notas.append(Nota(campo, ADVERTIR, f"{v!r} no es entero; se redondeó a {r}"))
        return r
    if n.accion or isinstance(v, float):
        notas.append(Nota(campo, REPARAR, f"{v!r} se interpretó como {int(x)}"))
    return int(x)


# ----------------------------------------------------------------------------- tienda
def parse_tienda(v: Any, cat: Catalogs, notas: list[Nota]) -> int:
    campo = "tienda_id"
    ids = set(cat.tienda_ids)
    tid: int | None = None
    if isinstance(v, bool) or v is None:
        raise Rechazo(f"tienda_id {v!r} no es válido")
    if isinstance(v, int):
        tid = v
    elif isinstance(v, float) and v.is_integer():
        tid = int(v)
        notas.append(Nota(campo, REPARAR, f"{v!r} se interpretó como {tid}"))
    elif isinstance(v, str):
        s = v.strip()
        m = re.fullmatch(r"[A-Za-z_\-# ]*?0*(\d+)(\.0+)?", s)
        if m:
            tid = int(m.group(1))
        else:  # ¿nombre de la tienda?
            nv = norm_text(s)
            nombres = {norm_text(n): int(t) for t, n in zip(cat.tiendas.tienda_id, cat.tiendas.nombre)}
            nombres.update({norm_text(str(n).replace("NovaMercado", "")): int(t)
                            for t, n in zip(cat.tiendas.tienda_id, cat.tiendas.nombre)})
            if nv in nombres:
                tid = nombres[nv]
        if tid is not None:
            notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como {tid}"))
    if tid is None:
        raise Rechazo(f"tienda_id {v!r} no es interpretable como identificador de tienda")
    if tid not in ids:
        raise Rechazo(f"la tienda {tid} no existe en el catálogo (tiendas válidas: {min(ids)}-{max(ids)})")
    return tid


# ----------------------------------------------------------------------------- fecha
def _mk(y: int, m: int, d: int, original: Any) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise Rechazo(f"fecha {original!r} no existe en el calendario")


def parse_fecha(v: Any, notas: list[Nota], campo: str = "fecha") -> date:
    """Fechas en AAAA-MM-DD. Otros formatos se reparan si son inequívocos. No usa el reloj del sistema."""
    if not isinstance(v, str):
        raise Rechazo(f"fecha {v!r} debe ser texto con formato AAAA-MM-DD")
    s = v.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        d = _mk(int(m[1]), int(m[2]), int(m[3]), v)
        if s != v:
            notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{d.isoformat()}'"))
        return d
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?", s):
        d = _mk(int(m[1]), int(m[2]), int(m[3]), v)
        notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{d.isoformat()}'"))
        return d
    if m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s):
        d = _mk(int(m[1]), int(m[2]), int(m[3]), v)
        notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{d.isoformat()}'"))
        return d
    if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 or a == b:
            d = _mk(y, b, a, v)  # día/mes inequívoco
            notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{d.isoformat()}' (día/mes)"))
        elif b > 12:
            d = _mk(y, a, b, v)  # mes/día inequívoco
            notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{d.isoformat()}' (mes/día)"))
        else:
            d = _mk(y, b, a, v)
            notas.append(Nota(campo, ADVERTIR,
                              f"'{v}' es ambiguo; se asumió día/mes (convención local): '{d.isoformat()}'"))
        return d
    raise Rechazo(f"fecha {v!r} no es interpretable; use AAAA-MM-DD")


# ----------------------------------------------------------------------------- familia
def _familia_index(cat: Catalogs) -> dict[str, str]:
    if "familias" not in cat._cache:
        idx: dict[str, str] = {}
        stop = {"y", "de", "e", "la", "el", "los", "las"}
        for fid, nombre in zip(cat.familias.familia_id, cat.familias.nombre):
            for txt in (fid, nombre):
                n = norm_text(txt)
                idx[n] = fid
                idx[n.replace(" ", "")] = fid
                idx[" ".join(w for w in n.split() if w not in stop)] = fid
                idx[norm_text(str(txt).replace("_", " "))] = fid
        cat._cache["familias"] = idx
    return cat._cache["familias"]


def parse_familia(v: Any, cat: Catalogs, campo: str, notas: list[Nota]) -> str | None:
    if not isinstance(v, str) or not v.strip():
        notas.append(Nota(campo, ADVERTIR, f"familia {v!r} no es válida; se ignoró"))
        return None
    if v in cat.familia_ids:
        return v
    idx = _familia_index(cat)
    stop = {"y", "de", "e", "la", "el", "los", "las"}
    n = norm_text(v)
    for cand in (n, n.replace(" ", ""), " ".join(w for w in n.split() if w not in stop)):
        if cand in idx:
            notas.append(Nota(campo, REPARAR, f"'{v}' se interpretó como '{idx[cand]}'"))
            return idx[cand]
    m = difflib.get_close_matches(n, list(idx), n=1, cutoff=0.8)
    if m:
        fid = idx[m[0]]
        notas.append(Nota(campo, ADVERTIR, f"'{v}' no coincide exactamente; se asumió '{fid}' (parecido)"))
        return fid
    notas.append(Nota(campo, ADVERTIR, f"familia '{v}' no existe en el catálogo; se ignoró"))
    return None


# ----------------------------------------------------------------------------- utilidades
def _as_list(v: Any, campo: str, item_key: str, value_key: str, notas: list[Nota]) -> list:
    """Acepta la lista del contrato o un objeto {familia: valor} (se repara)."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and v and all(not isinstance(x, (dict, list)) for x in v.values()):
        notas.append(Nota(campo, REPARAR, "objeto {familia: valor} convertido a lista"))
        return [{item_key: k, value_key: x} for k, x in v.items()]
    raise Rechazo(f"'{campo}' debe ser una lista de objetos")


def _required(d: dict, key: str, what: str) -> Any:
    if key not in d or d[key] is None:
        raise Rechazo(f"falta el campo obligatorio '{key}' ({what})")
    return d[key]


def _check_fecha_rango(f: date, tid: int, cat: Catalogs, notas: list[Nota], prediccion: bool) -> None:
    if cat.historia_inicio and f < cat.historia_inicio:
        raise Rechazo(f"la fecha {f} es anterior al inicio de los datos ({cat.historia_inicio})")
    apertura = cat.tiendas.set_index("tienda_id").get("fecha_apertura")
    if apertura is not None and tid in apertura.index:
        ap = date.fromisoformat(str(apertura.loc[tid])[:10])
        if f < ap:
            raise Rechazo(f"la tienda {tid} abrió el {ap}; no opera en {f}")
    if prediccion and cat.feriados_fin and f > cat.feriados_fin:
        notas.append(Nota("fecha", ADVERTIR,
                          f"{f} es posterior al calendario de feriados ({cat.feriados_fin}); se asumió día sin feriado"))


# ----------------------------------------------------------------------------- solicitudes
def clean_prediccion(raw: Any, cat: Catalogs) -> tuple[dict, list[Nota]]:
    notas: list[Nota] = []
    if not isinstance(raw, dict):
        raise Rechazo("el cuerpo debe ser un objeto JSON con tienda_id, fecha y familias")
    d = normalize_keys(raw, ALIAS_PREDICCION, "", notas)
    tid = parse_tienda(_required(d, "tienda_id", "identificador de tienda"), cat, notas)
    f = parse_fecha(_required(d, "fecha", "AAAA-MM-DD"), notas)
    _check_fecha_rango(f, tid, cat, notas, prediccion=True)
    items = _as_list(_required(d, "familias", "lista de familias"), "familias", "familia_id", "en_promocion", notas)
    if not items:
        raise Rechazo("la lista 'familias' está vacía")

    elegidas: dict[str, tuple[int, int]] = {}  # familia -> (índice original, promoción)
    for i, it in enumerate(items):
        p = f"familias[{i}]"
        if not isinstance(it, dict):
            notas.append(Nota(p, ADVERTIR, f"elemento {it!r} no es un objeto; se ignoró"))
            continue
        it = normalize_keys(it, ALIAS_ITEM_PRED, f"{p}.", notas)
        if "familia_id" not in it:
            notas.append(Nota(p, ADVERTIR, "elemento sin familia_id; se ignoró"))
            continue
        fid = parse_familia(it["familia_id"], cat, f"{p}.familia_id", notas)
        if fid is None:
            continue
        if "en_promocion" not in it or it["en_promocion"] is None:
            notas.append(Nota(f"{p}.en_promocion", ADVERTIR, "falta en_promocion; se asumió 0"))
            promo = 0
        else:
            promo = to_count(it["en_promocion"], f"{p}.en_promocion", notas)
            if promo is None:
                notas.append(Nota(f"{p}.en_promocion", ADVERTIR, "se asumió 0 productos en promoción"))
                promo = 0
        if fid in elegidas:
            j, prev = elegidas[fid]
            if prev == promo:
                notas.append(Nota(p, REPARAR, f"'{fid}' repetida con los mismos valores; se tomó una vez"))
            else:
                notas.append(Nota(p, ADVERTIR, f"'{fid}' repetida con valores distintos ({prev} y {promo}); "
                                                f"se usó el primero (familias[{j}])"))
            continue
        elegidas[fid] = (i, promo)

    if not elegidas:
        raise Rechazo("ninguna familia de la solicitud es válida")
    faltan = [x for x in cat.familia_ids if x not in elegidas]
    if faltan:
        notas.append(Nota("familias", ADVERTIR,
                          f"faltan {len(faltan)} familias que la tienda vende ({', '.join(faltan)}); "
                          "solo se pronostican las recibidas"))
    limpio = {
        "tienda_id": tid,
        "fecha": f.isoformat(),
        "familias": [{"familia_id": k, "en_promocion": v[1]} for k, v in elegidas.items()],
    }
    return limpio, notas


def clean_observaciones(raw: Any, cat: Catalogs) -> tuple[dict, list[Nota]]:
    notas: list[Nota] = []
    if not isinstance(raw, dict):
        raise Rechazo("el cuerpo debe ser un objeto JSON con tienda_id, fecha y ventas")
    d = normalize_keys(raw, ALIAS_OBSERVACION, "", notas)
    tid = parse_tienda(_required(d, "tienda_id", "identificador de tienda"), cat, notas)
    f = parse_fecha(_required(d, "fecha", "AAAA-MM-DD"), notas)
    _check_fecha_rango(f, tid, cat, notas, prediccion=False)
    items = _as_list(_required(d, "ventas", "lista de ventas por familia"), "ventas", "familia_id", "ventas", notas)
    if not items:
        raise Rechazo("la lista 'ventas' está vacía")

    elegidas: dict[str, tuple[int, int]] = {}
    for i, it in enumerate(items):
        p = f"ventas[{i}]"
        if not isinstance(it, dict):
            notas.append(Nota(p, ADVERTIR, f"elemento {it!r} no es un objeto; no se guardó"))
            continue
        it = normalize_keys(it, ALIAS_ITEM_OBS, f"{p}.", notas)
        if "familia_id" not in it:
            notas.append(Nota(p, ADVERTIR, "elemento sin familia_id; no se guardó"))
            continue
        fid = parse_familia(it["familia_id"], cat, f"{p}.familia_id", notas)
        if fid is None:
            continue
        if "ventas" not in it or it["ventas"] is None:
            notas.append(Nota(f"{p}.ventas", ADVERTIR, "falta el valor de ventas; no se guardó"))
            continue
        v = to_count(it["ventas"], f"{p}.ventas", notas)
        if v is None:
            notas.append(Nota(f"{p}.ventas", ADVERTIR, "no se guardó el registro (no se inventan ventas)"))
            continue
        if fid in elegidas:
            j, prev = elegidas[fid]
            if prev == v:
                notas.append(Nota(p, REPARAR, f"'{fid}' repetida con el mismo valor; se guardó una vez"))
            else:
                notas.append(Nota(p, ADVERTIR, f"'{fid}' repetida con valores distintos ({prev} y {v}); "
                                                f"se guardó el primero (ventas[{j}])"))
            continue
        elegidas[fid] = (i, v)

    limpio = {
        "tienda_id": tid,
        "fecha": f.isoformat(),
        "ventas": [{"familia_id": k, "ventas": v[1]} for k, v in elegidas.items()],
    }
    return limpio, notas
