import httpx
from .config import settings
from datetime import datetime


async def get_lead(lead_id: int) -> dict:
    """Получить данные лида из Bitrix24 по ID"""
    url = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.get.json"
    payload = {"id": lead_id}
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def update_lead_status(lead_id: int, status: str):
    """
    Обновить статус лида в Bitrix24
    status: 'new' | 'processing' | 'connected' | 'failed' | 'retry'
    """
    STATUSES = {
        "new": "NEW",
        "processing": "IN_PROCESS",
        "connected": "CONVERTED",
        "failed": "JUNK",
        "retry": "IN_PROCESS",
    }
    status_id = STATUSES.get(status, "NEW")

    url = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.update.json"
    payload = {
        "id": lead_id,
        "fields": {
            "STATUS_ID": status_id,
            "COMMENTS": f"Автодозвон: {status}",
        },
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def add_lead_comment(lead_id: int, comment: str):
    """Добавить комментарий к лиду"""
    url = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.update.json"
    payload = {
        "id": lead_id,
        "fields": {
            "COMMENTS": comment
        }
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()