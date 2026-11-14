"""FIXTURE DE TEST UNIQUEMENT : mini serveur MCP pour vérifier le CLIENT MCP de l'agent.
Ce n'est PAS le catalogue (celui-ci est développé par Alassane)."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def rechercher_biens(quartier: str = "", budget_max: int = 0) -> dict:
    """Recherche de biens (fixture)."""
    biens = [{"id": "FIX-1", "quartier": "Mermoz", "loyer": 300000}]
    res = [b for b in biens if (not quartier or b["quartier"] == quartier) and (not budget_max or b["loyer"] <= budget_max)]
    return {"ok": True, "biens": res, "count": len(res)}


@mcp.tool()
def detail_bien(bien_id: str) -> dict:
    """Détail d'un bien (fixture)."""
    return {"ok": True, "bien": {"id": bien_id}} if bien_id == "FIX-1" else {"ok": False, "error": "introuvable"}


if __name__ == "__main__":
    mcp.run()
