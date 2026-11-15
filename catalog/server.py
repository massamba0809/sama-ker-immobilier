"""Outils MCP de recherche dans le catalogue fictif Sama Kër Immobilier."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mcp.server.fastmcp import FastMCP


PACKAGE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("CATALOG_DB_PATH", PACKAGE_DIR / "data" / "catalogue.sqlite3"))

BIENS: tuple[tuple[str, str, str, int, int, str, bool], ...] = (
    ("SK-001", "Appartement", "Mermoz", 300000, 3, "Mermoz, Dakar", True),
    ("SK-002", "Appartement", "Mermoz", 380000, 4, "Mermoz Pyrotechnie, Dakar", True),
    ("SK-003", "Appartement", "Mermoz", 450000, 3, "Mermoz ancienne piste, Dakar", False),
    ("SK-004", "Appartement", "Almadies", 550000, 4, "Route des Almadies, Dakar", True),
    ("SK-005", "Appartement", "Almadies", 700000, 5, "Almadies 2, Dakar", True),
    ("SK-006", "Studio", "Almadies", 250000, 1, "Ngor Almadies, Dakar", True),
    ("SK-007", "Appartement", "Ngor", 220000, 2, "Ngor village, Dakar", True),
    ("SK-008", "Appartement", "Ngor", 320000, 3, "Ngor Extension, Dakar", False),
    ("SK-009", "Maison", "Ngor", 600000, 5, "Ngor, Dakar", True),
    ("SK-010", "Appartement", "Ouakam", 200000, 2, "Ouakam Cité Avion, Dakar", True),
    ("SK-011", "Appartement", "Ouakam", 280000, 3, "Ouakam Monument de la Renaissance, Dakar", True),
    ("SK-012", "Maison", "Ouakam", 450000, 4, "Ouakam, Dakar", True),
    ("SK-013", "Appartement", "Sacré-Cœur", 350000, 3, "Sacré-Cœur 3, Dakar", True),
    ("SK-014", "Appartement", "Sacré-Cœur", 420000, 4, "Sacré-Cœur 1, Dakar", False),
    ("SK-015", "Studio", "Sacré-Cœur", 180000, 1, "Sacré-Cœur VDN, Dakar", True),
    ("SK-016", "Appartement", "Yoff", 240000, 2, "Yoff Virage, Dakar", True),
    ("SK-017", "Appartement", "Yoff", 330000, 3, "Yoff Tonghor, Dakar", True),
    ("SK-018", "Maison", "Yoff", 500000, 5, "Yoff Layène, Dakar", True),
    ("SK-019", "Appartement", "Plateau", 400000, 3, "Plateau, avenue Lamine Guèye, Dakar", True),
    ("SK-020", "Appartement", "Plateau", 520000, 4, "Plateau, rue Félix Faure, Dakar", True),
    ("SK-021", "Studio", "Plateau", 260000, 1, "Plateau, Dakar", True),
    ("SK-022", "Appartement", "Liberté 6", 275000, 2, "Liberté 6 Extension, Dakar", True),
    ("SK-023", "Appartement", "Liberté 6", 360000, 3, "Liberté 6, Dakar", True),
    ("SK-024", "Maison", "Liberté 6", 480000, 5, "Liberté 6, Dakar", True),
    ("SK-025", "Appartement", "Fann", 500000, 3, "Fann Résidence, Dakar", True),
    ("SK-026", "Appartement", "Fann", 650000, 4, "Fann Hock, Dakar", True),
    ("SK-027", "Maison", "Point E", 750000, 5, "Point E, Dakar", True),
    ("SK-028", "Appartement", "Point E", 390000, 3, "Point E, Dakar", True),
    ("SK-029", "Appartement", "HLM", 160000, 2, "HLM Grand Médine, Dakar", True),
    ("SK-030", "Villa", "Parcelles Assainies", 300000, 4, "Unité 15, Parcelles Assainies, Dakar", True),
)

mcp = FastMCP("sama-ker-catalogue")


def _normalize(value: str) -> str:
    folded = value.casefold().replace("œ", "oe").replace("æ", "ae")
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(
        char for char in decomposed
        if char.isalnum() and not unicodedata.combining(char)
    )


def _connect(database_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(database_path) if database_path is not None else DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _connection(database_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    connection = _connect(database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_path: Path | str | None = None) -> None:
    """Crée le schéma SQLite et insère les 30 biens de démonstration sans doublons."""
    with _connection(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS biens (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                type_key TEXT NOT NULL,
                quartier TEXT NOT NULL,
                quartier_key TEXT NOT NULL,
                loyer INTEGER NOT NULL CHECK (loyer > 0),
                pieces INTEGER NOT NULL CHECK (pieces > 0),
                adresse TEXT NOT NULL,
                disponible INTEGER NOT NULL CHECK (disponible IN (0, 1))
            )
            """
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO biens
                (id, type, type_key, quartier, quartier_key, loyer, pieces, adresse, disponible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (id_, kind, _normalize(kind), area, _normalize(area), rent, rooms, address, int(available))
                for id_, kind, area, rent, rooms, address, available in BIENS
            ],
        )


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("type_key", None)
    result.pop("quartier_key", None)
    result["disponible"] = bool(result["disponible"])
    return result


