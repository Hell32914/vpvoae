import asyncio
import os
import sys
from playwright.async_api import async_playwright
from playwright.async_api import BrowserContext

async def main():
    """Главная функция для рендеринга веб-сайта на сервере с Xvfb."""
    browser = None
    try:
        async with async_playwright() as p:
            # Запускажиме для поддержки WebGL через Xvfb
            # Окно не вылезет, так как Xvfb перехваем НЕ в headless ретит его в виртуальный дисплей
            print("[Server Core] Запуск браузера на виртуальном дисплее...")
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1
            )
            page = await context.new_page()

            print("[Server Core] Открываем целевой сайт...")
            # Ждем, пока перестанут подгружаться новые ресурсы (networkidle)
            try:
                await page.goto(
                    "https://sleep-well-creatives.com",
                    wait_until="networkidle",
                    timeout=60000
                )
                print("[Server Core] Сайт загружен успешно")
            except asyncio.TimeoutError:
                print("[Server Core] Таймаут при загрузке, продолжаем...")

            print("[Server Core] Ожидание отрисовки WebGL анимаций...")
            # Сайт тяжелый, даем 5 секунд на полный рендер WebGL
            await page.wait_for_timeout(5000)

            print("[Server Core] Создание скриншота...")
            # Создаем директорию для результатов
            os.makedirs('output', exist_ok=True)
            
            # Сохраняем полноразмерный скриншот
            screenshot_path = "output/screenshot.png"
            await page.screenshot(path=screenshot_path, full_page=True)

            await context.close()
            print(f"[Server Core] ✅ Скриншот успешно сохранен: {screenshot_path}")
            print("[Server Core] Серверное ядро готово к использованию")
            sys.exit(0)

    except Exception as e:
        print(f"[Server Core] ❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

if __name__ == "__main__":
    asyncio.run(main())