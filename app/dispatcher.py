# app/dispatcher.py
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict

from sqlalchemy import select, delete

from .sipuni_client import make_outbound_call
from .bitrix_client import update_lead_status, add_lead_comment
from .db import async_session_maker
from .models import Manager, AutodialQueue, CallLog

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ПРИОРИТИЗАЦИЯ МЕНЕДЖЕРОВ
# ─────────────────────────────────────────────

class ManagerPriority:
    """
    In-memory статистика успешных/неуспешных звонков.
    Используется для сортировки менеджеров по приоритету.
    Сбрасывается при перезапуске (чего достаточно для MVP).
    """

    def __init__(self):
        self._stats: Dict[int, Dict[str, int]] = {}

    def update(self, manager_id: int, success: bool):
        s = self._stats.setdefault(manager_id, {"total": 0, "ok": 0})
        s["total"] += 1
        if success:
            s["ok"] += 1

    def score(self, manager_id: int) -> float:
        s = self._stats.get(manager_id)
        if not s or s["total"] == 0:
            return 0.5  # нейтральный приоритет для новых менеджеров
        return s["ok"] / s["total"]

    def sort(self, managers: List[Manager]) -> List[Manager]:
        return sorted(managers, key=lambda m: self.score(m.id), reverse=True)


_priority = ManagerPriority()


# ─────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────

def _next_delay_minutes(attempts: int) -> int:
    """Задержка перед следующей попыткой автодозвона."""
    if attempts <= 1:
        return 5
    if attempts == 2:
        return 15
    return 30


async def _get_available_managers() -> List[Manager]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.online == True)  # noqa: E712
        )
        managers = list(result.scalars().all())
    return _priority.sort(managers)


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
    """Фиксируем пропущенный звонок; при 3 подряд — отключаем менеджера."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if not mgr:
            return
        mgr.missed = (mgr.missed or 0) + 1
        if mgr.missed >= 3:
            mgr.online = False
            logger.warning(
                "[discipline] Менеджер %s (id=%d) исключён из очереди — 3 пропуска подряд",
                mgr.name, manager_id,
            )
        await session.commit()


async def _reset_missed(manager_id: int):
    """Сбрасываем счётчик пропущенных после успешного соединения."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if mgr:
            mgr.missed = 0
            await session.commit()


async def _mark_accepted(manager_id: int):
    """Увеличить счётчик принятых звонков у менеджера."""
    async with async_session_maker() as session:
        mgr = await session.get(Manager, manager_id)
        if mgr:
            mgr.accepted_calls = (mgr.accepted_calls or 0) + 1
            await session.commit()


# ─────────────────────────────────────────────
# ПЛАНИРОВАНИЕ АВТОДОЗВОНА
# ─────────────────────────────────────────────

