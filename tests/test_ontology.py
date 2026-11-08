from pathlib import Path

import pytest

from fos.ontology import Ontology, OntologyError
from fos.ontology.loader import load_ontology
from fos.ontology.schema import OntologyLayer
from fos.tenancy import TenantRegistry

REPO = Path(__file__).resolve().parents[1]


def _luneng() -> Ontology:
    t = TenantRegistry.load(REPO / "tenants").get("luneng")
    assert t
    return load_ontology(t, packs_dir=REPO / "packs", tenants_dir=REPO / "tenants")


def test_layers_merge():
    o = _luneng()
    assert o.layers == [
        "core",
        "pack:refractory",
        "pack:refractory/engineering",
        "pack:refractory/planning",
    ]
    assert "eng_project" in o.objects and "tc_a" in o.objects["product"].properties
    assert "kiln_run" in o.objects
    # 行业包给核心对象追加了属性
    assert o.objects["inspection"].properties["bulk_density"].unit == "g/cm³"
    assert o.objects["production_batch"].properties["kiln_run"].target == "kiln_run"


def _layer(d: dict) -> OntologyLayer:
    return OntologyLayer.model_validate(d)


BASE = {
    "objects": {"thing": {"name": "物", "domain": "x", "properties": {"a": {"type": "string", "name": "A"}}}},
}


@pytest.mark.parametrize(
    ("layer", "msg"),
    [
        (BASE, "已存在"),  # 重复定义对象类型
        ({"extend": {"thing": {"a": {"type": "number", "name": "A2"}}}}, "不能覆盖"),
        ({"extend": {"thing": {"b": {"type": "string", "name": "B", "required": True}}}}, "不能是必填"),
        ({"extend": {"nope": {"b": {"type": "string", "name": "B"}}}}, "不存在"),
    ],
)
def test_merge_rules(layer, msg):
    o = Ontology()
    o.add_layer("core", _layer(BASE))
    with pytest.raises(OntologyError, match=msg):
        o.add_layer("pack", _layer(layer))


def test_validate_catches_bad_events_and_refs():
    o = Ontology()
    o.add_layer(
        "core",
        _layer(
            {
                "objects": {
                    "thing": {
                        "name": "物",
                        "domain": "x",
                        "properties": {"a": {"type": "string", "name": "A", "required": True}},
                    }
                },
                "events": {
                    "x.thing_made": {
                        "name": "造",
                        "object": "thing",
                        "op": "create",
                        "permission": "x.thing:write",
                        "fields": [],
                    }
                },
            }
        ),
    )
    with pytest.raises(OntologyError, match="必填"):
        o.validate()


def test_payload_coercion():
    o = _luneng()
    ok = o.check_payload(
        "warehouse.lot_received",
        {"material": "m1", "qty": 32, "unit": "t", "received_at": "2026-11-02T08:30:00+08:00"},
        creating=True,
    )
    assert ok["qty"] == 32.0
    for bad, msg in [
        ({"material": "m1", "qty": "32", "unit": "t", "received_at": "2026-11-02T08:30:00+08:00"}, "number"),
        ({"material": "m1", "qty": 1, "unit": "t", "received_at": "2026-11-02T08:30:00"}, "时区"),
        ({"material": "m1", "qty": 1, "unit": "t"}, "必填"),
        (
            {"material": "m1", "qty": 1, "unit": "t", "received_at": "2026-11-02T08:30:00+08:00", "x": 1},
            "不接受",
        ),
        (
            {
                "material": "m1",
                "qty": 1,
                "unit": "t",
                "received_at": "2026-11-02T08:30:00+08:00",
                "status": "ok",
            },
            "取值",
        ),
    ]:
        with pytest.raises(OntologyError, match=msg):
            o.check_payload("warehouse.lot_received", bad, creating=True)
