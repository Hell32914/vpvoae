#!/bin/bash
set -e

echo "=========================================="
echo "🚀 VPVoAe Renderer Entrypoint"
echo "=========================================="

# Запуск Xvfb в фоне с поддержкой GLX
echo "🖥️  Starting Xvfb on :99 with GLX support..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset -dpi 96 > /tmp/xvfb.log 2>&1 &
XVF_PID=$!
echo "   Xvfb PID: $XVF_PID"

# Ждем пока Xvfb запустится
echo "⏳ Waiting for Xvfb to initialize..."
sleep 4

# Проверяем что Xvfb работает
if ! kill -0 $XVF_PID 2>/dev/null; then
    echo "❌ Xvfb failed to start! Check /tmp/xvfb.log"
    cat /tmp/xvfb.log
    exit 1
fi

echo "✅ Xvfb is running on :99"

# Запуск основного приложения
echo ""
echo "=========================================="
echo "📱 Starting VPVoAe Application"
echo "=========================================="

python /app/main.py
APP_EXIT_CODE=$?

echo ""
echo "=========================================="
echo "🛑 Shutting down Xvfb..."
echo "=========================================="

# Останавливаем Xvfb
kill $XVF_PID 2>/dev/null || true
wait $XVF_PID 2>/dev/null || true

echo "✅ Application finished (exit code: $APP_EXIT_CODE)"
exit $APP_EXIT_CODE
