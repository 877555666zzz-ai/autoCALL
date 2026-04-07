import hashlib
from typing import Dict, Any
import httpx

from .config import settings


def _make_hash(params_order: list[str], params: Dict[str, Any]) -> str:
    """
    Формирование MD5-хэша по правилам Sipuni:
    значения в указанном порядке + секретный ключ.
    """
    values = [str(params.get(name, "")) for name in params_order]
    values.append(settings.SIPUNI_SECRET)
    hash_string = "+".join(values)
    return hashlib.md5(hash_string.encode("utf-8")).hexdigest()


async def get_operators_status() -> str:
    """
    Получаем CSV со списком сотрудников и статусов присутствия:
    /api/statistic/operators
    """
    url = f"{settings.SIPUNI_API_BASE}/statistic/operators"
    params = {
        "user": settings.SIPUNI_USER,
    }
    # порядок для подписи: user + секретный ключ
    hash_value = _make_hash(["user"], params)
    params["hash"] = hash_value

    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(url, data=params)
        r.raise_for_status()
        return r.text  # CSV


async def make_outbound_call(manager_sipnumber: str, client_number: str) -> dict:
    """
    Реальный вызов Sipuni: Звонок с внутреннего номера (менеджер) на внешний (клиент)
    URL: https://sipuni.com/api/callback/call_number

    Логика:
    - reverse=0: сначала звонок менеджеру (sipnumber),
      после его ответа — соединение с клиентом (phone).
    """
    url = f"{settings.SIPUNI_API_BASE}/callback/call_number"

    params: Dict[str, Any] = {
        "user": settings.SIPUNI_USER,
        "phone": client_number,          # номер клиента (7701...)
        "sipnumber": manager_sipnumber,  # внутренний номер менеджера
        "reverse": 0,                    # 0 = сначала менеджеру, потом клиенту
        "antiaon": 0,                    # 0 = не скрывать номер
    }

    # порядок для подписи: antiaon, phone, reverse, sipnumber, user, секретный ключ
    hash_value = _make_hash(
        ["antiaon", "phone", "reverse", "sipnumber", "user"],
        params
    )
    params["hash"] = hash_value

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, data=params)
        r.raise_for_status()
        return r.json()  # JSON от Sipuni
