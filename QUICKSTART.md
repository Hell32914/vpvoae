# 🚀 Quick Start - VPVoAe Deployment

## ⚡ 5-минутный деплой

```bash
# 1. На сервере (замените URL на свой репозиторий)
ssh root@65.109.62.223

mkdir -p /srv/projects && cd /srv/projects
git clone https://github.com/yourusername/vpvoae.git && cd vpvoae

# 2. Подготовка конфига
cp .env.example .env.production
# Отредактируйте если нужно: nano .env.production

# 3. Запуск
chmod +x deploy.sh
./deploy.sh

# 4. Проверка
docker-compose ps
docker-compose logs --tail 20
```

---

## 🔍 Проверка результатов

```bash
# Контейнер работает?
docker ps | grep vpvoae-renderer

# Файлы создаются?
ls -la /srv/projects/vpvoae/output/

# На серверу все хорошо?
docker-compose logs
```

---

## ⚙️ Основные команды

**Просмотр логов:**
```bash
docker-compose -f /srv/projects/vpvoae/docker-compose.yml logs -f
```

**Остановить:**
```bash
cd /srv/projects/vpvoae && docker-compose down
```

**Перезапустить:**
```bash
cd /srv/projects/vpvoae && docker-compose restart
```

**Полная переустановка:**
```bash
cd /srv/projects/vpvoae
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

---

## ⚠️ Важные детали для вашего сервера

| Параметр | Значение | Статус |
|----------|---------|--------|
| **Xvfb Display** | `:100` | ✅ Свободен (`:99` занят) |
| **Container Name** | `vpvoae-renderer` | ✅ Уникален |
| **Memory Limit** | 2GB | ✅ Достаточно |
| **CPU Shares** | 1024 | ✅ Справедливое разделение |
| **Network** | shared-network | ✅ Отделен от других |
| **Disk Usage** | 81% | ⚠️ **СЛЕДИТЬ!** |

---

## 🔐 Безопасность

✅ Non-root запуск в контейнере  
✅ Отсутствие портов наружу (не нужны)  
✅ Volume-mounted only output  
✅ Лимиты ресурсов  
✅ Автоматическая ротация логов  

---

## 📞 Если что-то не работает

1. **Проверьте логи:**
   ```bash
   docker-compose logs | head -50
   ```

2. **Проверьте дисплей:**
   ```bash
   ps aux | grep Xvfb
   ```

3. **Проверьте Docker:**
   ```bash
   docker ps -a
   docker images | grep vpvoae
   ```

4. **Проверьте диск:**
   ```bash
   df -h
   ```

---

**Сервер:** 65.109.62.223 (Ubuntu 22.04.5 LTS)  
**Дата:** 2026-02-24
