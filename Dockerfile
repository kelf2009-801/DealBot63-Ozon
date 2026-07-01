FROM python:3.11-slim

WORKDIR /bot

# Системные зависимости для Chromium (нужен CloakBrowser)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates gnupg \
    fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 \
    libnss3 libu2f-udev libvulkan1 libxcomposite1 libxdamage1 \
    libxfixes3 libxkbcommon0 libxrandr2 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Ставим зависимости из requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ставим CloakBrowser с его Chromium
RUN python -m cloakbrowser install

# Копируем бота и настройки
COPY main_full.py .
COPY config.py .
COPY .env .

# Тома для данных и логов
VOLUME ["/bot/data"]

ENV PYTHONPATH=""
ENV PYTHONUNBUFFERED=1

CMD ["python", "main_full.py"]