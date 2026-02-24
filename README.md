# 🎬 VPVoAe - Web Renderer with Playwright & Xvfb

Headless web rendering solution with full WebGL support for server environments.

## 📋 Описание

**VPVoAe** - это контейнеризированное приложение для:
- ✅ Рендеринга веб-страниц с поддержкой WebGL (через Xvfb)
- ✅ Создания скриншотов высокой четкости (1920x1080)
- ✅ Работы на серверах без X11 Display Manager
- ✅ Безопасной изоляции через Docker контейнеры
- ✅ Интеграции с существующими Docker-приложениями

## 🚀 Быстрый старт

### Локальная разработка

```bash
# 1. Клонировать репозиторий
git clone https://github.com/yourusername/vpvoae.git
cd vpvoae

# 2. Создать виртуальное окружение (опционально для локальной разработки)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
.\venv\Scripts\activate   # Windows

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Запустить локально (если есть X-сервер)
python main.py
```

### Развертывание на сервере

📖 Полная инструкция в [DEPLOYMENT.md](DEPLOYMENT.md)

⚡ Быстрый стартовый гайд в [QUICKSTART.md](QUICKSTART.md)

```bash
# Развернуть за 5 минут:
mkdir -p /srv/projects/vpvoae && cd /srv/projects/vpvoae
git clone https://github.com/yourusername/vpvoae.git .
cp .env.example .env.production
chmod +x deploy.sh && ./deploy.sh
```

## 📦 Требуемые зависимости

- **Docker** 20.10+
- **Docker Compose** 1.29+
- **Linux** (Ubuntu 20.04+, CentOS 8+, Debian 11+)
- **Disk space**: 5GB+ (для image и output)
- **RAM**: 2GB+ per container
- **CPU**: 1+ CPU cores

## 🐳 Docker структура

```
Dockerfile                    # Multi-stage build для оптимизации
docker-compose.yml            # Конфигурация контейнера
.env.example                  # Переменные окружения
main.py                       # Основное приложение
```

### Docker image layers

```
1. mcr.microsoft.com/playwright/python:v1.41.0-jammy
   └─ Содержит Playwright и зависимости для Chromium

2. System dependencies
   └─ Xvfb, GLX, Mesa, X11 libraries

3. Python application
   └─ main.py + logging + error handling
```

## ⚙️ Переменные окружения

| Переменная | Значение по умолчанию | Описание |
|------------|--------|-------------|
| `DISPLAY` | `:100` | Xvfb дисплей |
| `XVFB_DISPLAY` | `:100` | Xvfb дисплей (дублирование) |
| `XVFB_SCREEN` | `0 1920x1080x24` | Разрешение экрана |
| `LOG_LEVEL` | `INFO` | Уровень логирования (DEBUG, INFO, WARNING) |
| `OUTPUT_PATH` | `/app/output` | Директория для скриншотов |
| `TARGET_URL` | `https://sleep-well-creatives.com` | Целевой сайт |
| `VIEWPORT_WIDTH` | `1920` | Ширина окна браузера |
| `VIEWPORT_HEIGHT` | `1080` | Высота окна браузера |
| `LOAD_TIMEOUT` | `60000` | Таймаут загрузки (ms) |
| `RENDER_TIMEOUT` | `5000` | Время на рендер WebGL (ms) |

## 📂 Структура проекта

```
vpvoae/
├── main.py                    # Основное приложение
├── Dockerfile                 # Docker конфигурация
├── docker-compose.yml         # Docker Compose конфигурация
├── .env.example              # Шаблон переменных окружения
├── .gitignore                # Git ignore rules
├── deploy.sh                 # Скрипт развертывания
├── monitor.sh                # Скрипт мониторинга
├── vpvoae.service            # Systemd unit file
├── DEPLOYMENT.md             # Полное руководство деплоя
├── QUICKSTART.md             # Быстрый старт
├── README.md                 # Этот файл
└── output/                   # Директория для результатов
    ├── screenshot_20260224_091234.png
    └── screenshot_latest.png
```

## 🔧 Конфигурация

### docker-compose.yml ключевые настройки:

```yaml
environment:
  DISPLAY: :100                    # Xvfb дисплей
  
volumes:
  - ./output:/app/output           # Результаты
  
resources:
  cpus: "1.0"                      # Один CPU core
  memory: 2G                       # 2GB RAM максимум
  
restart_policy:
  condition: on-failure:3          # Перезагрузка при ошибках
```

## 🚀 Управление контейнером

### Запуск

```bash
cd /srv/projects/vpvoae
docker-compose up -d
```

### Просмотр статуса

```bash
docker-compose ps
docker stats vpvoae-renderer
```

### Логирование

```bash
# Последние 50 строк
docker-compose logs --tail 50

# В реал-тайме
docker-compose logs -f

# Только ошибки
docker-compose logs | grep -i error
```

### Остановка

```bash
docker-compose down
```

### Перезагрузка

```bash
docker-compose restart
```

### Очистка и переустановка

```bash
docker-compose down -v
docker-compose build --no-cache
docker-compose up -d
```

## 📊 Мониторинг

### Автоматический мониторинг

```bash
chmod +x /srv/projects/vpvoae/monitor.sh
./monitor.sh
```

Покажет:
- 📊 Статус контейнера
- 💾 Использование CPU/Memory
- 📁 Размер output директории  
- 📝 Последние логи

### Ручной мониторинг

