"""Temporary static stubs for services not yet implemented."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard() -> dict:
    return {
        "range": "12h",
        "filters": {},
        "metrics": [],
        "channels": [],
        "functions": [],
        "summary": {
            "tokenSpendUsd": 0,
            "qualifiedLeads": 0,
            "functionCalls": 0,
            "functionConversionRate": 0,
            "conversations": 0,
            "uniqueDialogs": 0,
            "messages": 0,
            "avgResponseMinutes": 0,
            "activeChannels": 0,
            "handoffRate": 0,
            "topChannel": "",
        },
    }


@router.get("/leads")
async def get_leads() -> list:
    return []


@router.get("/notifications")
async def get_notifications() -> list:
    return []
