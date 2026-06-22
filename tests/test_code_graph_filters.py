from __future__ import annotations

from MCUM.db.code_graph_store import infer_code_graph_filters


def test_infer_code_graph_filters_selects_single_explicit_layer() -> None:
    assert infer_code_graph_filters("revisar sincronizacion GPS Flutter") == {
        "languages": ["dart"],
        "inferred": True,
    }
    assert infer_code_graph_filters("revisar arquitectura completa Flutter y Go") == {}
