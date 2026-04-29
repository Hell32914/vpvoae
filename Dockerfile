# Используем более новый образ Playwright compatible со всеми зависимостями
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Устанавливаем зависимости для WebGL и видеозаписи
RUN apt-get update && apt-get install -y \
    xvfb \
    ffmpeg \
    bash \
    libglib2.0-0 \
    libglvnd0 \
    libglvnd-dev \
    libglx0 \
    libgl1-mesa-glx \
    libgl1-mesa-dri \
    libxkbcommon0 \
    libxss1 \
    libxcomposite1 \
    libxdamage1 \
    libxrender1 \
    libxtst6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем требуемые файлы
COPY requirements.txt .
COPY main.py .
COPY entrypoint.sh /entrypoint.sh

# Устанавливаем зависимости из requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Создаем директорию для результатов
RUN mkdir -p /app/output

# Делаем entrypoint.sh исполняемым
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]