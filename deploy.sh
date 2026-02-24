#!/bin/bash
# deploy.sh - Скрипт развертывания vpvoae на продакшене

set -e

DEPLOY_DIR="/srv/projects/vpvoae"
PROJECT_URL="${1:-https://github.com/yourusername/vpvoae.git}"
BRANCH="${2:-main}"

echo "=== VPVoAe Deployment Script ==="
echo "Target: $DEPLOY_DIR"
echo "Branch: $BRANCH"

# 1. Проверка прав
if [ "$EUID" -eq 0 ]; then 
   echo "⚠️  Рекомендуется не использовать root для deploy"
fi

# 2. Создание директории
echo "📁 Создание директории..."
mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"

# 3. Клонирование репозитория (если еще не клонирован)
if [ ! -d ".git" ]; then
   echo "📥 Клонирование репозитория..."
   git clone --branch "$BRANCH" "$PROJECT_URL" . 2>/dev/null || {
      echo "⚠️  Не удалось клонировать (возможно уже в проекте). Пропускаем..."
   }
else
   echo "📤 Обновление репозитория..."
   git fetch origin
   git checkout "$BRANCH"
   git pull origin "$BRANCH"
fi

# Если в .git нет - значит мы уже в проекте, не нужно гит операции
if [ ! -d ".git" ]; then
   echo "✅ Используем существующие файлы проекта"
fi

# 4. Создание .env файла
if [ ! -f ".env.production" ]; then
   echo "⚙️  Создание .env.production..."
   cp .env.example .env.production
   echo "⚠️  Отредактируйте .env.production перед запуском!"
fi

# 5. Проверка Docker
echo "🐳 Проверка Docker..."
if ! command -v docker &> /dev/null; then
   echo "❌ Docker не установлен!"
   exit 1
fi

if ! command -v docker-compose &> /dev/null; then
   echo "⚠️  Docker Compose V1 не найден, проверим V2..."
   if ! docker compose version &> /dev/null; then
      echo "❌ Docker Compose не установлен!"
      exit 1
   fi
fi

# 6. Сборка image
echo "🔨 Сборка Docker image..."
docker-compose build --no-cache

# 7. Запуск контейнера в фоне
echo "▶️  Запуск контейнера..."
docker-compose up -d

# 8. Проверка логов
echo "📋 Логи (последние 50 строк):"
sleep 3
docker-compose logs --tail=50

# 9. Проверка статуса
echo ""
echo "✅ Deployment завершен!"
echo ""
echo "📊 Статус:"
docker-compose ps
echo ""
echo "🔍 Проверка результатов:"
echo "  Директория: $DEPLOY_DIR"
echo "  Output: $DEPLOY_DIR/output"
echo ""
echo "📝 Полезные команды:"
echo "  Логи в реал-тайме: docker-compose -f $DEPLOY_DIR/docker-compose.yml logs -f"
echo "  Остановить: docker-compose -f $DEPLOY_DIR/docker-compose.yml down"
echo "  Перезапустить: docker-compose -f $DEPLOY_DIR/docker-compose.yml restart"
echo "  Статус: docker-compose -f $DEPLOY_DIR/docker-compose.yml ps"
