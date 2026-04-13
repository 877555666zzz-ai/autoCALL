# AutoCall · Bitrix24 + Sipuni

Система автоматической обработки входящих лидов с дозвоном до клиента.

## Структура проекта

```
/
├── app/
│   ├── __init__.py
│   ├── config.py          # Настройки через .env
│   ├── db.py              # SQLAlchemy + SQLite
│   ├── models.py          # Manager, AutodialQueue, CallLog
│   ├── dispatcher.py      # Логика дозвона и очереди
│   ├── sipuni_client.py   # API Sipuni
│   ├── bitrix_client.py   # API Bitrix24
│   └── main.py            # FastAPI эндпоинты
├── static/
│   └── dashboard.html     # Дашборд управления
├── Procfile
├── requirements.txt
└── runtime.txt
```

## Переменные окружения (.env)

```env
BITRIX_WEBHOOK_URL=https://your-portal.bitrix24.ru/rest/1/your-token/
BITRIX_PORTAL_URL=https://your-portal.bitrix24.ru/
SIPUNI_USER=your_sipuni_user_id
SIPUNI_SECRET=your_sipuni_secret_key
```

## Деплой на Railway

1. Создайте проект на Railway
2. Подключите GitHub репозиторий
3. Добавьте переменные окружения через Settings → Variables
4. Railway автоматически запустит через `Procfile`

## API эндпоинты

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/` | Дашборд |
| GET | `/health` | Healthcheck |
| GET | `/managers` | Список менеджеров |
| POST | `/managers` | Добавить менеджера |
| PUT | `/managers/{id}` | Обновить менеджера |
| DELETE | `/managers/{id}` | Удалить менеджера |
| POST | `/managers/{id}/online` | Поставить на линию |
| POST | `/managers/{id}/offline` | Снять с линии |
| GET | `/logs` | Логи + очередь |
| GET | `/analytics` | Аналитика (за N дней) |
| POST | `/bitrix/webhook/lead` | Вебхук от Bitrix |
| GET | `/test/sipuni_call` | Тест звонка |
| POST | `/test/lead` | Тест обработки лида |

## Webhook в Bitrix24

Добавить исходящий вебхук:
- Событие: `ONCRMLEADADD`
- URL: `https://your-railway-app.railway.app/bitrix/webhook/lead`
- Метод: POST
