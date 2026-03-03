#!/bin/bash
# Скрипт для копирования результатов из Docker контейнера

CONTAINER_NAME="vpvoae-renderer"
OUTPUT_DIR="./output"

# Создаём локальную директорию для output
mkdir -p "$OUTPUT_DIR"

echo "📦 Копирование файлов из контейнера $CONTAINER_NAME..."

# Копируем все файлы из /app/output контейнера в локальную папку
docker cp "$CONTAINER_NAME:/app/output/." "$OUTPUT_DIR/"

if [ $? -eq 0 ]; then
    echo "✅ Файлы успешно скопированы в $OUTPUT_DIR/"
    echo ""
    echo "Содержимое:"
    ls -lh "$OUTPUT_DIR/"
else
    echo "❌ Ошибка при копировании файлов"
    exit 1
fi