async def schedule_autodial(lead_id: int, phone: str, current_attempts: int):
    """
    Поставить лид в очередь на следующую попытку.
    Если попыток уже 6 — помечаем как FAILED и уведомляем Bitrix.
    """
    next_attempt = current_attempts + 1

    if next_attempt > 6:
        async with async_session_maker() as session:
            result = await session.execute(
                select(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
            )
            item = result.scalar_one_or_none()
            if item:
                item.state = "FAILED"
                await session.commit()

        logger.info("[autodial] Лид %d: исчерпаны все попытки (6/6)", lead_id)
        try:
            await update_lead_status(lead_id, "failed")
            await add_lead_comment(
                lead_id, "Автодозвон: не удалось связаться после 6 попыток."
            )
        except Exception as e:
            logger.error("[bitrix] ошибка обновления статуса лида %d: %s", lead_id, e)
        return

    delay_min = _next_delay_minutes(next_attempt)
    next_call_time = datetime.utcnow() + timedelta(minutes=delay_min)

    async with async_session_maker() as session:
        result = await session.execute(
            select(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
        )
        item = result.scalar_one_or_none()

        if item:
            item.attempts = next_attempt
            item.phone = phone
            item.next_call_time = next_call_time
            item.state = "SCHEDULED"
        else:
            session.add(
                AutodialQueue(
                    lead_id=lead_id,
                    phone=phone,
                    attempts=next_attempt,
                    next_call_time=next_call_time,
                    state="SCHEDULED",
                )
            )
        await session.commit()

    logger.info(
        "[autodial] Лид %d: попытка %d/%d запланирована через %d мин.",
        lead_id, next_attempt, 6, delay_min,
    )


# ─────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА ДОЗВОНА
# ─────────────────────────────────────────────

async def process_new_lead(
    lead_id: int,
    client_phone: str,
    is_autodial: bool = False,
) -> Dict:
    """
    Последовательно обзвонить доступных менеджеров.
    При успехе — соединяем. При неудаче — ставим в автодозвон.
    """
    managers = await _get_available_managers()
    call_type = "autodial" if is_autodial else "initial"

    if not managers:
        logger.warning("[dispatch] Лид %d: нет доступных менеджеров", lead_id)
        await _log_call(lead_id, client_phone, call_type, "no_managers", [])
        if not is_autodial:
            await schedule_autodial(lead_id, client_phone, current_attempts=0)
        return {"status": "no_managers_available", "lead_id": lead_id}

    try:
        await update_lead_status(lead_id, "processing")
    except Exception as e:
        logger.error("[bitrix] ошибка смены статуса лида %d: %s", lead_id, e)

    attempts: List[Dict] = []

    for manager in managers:
        logger.info(
            "[dispatch] Лид %d: звоним менеджеру %s (ext=%s)",
            lead_id, manager.name, manager.sipnumber,
        )

        sipuni_resp = await make_outbound_call(manager.sipnumber, client_phone)
        attempts.append({
            "manager_id": manager.id,
            "manager_name": manager.name,
            "sipnumber": manager.sipnumber,
            "sipuni_response": sipuni_resp,
        })

        if sipuni_resp.get("success"):
            # ✅ Соединение установлено
            async with async_session_maker() as session:
                await session.execute(
                    delete(AutodialQueue).where(AutodialQueue.lead_id == lead_id)
                )
                await session.commit()

            await _reset_missed(manager.id)
            await _mark_accepted(manager.id)
            _priority.update(manager.id, success=True)

            await _log_call(lead_id, client_phone, call_type, "connected", attempts)

            try:
                await update_lead_status(lead_id, "connected")
                await add_lead_comment(
                    lead_id,
                    f"Автодозвон: соединён с менеджером {manager.name} (ext. {manager.sipnumber}).",
                )
            except Exception as e:
                logger.error("[bitrix] ошибка обновления лида %d: %s", lead_id, e)

            logger.info(
                "[dispatch] Лид %d: СОЕДИНЁН с %s", lead_id, manager.name
            )
            return {
                "status": "connected",
                "lead_id": lead_id,
                "connected_manager": manager.name,
                "attempts": attempts,
            }

        # ❌ Менеджер не ответил
        _priority.update(manager.id, success=False)
        await _increment_missed(manager.id)

        # Пауза перед следующим менеджером (не блокирует event loop)
        await asyncio.sleep(7)

    # Никто не ответил
    if not is_autodial:
        await schedule_autodial(lead_id, client_phone, current_attempts=0)
        try:
            await update_lead_status(lead_id, "retry")
            await add_lead_comment(
                lead_id,
                "Автодозвон: никто не ответил, лид поставлен в очередь повторного дозвона.",
            )
        except Exception as e:
            logger.error("[bitrix] ошибка обновления лида %d: %s", lead_id, e)
    else:
        try:
            await update_lead_status(lead_id, "retry")
        except Exception as e:
            logger.error("[bitrix] ошибка обновления лида %d: %s", lead_id, e)

    await _log_call(lead_id, client_phone, call_type, "no_answer", attempts)

    logger.info("[dispatch] Лид %d: НИКТО НЕ ОТВЕТИЛ, попыток=%d", lead_id, len(attempts))
    return {
        "status": "no_manager_answered",
        "lead_id": lead_id,
        "attempts": attempts,
    }


# ─────────────────────────────────────────────
# ФОНОВЫЙ ВОРКЕР АВТОДОЗВОНА
# ─────────────────────────────────────────────

async def autodial_worker(poll_interval_seconds: int = 30):
    """
    Каждые poll_interval_seconds секунд проверяет очередь автодозвона
    и запускает обзвон для лидов, у которых наступило next_call_time.
    """
    logger.info("[autodial_worker] Запущен (интервал=%ds)", poll_interval_seconds)

    while True:
        try:
            now = datetime.utcnow()

            # Атомарно помечаем задачи как IN_PROGRESS
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

            if items:
                logger.info("[autodial_worker] Обрабатываем %d задач из очереди", len(items))

            for item in items:
                try:
                    result = await process_new_lead(
                        item.lead_id,
                        item.phone,
                        is_autodial=True,
                    )
                    if result.get("status") != "connected":
                        await schedule_autodial(
                            item.lead_id,
                            item.phone,
                            current_attempts=item.attempts,
                        )
                except Exception as e:
                    logger.error(
                        "[autodial_worker] Ошибка обработки лида %d: %s",
                        item.lead_id, e,
                    )
                    # Вернуть задачу в очередь через 5 минут
                    async with async_session_maker() as session:
                        r = await session.execute(
                            select(AutodialQueue).where(
                                AutodialQueue.lead_id == item.lead_id
                            )
                        )
                        q_item = r.scalar_one_or_none()
                        if q_item and q_item.state == "IN_PROGRESS":
                            q_item.state = "SCHEDULED"
                            q_item.next_call_time = datetime.utcnow() + timedelta(minutes=5)
                            await session.commit()

        except Exception as e:
            logger.error("[autodial_worker] Критическая ошибка: %s", e)

        await asyncio.sleep(poll_interval_seconds)
