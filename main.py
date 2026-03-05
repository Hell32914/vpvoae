import asyncio
import os
import sys
import logging
import math
import random
import time
from typing import Any, Dict, List, Set, Tuple
from datetime import datetime
from playwright.async_api import async_playwright

# Конфигурация логирования для продакшена
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def cubic_bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    mt = 1 - t
    return (mt ** 3) * p0 + 3 * (mt ** 2) * t * p1 + 3 * mt * (t ** 2) * p2 + (t ** 3) * p3


async def collect_interactive_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Собирает видимые интерактивные DOM-элементы в текущем viewport."""
    return await page.evaluate(
        """
        ({ limit, viewportWidth, viewportHeight }) => {
            const selectors = [
                'a[href]',
                'button',
                'input:not([type="hidden"])',
                'select',
                'textarea',
                '[role="button"]',
                '[role="link"]',
                '[onclick]',
                '[tabindex]:not([tabindex="-1"])',
                '[data-hover]',
                '[class*="btn"]',
                '[class*="link"]',
                '[aria-haspopup="true"]'
            ];

            const now = performance.now();
            const nodes = document.querySelectorAll(selectors.join(','));
            const out = [];

            for (const el of nodes) {
                if (!(el instanceof HTMLElement)) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                    continue;
                }

                const rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) continue;
                if (rect.bottom < 0 || rect.right < 0 || rect.left > viewportWidth || rect.top > viewportHeight) {
                    continue;
                }

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;

                const area = Math.min(rect.width * rect.height, 12000);
                const distFromCenter = Math.hypot(cx - viewportWidth / 2, cy - viewportHeight / 2);
                const centerScore = Math.max(0, 1 - distFromCenter / Math.hypot(viewportWidth / 2, viewportHeight / 2));
                const pointerBoost = style.cursor === 'pointer' ? 120 : 0;
                const tagBoost = ({ button: 170, a: 130, input: 110, select: 110, textarea: 100 })[el.tagName.toLowerCase()] || 80;

                const text = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40);
                const idPart = el.id ? `#${el.id}` : '';
                const classPart = (el.className || '').toString().replace(/\\s+/g, '.').slice(0, 30);
                const key = `${el.tagName}${idPart}.${classPart}|${Math.round(cx)}:${Math.round(cy)}|${text}`;

                out.push({
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    width: rect.width,
                    height: rect.height,
                    score: area * 0.02 + centerScore * 100 + pointerBoost + tagBoost,
                    key
                });
            }

            out.sort((a, b) => b.score - a.score);
            return out.slice(0, Math.max(limit * 3, limit));
        }
        """,
        {
            "limit": limit,
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
        },
    )


async def move_mouse_human_like(
    page: Any,
    start: Tuple[float, float],
    end: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    duration_ms: int,
) -> Tuple[float, float]:
    """Имитирует естественный плавный путь курсора с легкой нестабильностью."""
    start_x, start_y = start
    end_x, end_y = end

    distance = math.hypot(end_x - start_x, end_y - start_y)
    base_steps = int(clamp(distance / 18, 12, 64))
    steps = int(clamp(base_steps + random.randint(-2, 6), 12, 72))

    cp1 = (
        start_x + (end_x - start_x) * random.uniform(0.2, 0.45) + random.uniform(-120, 120),
        start_y + (end_y - start_y) * random.uniform(0.1, 0.4) + random.uniform(-90, 90),
    )
    cp2 = (
        start_x + (end_x - start_x) * random.uniform(0.55, 0.85) + random.uniform(-120, 120),
        start_y + (end_y - start_y) * random.uniform(0.55, 0.9) + random.uniform(-90, 90),
    )

    for i in range(1, steps + 1):
        t = i / steps
        eased_t = t * t * (3 - 2 * t)

        x = cubic_bezier(eased_t, start_x, cp1[0], cp2[0], end_x)
        y = cubic_bezier(eased_t, start_y, cp1[1], cp2[1], end_y)

        # Шум уменьшается ближе к целевой точке, чтобы курсор "попадал" точно.
        noise_scale = (1 - eased_t) * 2.2
        x += random.uniform(-noise_scale, noise_scale)
        y += random.uniform(-noise_scale, noise_scale)

        x = clamp(x, 1, viewport_width - 1)
        y = clamp(y, 1, viewport_height - 1)

        await page.mouse.move(x, y)
        per_step_delay = max(4, int(duration_ms / steps + random.uniform(-4, 8)))
        await page.wait_for_timeout(per_step_delay)

    return end_x, end_y


async def run_smart_cursor(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    total_time_ms: int,
    max_targets: int,
    hover_min_ms: int,
    hover_max_ms: int,
) -> int:
    """Автоматически обходит интерактивные блоки, вызывая hover-эффекты без ручной разметки."""
    start_time = time.monotonic()
    visited_keys: Set[str] = set()
    hovered_count = 0
    empty_rounds = 0

    cursor_pos: Tuple[float, float] = (
        viewport_width * random.uniform(0.35, 0.65),
        viewport_height * random.uniform(0.35, 0.65),
    )
    await page.mouse.move(cursor_pos[0], cursor_pos[1])

    logger.info("🧭 Smart cursor: старт обхода интерактивных элементов")

    while (time.monotonic() - start_time) * 1000 < total_time_ms and hovered_count < max_targets:
        targets = await collect_interactive_targets(page, viewport_width, viewport_height, max_targets)
        candidates = [item for item in targets if item["key"] not in visited_keys]

        if not candidates:
            empty_rounds += 1

            if empty_rounds > 2:
                await page.mouse.wheel(0, int(viewport_height * random.uniform(0.45, 0.7)))
                await page.wait_for_timeout(random.randint(280, 700))

                at_bottom = await page.evaluate(
                    """() => (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 3)"""
                )
                if at_bottom:
                    await page.evaluate("""() => window.scrollTo({ top: 0, behavior: 'smooth' })""")
                    await page.wait_for_timeout(random.randint(350, 800))

                if empty_rounds > 5:
                    logger.info("🧭 Smart cursor: новых интерактивных узлов не найдено")
                    break

            continue

        empty_rounds = 0
        top_pool = sorted(candidates, key=lambda x: x["score"], reverse=True)[:8]
        target = random.choice(top_pool[:3] if len(top_pool) >= 3 else top_pool)

        tx = float(target["x"])
        ty = float(target["y"])

        travel_ms = random.randint(360, 1200)
        cursor_pos = await move_mouse_human_like(
            page,
            cursor_pos,
            (tx, ty),
            viewport_width,
            viewport_height,
            travel_ms,
        )

        # Микрокоррекции над элементом, чтобы ховер выглядел естественно.
        for _ in range(random.randint(1, 3)):
            jitter_x = clamp(tx + random.uniform(-4, 4), 1, viewport_width - 1)
            jitter_y = clamp(ty + random.uniform(-3, 3), 1, viewport_height - 1)
            await page.mouse.move(jitter_x, jitter_y)
            await page.wait_for_timeout(random.randint(30, 100))
            cursor_pos = (jitter_x, jitter_y)

        dwell_ms = random.randint(hover_min_ms, hover_max_ms)
        await page.wait_for_timeout(dwell_ms)

        hovered_count += 1
        visited_keys.add(target["key"])

        if random.random() < 0.28:
            await page.mouse.wheel(0, random.randint(80, 260))
            await page.wait_for_timeout(random.randint(180, 420))

    logger.info(f"🧭 Smart cursor: обработано интерактивных целей {hovered_count}")
    return hovered_count


async def main():
    """Главная функция для рендеринга веб-сайта на сервере с Xvfb и FFmpeg видеозаписью."""
    browser = None
    try:
        # Получение конфигурации из переменных окружения
        output_path = os.getenv('OUTPUT_PATH', 'output')
        target_url = os.getenv('TARGET_URL', 'https://sleep-well-creatives.com')
        viewport_width = int(os.getenv('VIEWPORT_WIDTH', '1920'))
        viewport_height = int(os.getenv('VIEWPORT_HEIGHT', '1080'))
        render_timeout = int(os.getenv('RENDER_TIMEOUT', '5000'))
        load_timeout = int(os.getenv('LOAD_TIMEOUT', '60000'))
        smart_cursor_enabled = env_bool('SMART_CURSOR_ENABLED', True)
        smart_cursor_timeout = int(os.getenv('SMART_CURSOR_TIMEOUT', '15000'))
        smart_cursor_max_targets = int(os.getenv('SMART_CURSOR_MAX_TARGETS', '24'))
        hover_min_ms = int(os.getenv('SMART_CURSOR_HOVER_MIN_MS', '450'))
        hover_max_ms = int(os.getenv('SMART_CURSOR_HOVER_MAX_MS', '1300'))

        if hover_max_ms < hover_min_ms:
            hover_max_ms = hover_min_ms
        
        logger.info("🚀 Запуск VPVoAe Web Renderer")
        logger.info(f"Target URL: {target_url}")
        logger.info(f"Viewport: {viewport_width}x{viewport_height}")
        logger.info(f"Display: {os.getenv('DISPLAY', ':99')}")
        logger.info("📹 Video recording: ENABLED (запись идёт параллельно)")
        logger.info(f"🧭 Smart cursor: {'ENABLED' if smart_cursor_enabled else 'DISABLED'}")
        
        async with async_playwright() as p:
            logger.info("🌐 Запуск браузера на виртуальном дисплее...")
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-extensions',
                    '--disable-web-resources'
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': viewport_width, 'height': viewport_height},
                device_scale_factor=1
            )
            page = await context.new_page()

            logger.info(f"📄 Открываем целевой сайт: {target_url}")
            # Ждем, пока перестанут подгружаться новые ресурсы (networkidle)
            try:
                await page.goto(
                    target_url,
                    wait_until="networkidle",
                    timeout=load_timeout
                )
                logger.info("✅ Сайт загружен успешно")
            except asyncio.TimeoutError:
                logger.warning(f"⏱️  Таймаут при загрузке ({load_timeout}ms), продолжаем...")

            logger.info(f"⏳ Ожидание отрисовки WebGL анимаций ({render_timeout}ms)...")
            # Сайт тяжелый, даем время на полный рендер WebGL
            await page.wait_for_timeout(render_timeout)

            if smart_cursor_enabled and smart_cursor_timeout > 0 and smart_cursor_max_targets > 0:
                logger.info(
                    f"🧭 Smart cursor активирован: budget={smart_cursor_timeout}ms, max_targets={smart_cursor_max_targets}"
                )
                await run_smart_cursor(
                    page=page,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    total_time_ms=smart_cursor_timeout,
                    max_targets=smart_cursor_max_targets,
                    hover_min_ms=hover_min_ms,
                    hover_max_ms=hover_max_ms,
                )
            else:
                logger.info("🧭 Smart cursor пропущен по конфигурации")

            logger.info("📸 Создание скриншота...")
            # Создаем директорию для результатов
            os.makedirs(output_path, exist_ok=True)
            
            # Сохраняем скриншот с временной меткой (без full_page для совместимости с Xvfb)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(output_path, f"screenshot_{timestamp}.png")
            await page.screenshot(path=screenshot_path, full_page=False, omit_background=False)
            
            # Также сохраняем последний скриншот для быстрого доступа
            latest_path = os.path.join(output_path, "screenshot_latest.png")
            await page.screenshot(path=latest_path, full_page=False, omit_background=False)

            await context.close()
            
            file_size = os.path.getsize(screenshot_path) / (1024 * 1024)  # MB
            logger.info(f"✅ Скриншот сохранен: {screenshot_path} ({file_size:.2f}MB)")
            logger.info(f"✅ Latest: {latest_path}")
            logger.info("✨ Рендеринг завершен успешно")
            logger.info("📹 FFmpeg продолжает запись (будет остановлен в entrypoint.sh)")
            sys.exit(0)

    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if browser:
            try:
                await browser.close()
                logger.info("🛑 Браузер закрыт")
            except Exception as e:
                logger.warning(f"Ошибка при закрытии браузера: {e}")


if __name__ == "__main__":
    logger.info("=" * 60)
    asyncio.run(main())