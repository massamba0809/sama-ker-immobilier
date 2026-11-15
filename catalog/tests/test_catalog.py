from __future__ import annotations

import sqlite3

import pytest

from catalog import server


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.sqlite3"
    server.initialize_database(path)
    return path


def test_seeds_thirty_unique_properties(database):
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM biens").fetchone()[0] == 30
        assert connection.execute("SELECT COUNT(DISTINCT id) FROM biens").fetchone()[0] == 30


def test_initialization_is_idempotent(database):
    server.initialize_database(database)
    server.initialize_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM biens").fetchone()[0] == 30


def test_search_combines_filters_and_budget_is_inclusive(database):
    result = server.search_properties(
        "Mermoz", "Appartement", 300000, pieces=3, database_path=database
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["biens"][0]["id"] == "SK-001"


def test_search_is_accent_and_case_insensitive(database):
    accented = server.search_properties("Sacré-Cœur", database_path=database)
    plain = server.search_properties("SACRE-COEUR", database_path=database)
    assert [item["id"] for item in accented["biens"]] == [item["id"] for item in plain["biens"]]
    assert accented["count"] == 2


def test_search_can_find_unavailable_or_all_properties(database):
    unavailable = server.search_properties("Mermoz", disponible=False, database_path=database)
    all_statuses = server.search_properties("Mermoz", disponible=None, database_path=database)
    assert [item["id"] for item in unavailable["biens"]] == ["SK-003"]
    assert all_statuses["count"] == 3


def test_no_results_is_successful_empty_result(database):
    result = server.search_properties("Dakar-Plateau-Nord", database_path=database)
    assert result == {"ok": True, "biens": [], "count": 0}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"budget_max": 0}, "budget_max"),
        ({"pieces_min": 0}, "pieces_min"),
        ({"pieces": 0}, "pieces"),
        ({"pieces": 3, "pieces_min": 2}, "ne peut pas être combiné"),
        ({"pieces_min": 4, "pieces_max": 2}, "ne peut pas dépasser"),
        ({"limit": 51}, "limit"),
    ],
)
def test_invalid_search_arguments_return_exploitable_errors(database, kwargs, message):
    result = server.search_properties(database_path=database, **kwargs)
    assert result["ok"] is False
    assert message in result["error"]


def test_detail_returns_complete_record(database):
    result = server.get_property("sk-001", database)
    assert result["ok"] is True
    assert result["bien"] == {
        "id": "SK-001",
        "type": "Appartement",
        "quartier": "Mermoz",
        "loyer": 300000,
        "pieces": 3,
        "adresse": "Mermoz, Dakar",
        "disponible": True,
    }


def test_detail_reports_unknown_and_malformed_ids(database):
    assert "Aucun bien trouvé" in server.get_property("SK-999", database)["error"]
    assert "bien_id est obligatoire" in server.get_property("'; DROP TABLE biens;--", database)["error"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM biens").fetchone()[0] == 30


def test_agency_resource_warns_about_demo_availability():
    info = server.informations_agence()
    assert "fictif" in info
    assert "doivent être confirmées" in info
