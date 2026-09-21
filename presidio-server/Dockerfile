FROM python:3.10-slim

WORKDIR /app

# 1. Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Установка Python-пакетов (включая fastapi и uvicorn)
RUN pip install --no-cache-dir \
    presidio-analyzer \
    presidio-anonymizer \
    spacy \
    fastapi \
    uvicorn

# 3. Запекание модели Spacy в образ
RUN python -m spacy download en_core_web_lg

# 4. Копирование кода
COPY . .

EXPOSE 5001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5001"]