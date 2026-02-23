# Официальный образ Playwright с нужными системными зависимостями
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

# Устанавливаем Xvfb и все необходимые для WebGL зависимости
RUN apt-get update && apt-get install -y \
    xvfb \
    xvfb-run \
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

# Копируем код скрипта
COPY main.py .

# Устанавливаем Python-библиотеку
RUN pip install --no-cache-dir playwright

# Создаем директорию для результатов
RUN mkdir -p /app/output

# Команда запуска: создаем монитор 1920x1080 и внутри него запускаем скрипт
CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "python", "main.py"]