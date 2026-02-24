import asyncio
import os
import sys
import logging
from datetime import datetime
from playwright.async_api import async_playwright
from playwright.async_api import BrowserContext

# Конфигурация логирования для продакшена
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def main():
    """Главная функция для рендеринга веб-сайта на сервере с Xvfb."""
    browser = None
    try:
        # Получение конфигурации из переменных окружения
        output_path = os.getenv('OUTPUT_PATH', 'output')
        target_url = os.getenv('TARGET_URL', 'https://sleep-well-creatives.com')
        viewport_width = int(os.getenv('VIEWPORT_WIDTH', '1920'))
        viewport_height = int(os.getenv('VIEWPORT_HEIGHT', '1080'))
        render_timeout = int(os.getenv('RENDER_TIMEOUT', '5000'))
        load_timeout = int(os.getenv('LOAD_TIMEOUT', '60000'))
        
        logger.info("🚀 Запуск VPVoAe Web Renderer")
        logger.info(f"Target URL: {target_url}")
        logger.info(f"Viewport: {viewport_width}x{viewport_height}")
        logger.info(f"Display: {os.getenv('DISPLAY', 'не установлена')}")
        
        async with async_playwright() as p:
            logger.info("🌐 Запуск браузера на виртуальном дисплее (Xvfb)...")
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
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

            logger.info("📸 Создание скриншота...")
            # Создаем директорию для результатов
            os.makedirs(output_path, exist_ok=True)
            
            # Сохраняем полноразмерный скриншот с временной меткой
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(output_path, f"screenshot_{timestamp}.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            
            # Также сохраняем последний скриншот для быстрого доступа
            latest_path = os.path.join(output_path, "screenshot_latest.png")
            await page.screenshot(path=latest_path, full_page=True)

            await context.close()
            
            file_size = os.path.getsize(screenshot_path) / (1024 * 1024)  # MB
            logger.info(f"✅ Скриншот сохранен: {screenshot_path} ({file_size:.2f}MB)")
            logger.info(f"✅ Latest: {latest_path}")
            logger.info("✨ Серверное ядро готово к использованию")
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