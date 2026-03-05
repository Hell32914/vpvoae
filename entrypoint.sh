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

# Запускаем FFmpeg для видеозаписи
echo ""
echo "=========================================="
echo "🎥 Starting FFmpeg video capture"
echo "=========================================="

OUTPUT_PATH=${OUTPUT_PATH:-/app/output}
VIDEO_OUTPUT_FILE="${OUTPUT_PATH}/recording_$(date +%Y%m%d_%H%M%S).mp4"
FFMPEG_FRAMERATE=${FFMPEG_FRAMERATE:-24}
FFMPEG_PRESET=${FFMPEG_PRESET:-ultrafast}
FFMPEG_CRF=${FFMPEG_CRF:-23}
FFMPEG_CROP_TOP=${FFMPEG_CROP_TOP:-0}
FFMPEG_AUTO_CROP_BROWSER_UI=${FFMPEG_AUTO_CROP_BROWSER_UI:-true}

# На некоторых окружениях X11 браузерные панели остаются видимыми даже в kiosk/fullscreen.
# Включаем безопасный автокроп верхней полосы, если явный crop не задан.
if [ "$FFMPEG_CROP_TOP" -eq 0 ] && [ "$FFMPEG_AUTO_CROP_BROWSER_UI" = "true" ]; then
  FFMPEG_CROP_TOP=96
fi

FILTER_ARGS=()
if [ "$FFMPEG_CROP_TOP" -gt 0 ]; then
  CROP_HEIGHT=$((1080 - FFMPEG_CROP_TOP))
  if [ "$CROP_HEIGHT" -lt 100 ]; then
    CROP_HEIGHT=100
  fi
  FILTER_ARGS=(-vf "crop=1920:${CROP_HEIGHT}:0:${FFMPEG_CROP_TOP}")
fi

# Запускаем FFmpeg в фоне для захвата видео с виртуального дисплея.
# По умолчанию crop отключен, чтобы верхняя навигация сайта попадала в запись.
ffmpeg -f x11grab \
  -video_size 1920x1080 \
    -framerate "$FFMPEG_FRAMERATE" \
  -i :99 \
  "${FILTER_ARGS[@]}" \
  -c:v libx264 \
    -preset "$FFMPEG_PRESET" \
    -crf "$FFMPEG_CRF" \
  -pix_fmt yuv420p \
  -y \
  "$VIDEO_OUTPUT_FILE" > /tmp/ffmpeg.log 2>&1 &
FFMPEG_PID=$!
echo "   FFmpeg PID: $FFMPEG_PID"
echo "   Recording to: $VIDEO_OUTPUT_FILE"
if [ "$FFMPEG_CROP_TOP" -gt 0 ]; then
  echo "   Crop top: ${FFMPEG_CROP_TOP}px"
else
  echo "   Crop top: disabled"
fi
echo "   Auto crop browser UI: ${FFMPEG_AUTO_CROP_BROWSER_UI}"

sleep 2

if ! kill -0 $FFMPEG_PID 2>/dev/null; then
    echo "❌ FFmpeg failed to start! Check /tmp/ffmpeg.log"
    cat /tmp/ffmpeg.log
    kill $XVF_PID 2>/dev/null || true
    exit 1
fi

echo "✅ FFmpeg is recording at ${FFMPEG_FRAMERATE}fps with preset=${FFMPEG_PRESET}, crf=${FFMPEG_CRF}"

# Запуск основного приложения
echo ""
echo "=========================================="
echo "📱 Starting VPVoAe Application"
echo "=========================================="

DISPLAY=:99 python /app/main.py
APP_EXIT_CODE=$?

echo ""
echo "=========================================="
echo "🎬 Stopping FFmpeg capture..."
echo "=========================================="

# Останавливаем FFmpeg gracefully (отправляем сигнал завершения)
kill -TERM $FFMPEG_PID 2>/dev/null || true

# Даём FFmpeg время на graceful shutdown (максимум 10 секунд)
for i in {1..10}; do
    if ! kill -0 $FFMPEG_PID 2>/dev/null; then
        break
    fi
    sleep 1
done

# Если FFmpeg ещё работает, принудительно завершаем
kill -9 $FFMPEG_PID 2>/dev/null || true
wait $FFMPEG_PID 2>/dev/null || true

echo "✅ Video recording stopped"

echo ""
echo "=========================================="
echo "🛑 Shutting down Xvfb..."
echo "=========================================="

# Останавливаем Xvfb
kill $XVF_PID 2>/dev/null || true
wait $XVF_PID 2>/dev/null || true

echo "✅ Application finished (exit code: $APP_EXIT_CODE)"
echo "✅ Output files:"
echo "   - Video: $VIDEO_OUTPUT_FILE"
echo "   - Screenshots: ${OUTPUT_PATH}/screenshot_*.png"

exit $APP_EXIT_CODE
