# Используем более новый образ Playwright compatible со всеми зависимостями
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Устанавливаем зависимости для WebGL
RUN apt-get update && apt-get install -y \
    xvfb \
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

# Устанавливаем зависимости из requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Создаем директорию для результатов
RUN mkdir -p /app/output

# Команда запуска переопределяется в docker-compose.yml
CMD ["python", "main.py"]

# Устанавливаем Python-библиотеку
RUN pip install --no-cache-dir playwright

# Создаем директорию для результатов
RUN mkdir -p /app/output

# Команда запуска: создаем монитор 1920x1080 и внутри него запускаем скрипт
CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "python", "main.py"]