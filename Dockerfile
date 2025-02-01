# Используем базовый образ Python 3.10
FROM python:3.10-slim

# 1. Создадим рабочую директорию
WORKDIR app

# 2. Скопируем файл зависимостей (requirements.txt)
COPY requirements.txt app

# 3. Установим пакеты
RUN pip install --no-cache-dir -r requirements.txt

# 4. Скопируем остальной код (включая monitor.py, data и т.д.)
COPY . app

# На всякий случай, убедимся, что папка data существует
RUN mkdir -p appdata

# 5. Запуск скрипта мониторинга
CMD [python, monitor.py]
