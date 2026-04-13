import logging
import httpx
from .config import settings

logger = logging.getLogger(__name__)

# Сколько раз повторять при сетевой ошибке
_RETRIES = 2


async def _post(url: str, payload: dict) -> dict:
    """POST с повторными попытками."""
    last_exc = None
    for attempt in range(1, _RETRIES + 2):
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            if attempt <= _RETRIES:
                logger.warning("[bitrix] attempt %d failed: %s", attempt, e)
    logger.error("[bitrix] all retries failed for %s: %s", url, last_exc)
    raise last_exc


async def get_lead(lead_id: int) -> dict:
    """Получить данные лида из Bitrix24 по ID."""
    url = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.get.json"
    return await _post(url, {"id": lead_id})


_BITRIX_STATUS_MAP = {
    "new": "NEW",
    "processing": "IN_PROCESS",
    "connected": "CONVERTED",
    "failed": "JUNK",
    "retry": "IN_PROCESS",
}


async def update_lead_status(lead_id: int, status: str):
    """Обновить статус лида в Bitrix24."""
    status_id = _BITRIX_STATUS_MAP.get(status, "NEW")
    url = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.update.json"
    payload = {
        "id": lead_id,
        "fields": {"STATUS_ID": status_id},
    }
    return await _post(url, payload)


async def add_lead_comment(lead_id: int, comment: str):
    """Добавить комментарий к лиду через timeline."""
    url = f"{settings.BITRIX_WEBHOOK_URL}crm.timeline.comment.add.json"
    payload = {
        "fields": {
            "ENTITY_ID": lead_id,
            "ENTITY_TYPE": "lead",
            "COMMENT": comment,
        }
    }
    try:
        return await _post(url, payload)
    except Exception:
        # Fallback: старый метод через update COMMENTS
        url2 = f"{settings.BITRIX_WEBHOOK_URL}crm.lead.update.json"
        return await _post(url2, {"id": lead_id, "fields": {"COMMENTS": comment}})
