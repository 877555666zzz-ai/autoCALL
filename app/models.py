# app/models.py
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.sql import func

from .db import Base


class Manager(Base):
    __tablename__ = "managers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    sipnumber = Column(String, nullable=False)
    online = Column(Boolean, default=True)
    missed = Column(Integer, default=0)
    accepted_calls = Column(Integer, default=0)  # новое поле


class AutodialQueue(Base):
    __tablename__ = "autodial_queue"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, index=True, nullable=False)
    phone = Column(String, nullable=False)
    attempts = Column(Integer, default=0)
    next_call_time = Column(DateTime, nullable=False)
    state = Column(String, default="SCHEDULED")  # SCHEDULED, IN_PROGRESS, DONE, FAILED


class CallLog(Base):
    __tablename__ = "call_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False)
    lead_id = Column(Integer, index=True, nullable=False)
    phone = Column(String, nullable=False)
    type = Column(String, nullable=False)    # initial / autodial
    status = Column(String, nullable=False)  # connected / no_answer / no_managers / error
    details = Column(Text, nullable=True)    # JSON строка с деталями попыток