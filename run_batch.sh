#!/bin/bash
# run_batch.sh — обёртка для запуска оркестратора на сервере.
#
# Использование:
#   ./run_batch.sh urls.txt                    # 4 воркера, 2 ретрая
#   WORKERS=8 RETRIES=3 ./run_batch.sh urls.txt
#   ./run_batch.sh urls.txt --resume           # продолжить прерванный батч
#
# Перед первым запуском один раз собрать образ:
#   docker build -t vpvoae-renderer:latest .

set -euo pipefail

URLS_FILE="${1:-urls.txt}"
shift || true

WORKERS="${WORKERS:-4}"
RETRIES="${RETRIES:-2}"
IMAGE="${IMAGE:-vpvoae-renderer:latest}"
OUTPUT_ROOT="${OUTPUT_ROOT:-./batch-output}"
JOB_TIMEOUT="${JOB_TIMEOUT:-1800}"
SHM_SIZE="${SHM_SIZE:-2gb}"
MEM_LIMIT="${MEM_LIMIT:-4g}"
CPU_SHARES="${CPU_SHARES:-1024}"

if [ ! -f "$URLS_FILE" ]; then
  echo "❌ Файл с URL не найден: $URLS_FILE" >&2
  exit 1
fi

# Проверяем образ; если нет — собираем.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "🔨 Образ $IMAGE не найден, собираем…"
  docker build -t "$IMAGE" "$(dirname "$0")"
fi

mkdir -p "$OUTPUT_ROOT"

exec python3 "$(dirname "$0")/orchestrator.py" \
  --urls "$URLS_FILE" \
  --workers "$WORKERS" \
  --retries "$RETRIES" \
  --image "$IMAGE" \
  --output-root "$OUTPUT_ROOT" \
  --job-timeout "$JOB_TIMEOUT" \
  --shm-size "$SHM_SIZE" \
  --mem-limit "$MEM_LIMIT" \
  --cpu-shares "$CPU_SHARES" \
  "$@"
