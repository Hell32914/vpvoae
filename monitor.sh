#!/bin/bash
# monitor.sh - Мониторинг vpvoae контейнера

CONTAINER_NAME="vpvoae-renderer"
CHECK_INTERVAL=10  # секунд

echo "=== VPVoAe Container Monitor ==="
echo "Container: $CONTAINER_NAME"
echo "Check interval: ${CHECK_INTERVAL}s"
echo "Press Ctrl+C to stop"
echo ""

while true; do
    clear
    echo "=== VPVoAe Monitor - $(date) ==="
    echo ""
    
    # Статус контейнера
    echo "📊 Статус контейнера:"
    docker ps --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Size}}"
    echo ""
    
    # Использование ресурсов
    echo "💾 Использование ресурсов:"
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}" | grep "$CONTAINER_NAME" || echo "Контейнер не запущен"
    echo ""
    
    # Размер output директории
    echo "📁 Output директория:"
    docker exec "$CONTAINER_NAME" du -sh /app/output 2>/dev/null || echo "Не удается получить размер"
    echo ""
    
    # Последние ошибки в логах
    echo "📝 Последние 10 строк логов:"
    docker logs --tail 10 "$CONTAINER_NAME" 2>/dev/null | head -10
    echo ""
    
    echo "⏳ Следующая проверка через ${CHECK_INTERVAL}s... (Ctrl+C для выхода)"
    sleep "$CHECK_INTERVAL"
done
