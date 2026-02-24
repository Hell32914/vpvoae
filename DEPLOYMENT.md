# Deployment Guide: VPVoAe на сервер 65.109.62.223

## 📋 Шаг 1: Подготовка на локальной машине

### Убедитесь, что файлы готовы:
```bash
cd d:\my_repo\vpvoae

# Файлы должны быть в репозитории:
ls -la
# main.py
# Dockerfile
# docker-compose.yml
# .env.example
# deploy.sh
# monitor.sh
# README.md (опционально)
```

### Коммитьте в Git:
```bash
git add .
git commit -m "Add docker-compose and deploy scripts for production"
git push origin main
```

---

## 🚀 Шаг 2: Развертывание на сервере

### 2.1 Подключитесь к серверу:
```bash
ssh root@65.109.62.223
```

### 2.2 Создайте директорию и разверните проект:
```bash
# Создание структуры
mkdir -p /srv/projects
cd /srv/projects

# Клонирование (замените URL на свой репозиторий)
git clone https://github.com/yourusername/vpvoae.git
cd vpvoae

# Создание .env для продакшена
cp .env.example .env.production

# ВАЖНО: Отредактируйте .env.production под свой сервер:
nano .env.production
```

### 2.3 Запустите скрипт развертывания:
```bash
chmod +x deploy.sh
./deploy.sh

# ИЛИ полностью вручную:
docker-compose build
docker-compose up -d
```

---

## ✅ Шаг 3: Проверка развертывания

### Проверите статус:
```bash
docker ps | grep vpvoae
docker-compose ps

# Должно быть:
# vpvoae-renderer   Up (healthy)
```

### Смотрите логи:
```bash
docker-compose logs -f

# Или только ошибки:
docker-compose logs --tail=30
```

### Проверьте, что файлы создаются:
```bash
ls -la output/

# Там должны появиться скриншоты (screenshot_*.png)
```

---

## ⚙️ Шаг 4: Конфигурация и оптимизация

### ⚠️ ВАЖНОЕ: Убедитесь, что используется свободный Xvfb дисплей

**На вашем сервере уже занят `:99`**, поэтому используем `:100`:

```bash
# Проверить активные дисплеи:
ps aux | grep Xvfb

# Вывод должен показать:
# :99 - занят (deploy user)
# :100 - свободен (для vpvoae)
```

### Проверьте порты (если приложение их слушает):
```bash
docker-compose port vpvoae
# Если есть порты - они должны быть свободны
```

### Проверьте диск:
```bash
df -h

# На вашем сервере 81% использовано - следите за output/ директорией!
# Удаляйте старые скриншоты если нужно:
find output/ -mtime +7 -delete  # Удалить старше 7 дней
```

---

## 🔧 Управление контейнером

### Запуск:
```bash
cd /srv/projects/vpvoae
docker-compose up -d
```

### Остановка:
```bash
docker-compose down

# Или просто перезагрузка:
docker-compose restart
```

### Просмотр логов:
```bash
# Последние 100 строк:
docker-compose logs --tail=100

# В реал-тайме:
docker-compose logs -f

# Только ошибки/warnings:
docker-compose logs | grep -i "error\|warning"
```

### Мониторинг:
```bash
# Использование ресурсов:
docker stats vpvoae-renderer

# Размер output:
du -sh /srv/projects/vpvoae/output
```

---

## 📊 Мониторинг (автоматический)

```bash
chmod +x monitor.sh
./monitor.sh

# Покажет в реал-тайме:
# - Статус контейнера
# - CPU/Memory usage
# - Размер output директории
# - Последние 10 строк логов
```

---

## 🐛 Решение проблем

### Проблема: Cannot connect to docker daemon
```bash
# Решение: Убедитесь, что Docker запущен
systemctl start docker
# Или используйте sudo
sudo docker-compose up -d
```

### Проблема: Display :100 в use
```bash
# Проверить какой процесс использует дисплей:
ps aux | grep ":100"

# Убить процесс если нужно (осторожно!):
kill -9 <PID>

# Или использовать другой дисплей в docker-compose.yml
```

### Проблема: Disk space full
```bash
# Проверить размер output:
du -sh /srv/projects/vpvoae/output

# Удалить старые файлы:
find /srv/projects/vpvoae/output -mtime +14 -delete

# Или очистить полностью (осторожно!):
rm -rf /srv/projects/vpvoae/output/*
mkdir -p /srv/projects/vpvoae/output
```

### Проблема: Контейнер постоянно рестартится
```bash
# Смотрите логи:
docker-compose logs

# Проверьте requirements:
docker exec vpvoae-renderer pip list | grep playwright

# Если нужно переустановить:
docker-compose build --no-cache
docker-compose up -d
```

---

## 📈 Автозапуск при перезагрузке системы

### Создайте Systemd unit:

```bash
sudo bash -c 'cat > /etc/systemd/system/vpvoae.service << EOF
[Unit]
Description=VPVoAe Web Renderer
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/srv/projects/vpvoae
ExecStart=/usr/bin/docker-compose up
ExecStop=/usr/bin/docker-compose down
Restart=always
RestartSec=10s
User=root

[Install]
WantedBy=multi-user.target
EOF'

# Активируйте:
sudo systemctl enable vpvoae
sudo systemctl start vpvoae

# Проверьте статус:
sudo systemctl status vpvoae
```

---

## 🎯 Интеграция с существующими проектами

### Ваш сервер уже имеет:
- ✅ Ruby Rails проекты (`current`, `37`)
- ✅ Sidekiq workers
- ✅ TG-bot
- ✅ Node.js проект (`cs2`)

### VPVoAe изолирован через:
- 🔒 Отдельный Docker контейнер
- 🔒 Отдельный Xvfb дисплей (`:100` вместо `:99`)
- 🔒 Отдельная volume для output
- 🔒 Лимиты на CPU (1024 shares) и память (2GB)
- 🔒 Собственная docker network (shared-network)

### Если нужно взаимодействие:
- Используйте имя сервиса: `vpvoae`
- Docker DNS разрешит на IP контейнера
- Пример: `http://vpvoae:8000` (если это нужно)

---

## ✨ Рекомендации

1. **Мониторьте диск** - он уже на 81%, output может расти
2. **Ротируйте логи** - docker-compose.yml уже настроен (макс 100MB)
3. **Проверяйте лог-файлы** - ищите ошибки на ранних стадиях
4. **Резервная копия** - периодически бэкапьте output
5. **Обновляйте базу** - слежите за обновлениями Playwright

---

## 📞 Техническая поддержка

Если возникают проблемы:

```bash
# Соберите информацию для диагностики:
echo "=== Server Info ===" > diagnostics.txt
uname -a >> diagnostics.txt
docker --version >> diagnostics.txt
docker-compose --version >> diagnostics.txt
echo "=== Docker Containers ===" >> diagnostics.txt
docker ps -a >> diagnostics.txt
echo "=== VPVoAe Logs ===" >> diagnostics.txt
docker-compose logs --tail=50 >> diagnostics.txt

cat diagnostics.txt
```

---

**Дата создания:** 2026-02-24  
**Версия:** 1.0  
**Для сервера:** 65.109.62.223 (Ubuntu 22.04.5 LTS)