```bash
# Размер выходной директории
du -sh /srv/projects/vpvoae/output

# Файлы output (последние созданные)
ls -lht /srv/projects/vpvoae/output | head -5

# Использование ресурсов
docker stats vpvoae-renderer

# Статус контейнера
docker inspect vpvoae-renderer
```

## 🔒 Безопасность

### Меры безопасности реализованные:

✅ **Контейнеризация** - полная изоляция от хоста  
✅ **Non-root user** - процесс не запущен от root в контейнере  
✅ **Resource limits** - лимиты CPU и памяти  
✅ **Volume mounting** - только необходимые директории  
✅ **No exposed ports** - нет открытых сетевых портов  
✅ **Log rotation** - автоматическая ротация логов  

### Лучшие практики:

1. **Используйте .env.production** - не коммитьте в Git
2. **Ограничьте доступ к output** - используйте правильные permissions
3. **Мониторьте логи** - выявляйте проблемы рано
4. **Обновляйте image** - следите за обновлениями Playwright
5. **Резервные копии** - бэкапьте important output файлы

## 🐛 Решение проблем

### Контейнер не запускается

```bash
# Проверьте логи
docker-compose logs

# Проверьте дисплей
ps aux | grep Xvfb

# Убедитесь, что в use только :100
# Если конфликт - измените в docker-compose.yml
```

### Диск переполнен

```bash
# Проверьте размер output
du -sh /srv/projects/vpvoae/output

# Удалите старые файлы (старше 7 дней)
find /srv/projects/vpvoae/output -mtime +7 -delete

# Или очистите полностью (осторожно!)
rm -rf /srv/projects/vpvoae/output/*
mkdir -p /srv/projects/vpvoae/output
```

### Ошибки Playwright

```bash
# Переустановите зависимости
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Проверьте версию Chromium
docker exec vpvoae-renderer playwright install-deps chromium
```

### Xvfb ошибки

```bash
# Проверьте запущенные дисплеи
ps aux | grep Xvfb

# Если процесс зависает - убейте его
pkill -f "Xvfb :100"

# Перезагрузите контейнер
docker-compose restart
```

## 📈 Интеграция с существующими системами

### На вашем сервере 65.109.62.223 вже есть:

- Ruby on Rails приложения (`current`, `37`)
- Sidekiq workers для асинхронных задач
- TG-bot
- Node.js приложение (`cs2`)

### VPVoAe интегрирован безопасно через:

🔒 **Отдельный Docker контейнер**  
🔒 **Отдельный Xvfb дисплей (`:100`)**  
🔒 **Своя Docker network (`shared-network`)**  
🔒 **Лимиты ресурсов (CPU, Memory)**  

### Если нужна интеграция:

```yaml
# В docker-compose.yml других проектов добавурьте:
networks:
  - shared-network  # Подключитесь к той же сети

# Тогда контейнеры смогут взаимодействовать:
# curl http://vpvoae:8000  (если открыт порт)
```

## 📝 Логирование

### Логирование настроено на:

1. **Stdout** - видно через `docker-compose logs`
2. **Systemd journal** - если через systemd сервис
3. **Ротация** - максимум 100MB на один файл (3 файла)

### Уровни логирования:

```
DEBUG   - Детальная диагностическая информация
INFO    - Информационные сообщения (по умолчанию)
WARNING - Предупреждения (что-то странное)
ERROR   - Ошибки (но приложение продолжает работать)
CRITICAL - Критические ошибки (выход из приложения)
```

### Смотрите логи:

```bash
# Интерактивно
docker-compose logs -f --tail=100

# Сохраните в файл
docker-compose logs > /tmp/vpvoae_logs.txt

# Только ошибки
docker-compose logs | grep -i "error\|exception\|failed"
```

## 🚀 Автозапуск при перезагрузке

### Вариант 1: Docker restart policy

Уже настроено в `docker-compose.yml`:
```yaml
restart: on-failure:3
```

### Вариант 2: Systemd unit

```bash
# Установите unit file
sudo install -m 644 vpvoae.service /etc/systemd/system/

# Активируйте
sudo systemctl enable vpvoae
sudo systemctl start vpvoae

# Проверьте
sudo systemctl status vpvoae

# Управление
sudo systemctl restart vpvoae
sudo systemctl stop vpvoae
sudo systemctl disable vpvoae
```

## 📚 Документация

- **[DEPLOYMENT.md](DEPLOYMENT.md)** - Полное руководство развертывания
- **[QUICKSTART.md](QUICKSTART.md)** - Быстрый старт (5 минут)
- **[main.py](main.py)** - Исходный код приложения
- **[Dockerfile](Dockerfile)** - Docker конфигурация
- **[docker-compose.yml](docker-compose.yml)** - Docker Compose конфигурация

## 🔗 Полезные ссылки

- [Playwright Python Documentation](https://playwright.dev/python/)
- [Docker Documentation](https://docs.docker.com/)
- [Xvfb Documentation](https://www.x.org/releases/current/doc/man/man1/Xvfb.1.xhtml)
- [Ubuntu Server Guide](https://ubuntu.com/server/docs)

## 📞 Поддержка и Контакты

Если возникают проблемы:

1. Проверьте [DEPLOYMENT.md](DEPLOYMENT.md) - раздел "Решение проблем"
2. Соберите логи: `docker-compose logs > diagnostics.txt`
3. Проверьте ресурсы: `docker stats`
4. Проверьте диск: `df -h`

---

**Версия:** 1.0  
**Последнее обновление:** 2026-02-24  
**Осремотр:** Ubuntu 22.04.5 LTS, Docker 20.10+  
**Сервер:** 65.109.62.223
