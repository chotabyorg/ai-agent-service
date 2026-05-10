"""REST endpoints for static MCP service discovery."""

from fastapi import APIRouter

from schemas.mcp import MCPServiceResponse


router = APIRouter()

SUPPORTED_MCP_SERVICES = [
    MCPServiceResponse(
        service_name="bitrix",
        display_name="Bitrix24 CRM",
        description="Интеграция с вашей CRM Bitrix24",
        required_keys=["portal_url"],
    )
]


@router.get("/mcp-services", response_model=list[MCPServiceResponse])
async def list_mcp_services() -> list[MCPServiceResponse]:
    return SUPPORTED_MCP_SERVICES