def search_properties(
    quartier: str | None = None,
    type: str | None = None,
    budget_max: int | None = None,
    pieces_min: int | None = None,
    pieces_max: int | None = None,
    disponible: bool | None = True,
    pieces: int | None = None,
    limit: int = 20,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    """Recherche les biens par filtres combinables; le budget maximal est inclusif."""
    if budget_max is not None and budget_max < 1:
        return {"ok": False, "error": "budget_max doit être un entier positif."}
    if pieces_min is not None and pieces_min < 1:
        return {"ok": False, "error": "pieces_min doit être supérieur ou égal à 1."}
    if pieces_max is not None and pieces_max < 1:
        return {"ok": False, "error": "pieces_max doit être supérieur ou égal à 1."}
    if pieces is not None and pieces < 1:
        return {"ok": False, "error": "pieces doit être supérieur ou égal à 1."}
    if pieces is not None and (pieces_min is not None or pieces_max is not None):
        return {"ok": False, "error": "pieces ne peut pas être combiné avec pieces_min ou pieces_max."}
    if pieces_min is not None and pieces_max is not None and pieces_min > pieces_max:
        return {"ok": False, "error": "pieces_min ne peut pas dépasser pieces_max."}
    if not 1 <= limit <= 50:
        return {"ok": False, "error": "limit doit être compris entre 1 et 50."}

    clauses: list[str] = []
    parameters: list[Any] = []
    if quartier and quartier.strip():
        clauses.append("quartier_key = ?")
        parameters.append(_normalize(quartier.strip()))
    if type and type.strip():
        clauses.append("type_key = ?")
        parameters.append(_normalize(type.strip()))
    if budget_max is not None:
        clauses.append("loyer <= ?")
        parameters.append(budget_max)
    if pieces_min is not None:
        clauses.append("pieces >= ?")
        parameters.append(pieces_min)
    if pieces_max is not None:
        clauses.append("pieces <= ?")
        parameters.append(pieces_max)
    if pieces is not None:
        clauses.append("pieces = ?")
        parameters.append(pieces)
    if disponible is not None:
        clauses.append("disponible = ?")
        parameters.append(int(disponible))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with _connection(database_path) as connection:
            count = connection.execute(f"SELECT COUNT(*) FROM biens{where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT id, type, quartier, loyer, pieces, adresse, disponible FROM biens{where} "
                "ORDER BY loyer, id LIMIT ?",
                [*parameters, limit],
            ).fetchall()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"Erreur SQLite pendant la recherche : {exc}"}
    return {"ok": True, "biens": [_as_dict(row) for row in rows], "count": count}


def get_property(bien_id: str, database_path: Path | str | None = None) -> dict[str, Any]:
    """Renvoie tous les champs d'un bien ou une erreur exploitable si son identifiant manque."""
    normalized_id = bien_id.strip().upper() if isinstance(bien_id, str) else ""
    if not normalized_id or len(normalized_id) > 32 or not re.fullmatch(r"[A-Z0-9-]+", normalized_id):
        return {"ok": False, "error": "bien_id est obligatoire et doit contenir uniquement lettres, chiffres et tirets."}
    try:
        with _connection(database_path) as connection:
            row = connection.execute(
                "SELECT id, type, quartier, loyer, pieces, adresse, disponible FROM biens WHERE id = ?",
                (normalized_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"Erreur SQLite pendant la lecture du bien : {exc}"}
    if row is None:
        return {"ok": False, "error": f"Aucun bien trouvé pour l'identifiant {normalized_id}."}
    return {"ok": True, "bien": _as_dict(row)}


@mcp.tool()
def rechercher_biens(
    quartier: str | None = None,
    type: str | None = None,
    budget_max: int | None = None,
    pieces_min: int | None = None,
    pieces_max: int | None = None,
    pieces: int | None = None,
    disponible: bool | None = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Recherche des locations par quartier, type, budget maximal, pièces et disponibilité.

    Les filtres se combinent. Les quartiers et types ignorent la casse et les accents.
    Par défaut, seuls les biens disponibles sont listés; passez disponible=false pour
    chercher les biens indisponibles ou null pour ne pas filtrer ce champ.
    """
    initialize_database()
    return search_properties(
        quartier=quartier,
        type=type,
        budget_max=budget_max,
        pieces_min=pieces_min,
        pieces_max=pieces_max,
        disponible=disponible,
        pieces=pieces,
        limit=limit,
    )


@mcp.tool()
def detail_bien(bien_id: str) -> dict[str, Any]:
    """Renvoie l'adresse, le loyer, le type, les pièces et la disponibilité d'un bien."""
    initialize_database()
    return get_property(bien_id)


@mcp.resource("agence://sama-ker-immobilier")
def informations_agence() -> str:
    """Règles et coordonnées de référence de l'agence pour le traitement des demandes."""
    return (
        "Sama Kër Immobilier — catalogue fictif de démonstration, Dakar, Sénégal. "
        "Les loyers sont mensuels et exprimés en francs CFA (XOF). Les disponibilités "
        "proviennent uniquement du catalogue de démonstration et doivent être confirmées "
        "par un conseiller avant toute visite. Ne jamais inventer un bien, une adresse ou "
        "un prix; utiliser rechercher_biens puis detail_bien pour les vérifier."
    )


if __name__ == "__main__":
    initialize_database()
    mcp.run()
