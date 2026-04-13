import hashlib
import logging
from typing import Dict, Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


def _make_hash(params_order: list[str], params: Dict[str, Any]) -> str:
    """MD5-хэш по правилам Sipuni: значения в заданном порядке + секрет."""
    values = [str(params.get(name, "")) for name in params_order]
    values.append(settings.SIPUNI_SECRET)
    hash_string = "+".join(values)
    return hashlib.md5(hash_string.encode("utf-8")).hexdigest()


async def make_outbound_call(manager_sipnumber: str, client_number: str) -> dict:
    """
    Инициировать звонок через Sipuni.
    reverse=0 → сначала звонок менеджеру, после его ответа — клиенту.

    Возвращает словарь с полями:
      success: bool
      raw: str  — исходный ответ Sipuni
      error: str | None
    """
    url = f"{settings.SIPUNI_API_BASE}/callback/call_number"

    params: Dict[str, Any] = {
        "user": settings.SIPUNI_USER,
        "phone": client_number,
        "sipnumber": manager_sipnumber,
        "reverse": 0,
        "antiaon": 0,
    }
    hash_value = _make_hash(
        ["antiaon", "phone", "reverse", "sipnumber", "user"], params
    )
    params["hash"] = hash_value

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, data=params)
            r.raise_for_status()
            raw_text = r.text

        # Sipuni может вернуть JSON или plain-text "1" / "0"
        try:
            data = r.json()
        except Exception:
            data = {}

        # Разбираем разные форматы ответа Sipuni
        # Вариант 1: {"success": true, ...}
        # Вариант 2: {"result": 1, ...}
        # Вариант 3: plain text "1"
        success = bool(
            data.get("success")
            or data.get("result") in (1, "1", True)
            or raw_text.strip() == "1"
        )

        logger.info(
            "[sipuni] call manager=%s client=%s success=%s raw=%s",
            manager_sipnumber, client_number, success, raw_text[:120],
        )
        return {"success": success, "raw": raw_text, "data": data, "error": None}

    except httpx.HTTPStatusError as e:
        logger.error("[sipuni] HTTP error: %s", e)
        return {"success": False, "raw": "", "error": f"HTTP {e.response.status_code}"}
    except httpx.TimeoutException:
        logger.error("[sipuni] timeout calling manager=%s", manager_sipnumber)
        return {"success": False, "raw": "", "error": "timeout"}
    except Exception as e:
        logger.error("[sipuni] unexpected error: %s", e)
        return {"success": False, "raw": "", "error": str(e)}


async def get_operators_status() -> str:
    """Получить CSV со статусами операторов."""
    url = f"{settings.SIPUNI_API_BASE}/statistic/operators"
    params = {"user": settings.SIPUNI_USER}
    hash_value = _make_hash(["user"], params)
    params["hash"] = hash_value

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, data=params)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.error("[sipuni] get_operators_status error: %s", e)
        return ""
