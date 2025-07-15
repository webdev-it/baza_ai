# Используем официальный Python
FROM python:3.11-slim

# Устанавливаем ffmpeg для pydub
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Создаём рабочую папку
WORKDIR /app

# Копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Если хочешь скрыть .env локально, рендер подтянет свои ENV
# ENTRYPOINT перехватывает SIGTERM и др. для корректного завершения
ENTRYPOINT ["python", "bot.py"]
