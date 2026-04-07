# app/db.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite+aiosqlite:///./app.db"  # файл app.db в корне проекта

engine = create_async_engine(
    DATABASE_URL, echo=False, future=True,
)

async_session_maker = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def init_db():
    """
    Создаёт таблицы при старте приложения.
    """
    from . import models  # импортируем модели, чтобы Base знала о таблицах
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
