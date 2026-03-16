#!/bin/bash
set -e

echo "=========================================="
echo "🚀 VPVoAe Renderer Entrypoint"
echo "=========================================="

SCREEN_WIDTH=${VIEWPORT_WIDTH:-1920}
SCREEN_HEIGHT=${VIEWPORT_HEIGHT:-1080}
SCREEN_DEPTH=${XVFB_COLOR_DEPTH:-24}

if ! [[ "$SCREEN_WIDTH" =~ ^[0-9]+$ ]]; then
  SCREEN_WIDTH=1920
fi
if ! [[ "$SCREEN_HEIGHT" =~ ^[0-9]+$ ]]; then
  SCREEN_HEIGHT=1080
fi
if ! [[ "$SCREEN_DEPTH" =~ ^[0-9]+$ ]]; then
  SCREEN_DEPTH=24
fi

# libx264 (yuv420p) требует чётные размеры кадра.
if [ $((SCREEN_WIDTH % 2)) -ne 0 ]; then
  SCREEN_WIDTH=$((SCREEN_WIDTH - 1))
fi
if [ $((SCREEN_HEIGHT % 2)) -ne 0 ]; then
  SCREEN_HEIGHT=$((SCREEN_HEIGHT - 1))
fi

# Очистка устаревших lock-файлов Xvfb (после аварийного перезапуска контейнера)
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Запуск Xvfb в фоне с поддержкой GLX
echo "🖥️  Starting Xvfb on :99 with GLX support..."
Xvfb :99 -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} -ac +extension GLX +render -noreset -dpi 96 > /tmp/xvfb.log 2>&1 &
XVF_PID=$!
echo "   Xvfb PID: $XVF_PID"
echo "   Screen: ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}"

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

# FFmpeg управляется напрямую из Python (subprocess.Popen после 30-секундного прогрева).
# Xvfb запущен выше — этого достаточно для работы Python-скрипта.

# Запуск основного приложения (Python сам стартует FFmpeg после прогрева)
echo ""
echo "=========================================="
echo "📱 Starting VPVoAe Application"
echo "=========================================="

DISPLAY=:99 python /app/main.py
APP_EXIT_CODE=$?

echo ""
echo "=========================================="
echo "🛑 Shutting down Xvfb..."
echo "=========================================="

# FFmpeg был остановлен Python-скриптом до выхода.
# Останавливаем Xvfb.
kill $XVF_PID 2>/dev/null || true
wait $XVF_PID 2>/dev/null || true

OUTPUT_PATH_DISPLAY=${OUTPUT_PATH:-/app/output}
echo "✅ Application finished (exit code: $APP_EXIT_CODE)"
echo "✅ Output files in: ${OUTPUT_PATH_DISPLAY}/"

exit $APP_EXIT_CODE
