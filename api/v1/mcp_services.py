"""REST endpoints for static MCP service discovery."""

from fastapi import APIRouter

from schemas.mcp import MCPServiceResponse


router = APIRouter()

SUPPORTED_MCP_SERVICES = [
    MCPServiceResponse(
        service_name="bitrix",
        display_name="Bitrix24 CRM",
        description="Интеграция с вашей CRM Bitrix24",
        required_keys=["webhook_url"],
    ),
    MCPServiceResponse(
        service_name="google_workspace",
        display_name="Google Workspace",
        description="Интеграция с Google Sheets, Calendar, Gmail, Drive и Contacts",
        required_keys=["client_id", "client_secret", "refresh_token"],
    ),
]


@router.get("/mcp-services", response_model=list[MCPServiceResponse])
async def list_mcp_services() -> list[MCPServiceResponse]:
    return SUPPORTED_MCP_SERVICES
