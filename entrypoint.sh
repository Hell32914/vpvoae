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

# Запускаем FFmpeg для видеозаписи
echo ""
echo "=========================================="
echo "🎥 Starting FFmpeg video capture"
echo "=========================================="

OUTPUT_PATH=${OUTPUT_PATH:-/app/output}
VIDEO_OUTPUT_FILE="${OUTPUT_PATH}/recording_$(date +%Y%m%d_%H%M%S).mp4"
FFMPEG_FRAMERATE=${FFMPEG_FRAMERATE:-15}
FFMPEG_PRESET=${FFMPEG_PRESET:-ultrafast}
FFMPEG_CRF=${FFMPEG_CRF:-24}
FFMPEG_CROP_TOP=${FFMPEG_CROP_TOP:-0}
FFMPEG_AUTO_CROP_BROWSER_UI=${FFMPEG_AUTO_CROP_BROWSER_UI:-true}
FFMPEG_DRAW_MOUSE=${FFMPEG_DRAW_MOUSE:-0}
FFMPEG_THREADS=${FFMPEG_THREADS:-2}
FFMPEG_NICE_LEVEL=${FFMPEG_NICE_LEVEL:-10}

if ! [[ "$FFMPEG_FRAMERATE" =~ ^[0-9]+$ ]]; then
  FFMPEG_FRAMERATE=18
fi
if [ "$FFMPEG_FRAMERATE" -lt 8 ]; then
  FFMPEG_FRAMERATE=8
fi
if [ "$FFMPEG_FRAMERATE" -gt 60 ]; then
  FFMPEG_FRAMERATE=60
fi
if ! [[ "$FFMPEG_THREADS" =~ ^[0-9]+$ ]]; then
  FFMPEG_THREADS=2
fi
if [ "$FFMPEG_THREADS" -lt 1 ]; then
  FFMPEG_THREADS=1
fi
if ! [[ "$FFMPEG_NICE_LEVEL" =~ ^-?[0-9]+$ ]]; then
  FFMPEG_NICE_LEVEL=10
fi
if [ "$FFMPEG_NICE_LEVEL" -lt -20 ]; then
  FFMPEG_NICE_LEVEL=-20
fi
if [ "$FFMPEG_NICE_LEVEL" -gt 19 ]; then
  FFMPEG_NICE_LEVEL=19
fi
if [ "$FFMPEG_DRAW_MOUSE" != "0" ] && [ "$FFMPEG_DRAW_MOUSE" != "1" ]; then
  FFMPEG_DRAW_MOUSE=0
fi

# На некоторых окружениях X11 браузерные панели остаются видимыми даже в kiosk/fullscreen.
# Включаем безопасный автокроп верхней полосы, если явный crop не задан.
if [ "$FFMPEG_CROP_TOP" -eq 0 ] && [ "$FFMPEG_AUTO_CROP_BROWSER_UI" = "true" ]; then
  FFMPEG_CROP_TOP=$((SCREEN_HEIGHT / 11))
fi

FILTER_ARGS=()
if [ "$FFMPEG_CROP_TOP" -gt 0 ]; then
  # y (crop top) тоже делаем чётным, чтобы избежать проблем кодека.
  if [ $((FFMPEG_CROP_TOP % 2)) -ne 0 ]; then
    FFMPEG_CROP_TOP=$((FFMPEG_CROP_TOP + 1))
  fi

  CROP_HEIGHT=$((SCREEN_HEIGHT - FFMPEG_CROP_TOP))
  if [ "$CROP_HEIGHT" -lt 100 ]; then
    CROP_HEIGHT=100
  fi

  # h должен быть чётным для yuv420p/libx264.
  if [ $((CROP_HEIGHT % 2)) -ne 0 ]; then
    CROP_HEIGHT=$((CROP_HEIGHT - 1))
  fi

  # Пересчитываем верхний отступ после коррекции высоты.
  FFMPEG_CROP_TOP=$((SCREEN_HEIGHT - CROP_HEIGHT))

  FILTER_ARGS=(-vf "crop=${SCREEN_WIDTH}:${CROP_HEIGHT}:0:${FFMPEG_CROP_TOP}")
fi

# Запускаем FFmpeg в фоне для захвата видео с виртуального дисплея.
# По умолчанию crop отключен, чтобы верхняя навигация сайта попадала в запись.
nice -n "$FFMPEG_NICE_LEVEL" ffmpeg -f x11grab \
  -video_size ${SCREEN_WIDTH}x${SCREEN_HEIGHT} \
    -framerate "$FFMPEG_FRAMERATE" \
    -draw_mouse "$FFMPEG_DRAW_MOUSE" \
  -i :99 \
  "${FILTER_ARGS[@]}" \
  -c:v libx264 \
    -preset "$FFMPEG_PRESET" \
    -crf "$FFMPEG_CRF" \
    -threads "$FFMPEG_THREADS" \
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
echo "   Draw X11 mouse: ${FFMPEG_DRAW_MOUSE}"
echo "   FFmpeg threads: ${FFMPEG_THREADS}"
echo "   FFmpeg nice level: ${FFMPEG_NICE_LEVEL}"

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

# Фаза предзагрузки: если PRELOAD_TIME_S > 0 — открываем сайт заранее,
# чтобы все анимации и медиа загрузились ДО начала записи с курсором.
PRELOAD_TIME_S=${PRELOAD_TIME_S:-0}
if [ "$PRELOAD_TIME_S" -gt 0 ] 2>/dev/null; then
    echo "⏳ Preload phase: загружаем сайт ${PRELOAD_TIME_S}s до начала записи..."
    PRELOAD_MODE=1 timeout "${PRELOAD_TIME_S}" python /app/main.py 2>&1 | tail -30 || true
    echo "✅ Preload phase завершена, начинаем запись с курсором"
fi

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
