# app/dispatcher.py
import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Dict

from sqlalchemy import select, delete

from .sipuni_client import make_outbound_call
from .bitrix_client import update_lead_status, add_lead_comment
from .db import async_session_maker
from .models import Manager, AutodialQueue, CallLog


# === ПРИОРИТИЗАЦИЯ ===
class ManagerPriority:
    def __init__(self):
        self.manager_stats = {}

    def update_stat(self, manager_id: int, success: bool):
        if manager_id not in self.manager_stats:
            self.manager_stats[manager_id] = {
                "total_calls": 0,
                "successful_calls": 0,
            }
        stats = self.manager_stats[manager_id]
        stats["total_calls"] += 1
        if success:
            stats["successful_calls"] += 1

    def get_priority_managers(self, managers):
        def priority_score(m):
            s = self.manager_stats.get(
                m.id,
                {"total_calls": 1, "successful_calls": 0}
            )
            return s["successful_calls"] / s["total_calls"]

        return sorted(managers, key=priority_score, reverse=True)


manager_priority = ManagerPriority()


async def get_available_managers() -> List[Manager]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.online == True)  # noqa
        )
        managers = list(result.scalars().all())
        return manager_priority.get_priority_managers(managers)


def _get_next_delay_minutes(attempts: int) -> int:
    if attempts == 1:
        return 5
    elif attempts == 2:
        return 15
    else:
        return 30


async def schedule_autodial(lead_id: int, phone: str, current_attempts: int):
    attempts = current_attempts + 1
    if attempts > 6:
        async with async_session_maker() as session:
            result = await session.execute(
                select(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
            )
            item = result.scalar_one_or_none()
            if item:
                item.state = "FAILED"
                await session.commit()

        try:
            await update_lead_status(lead_id, "failed")
            await add_lead_comment(
                lead_id,
                "Автодозвон: не удалось связаться после 6 попыток."
            )
        except Exception as e:
            print(f"[bitrix status error] {e}")
        return

    delay_min = _get_next_delay_minutes(attempts)
    next_call_time = datetime.utcnow() + timedelta(minutes=delay_min)

    async with async_session_maker() as session:
        result = await session.execute(
            select(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
        )
        item = result.scalar_one_or_none()

        if item:
            item.attempts = attempts
            item.phone = phone
            item.next_call_time = next_call_time
            item.state = "SCHEDULED"
        else:
            session.add(
                AutodialQueue(
                    lead_id=lead_id,
                    phone=phone,
                    attempts=attempts,
                    next_call_time=next_call_time,
                    state="SCHEDULED",
                )
            )
        await session.commit()


async def _log_call(
    lead_id: int,
    phone: str,
    call_type: str,
    status: str,
    attempts: List[Dict],
):
    async with async_session_maker() as session:
        session.add(
            CallLog(
                lead_id=lead_id,
                phone=phone,
                type=call_type,
                status=status,
                details=json.dumps(attempts, ensure_ascii=False),
            )
        )
        await session.commit()


async def _increment_missed(manager_id: int):
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return
        mgr.missed = (mgr.missed or 0) + 1
        if mgr.missed >= 3:
            mgr.online = False
            print(f"[discipline] Менеджер {mgr.name} исключён из очереди (3 пропуска)")
        await session.commit()


async def _reset_missed(manager_id: int):
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return
        mgr.missed = 0
        await session.commit()


async def process_new_lead(
    lead_id: int,
    client_phone: str,
    is_autodial: bool = False
) -> Dict:
    managers = await get_available_managers()
    call_type = "autodial" if is_autodial else "initial"

    if not managers:
        if not is_autodial:
            await schedule_autodial(lead_id, client_phone, current_attempts=0)
        await _log_call(lead_id, client_phone, call_type, "no_managers", [])
        return {"status": "no_managers_available", "lead_id": lead_id}

    try:
        await update_lead_status(lead_id, "processing")
    except Exception as e:
        print(f"[bitrix status error] {e}")

    attempts: List[Dict] = []

    for manager in managers:
        manager_id = manager.id
        sipnumber = manager.sipnumber

        sipuni_resp = await make_outbound_call(sipnumber, client_phone)
        attempts.append(
            {
                "manager_id": manager_id,
                "manager_name": manager.name,
                "sipuni_response": sipuni_resp,
            }
        )

        if isinstance(sipuni_resp, dict) and sipuni_resp.get("success") is True:
            async with async_session_maker() as session:
                await session.execute(
                    delete(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
                )
                await session.commit()

            await _reset_missed(manager_id)
            manager_priority.update_stat(manager_id, success=True)

            # увеличить счётчик принятых звонков
            async with async_session_maker() as session:
                mgr = await session.get(Manager, manager_id)
                if mgr:
                    mgr.accepted_calls = (mgr.accepted_calls or 0) + 1
                    await session.commit()

            await _log_call(lead_id, client_phone, call_type, "connected", attempts)

            try:
                await update_lead_status(lead_id, "connected")
                await add_lead_comment(
                    lead_id,
                    f"Автодозвон: соединён с менеджером {manager.name} ({sipnumber})."
                )
            except Exception as e:
                print(f"[bitrix status error] {e}")

            return {
                "status": "connected",
                "lead_id": lead_id,
                "connected_manager_id": manager_id,
                "attempts": attempts,
            }
        else:
            manager_priority.update_stat(manager_id, success=False)
            await _increment_missed(manager_id)

        await asyncio.sleep(7)

    if not is_autodial:
        await schedule_autodial(lead_id, client_phone, current_attempts=0)
        try:
            await update_lead_status(lead_id, "retry")
            await add_lead_comment(
                lead_id,
                "Автодозвон: не дозвонились, поставлен в очередь повторного дозвона."
            )
        except Exception as e:
            print(f"[bitrix status error] {e}")
    else:
        try:
            await update_lead_status(lead_id, "retry")
        except Exception as e:
            print(f"[bitrix status error] {e}")

    await _log_call(lead_id, client_phone, call_type, "no_answer", attempts)

    return {
        "status": "no_manager_answered",
        "lead_id": lead_id,
        "attempts": attempts,
    }


async def autodial_worker(poll_interval_seconds: int = 30):
    while True:
        try:
            now = datetime.utcnow()
            async with async_session_maker() as session:
                result = await session.execute(
                    select(AutodialQueue).where(
                        AutodialQueue.state == "SCHEDULED",
                        AutodialQueue.next_call_time <= now,
                    )
                )
                items = list(result.scalars().all())

                for item in items:
                    item.state = "IN_PROGRESS"
                await session.commit()

            for item in items:
                dial_result = await process_new_lead(
                    item.lead_id,
                    item.phone,
                    is_autodial=True,
                )
                if dial_result.get("status") != "connected":
                    await schedule_autodial(
                        item.lead_id,
                        item.phone,
                        current_attempts=item.attempts,
                    )

        except Exception as e:
            print(f"[autodial_worker error] {e}")

        await asyncio.sleep(poll_interval_seconds)