# GP Access Control Plane

GP Access Control Plane - управляющий контур для Linux-хоста, который подбирает рабочие стратегии `zapret2` через `blockcheck2.sh` и дает локальную web panel для оператора.

`Control plane` здесь означает отдельный управляющий слой: GP собирает данные, запускает проверки и хранит результаты, а пользовательский трафик остается в `data plane` на роутере или другом целевом устройстве.

## Установка

### 1. Обновите систему

Перед первой установкой на чистую систему лучше отдельно обновить пакеты и перезагрузить хост:

```bash
sudo apt update
sudo apt upgrade -y
sudo reboot
```

На уже настроенной системе этот шаг остается вашим решением. Установщик GP не делает полный `apt upgrade` сам.

### 2. Запустите установщик

Обычная установка с Core service и Web UI:

```bash
curl -LfsS https://github.com/balbomush/GP-access-control-plane/raw/main/scripts/bootstrap-linux.sh | bash
```

Headless-установка без штатного Web UI:

```bash
curl -LfsS https://github.com/balbomush/GP-access-control-plane/raw/main/scripts/bootstrap-linux.sh | GP_INSTALL_WEB=off bash
```

Bootstrap-скрипт ставит минимальные зависимости для загрузки (`ca-certificates`, `curl`, `git`), находит последний стабильный git tag и запускает установщик из этого tag. Если `sudo` нужен, скрипт запросит его сам.

Установка проверяется на Debian/Ubuntu-like системах с `apt-get` и `systemd`.

После установки откройте:

```text
http://<ip-board>:8080/
```

API-контракт доступен здесь:

- Swagger UI: `http://<ip-board>:8080/swagger`;
- raw OpenAPI JSON: `http://<ip-board>:8080/openapi.json`.

В headless-only режиме эти маршруты доступны на локальном Core API: `http://127.0.0.1:8081/swagger` и `http://127.0.0.1:8081/openapi.json`. Web/monolith OpenAPI показывает полный контракт, а headless Core OpenAPI показывает только callable Core/Service/OpenAPI операции.

### Безопасность и вход

При первом запуске используйте учетные данные `admin` / `admin`. Сразу после входа обязательно смените пароль: начальные учетные данные известны всем, кто читает эту документацию.

`POST /api/auth/login` выдает Bearer-токен сроком на 24 часа. Например:

```bash
curl -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}'
```

Сохраните значение `access_token` из ответа и передайте его при смене пароля:

```bash
curl -X POST "$BASE_URL/api/auth/change-password" \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"current_password":"admin","new_password":"новый-надежный-пароль"}'
```

Среди API без токена доступны только `GET /api/health` и `POST /api/auth/login`; для всех остальных API-операций требуется заголовок `Authorization: Bearer <access_token>`.

Swagger UI и raw OpenAPI можно открыть без токена. Чтобы выполнить защищенный метод через **Try it out**, сначала укажите Bearer-токен через кнопку **Authorize** в Swagger UI.

### Конфиг Установки

Для нестандартных параметров создайте env-файл и передайте его через `GP_INSTALL_CONFIG`:

```bash
cat > gp-install.env <<'EOF'
GP_INSTALL_WEB=on
GP_INSTALL_DIR="$HOME/gp/GP-access-control-plane"
GP_SERVICE_MEMORY_HIGH=768M
GP_SERVICE_MEMORY_MAX=1500M
EOF

export GP_INSTALL_CONFIG="$PWD/gp-install.env"
curl -LfsS https://github.com/balbomush/GP-access-control-plane/raw/main/scripts/bootstrap-linux.sh | bash
```

Без конфига проект ставится в `~/gp/GP-access-control-plane`, а данные хранятся в `~/gp/GP-access-control-plane/build/state`.

Что делает установщик:

- ставит нужные пакеты через `apt-get install`;
- устанавливает `zapret2` в `/opt/zapret2`;
- скачивает GP и создает Python-окружение;
- устанавливает команду `gp-control-plane`;
- готовит локальный каталог `v2fly/domain-list-community`;
- ставит root-helper для запуска `blockcheck2` без интерактивного sudo-пароля;
- создает и запускает systemd-сервисы.

## Проверки После Установки

Проверить root-helper и `zapret2`:

```bash
gp-control-plane zapret2 check-install
```

В выводе должны быть `root_helper_found: true` и `root_helper_ready: true`.

Проверить Web UI:

```bash
curl -I http://127.0.0.1:8080/
```

Проверить сервисы:

```bash
sudo systemctl status gp-control-plane-core.service
sudo systemctl status gp-control-plane-web.service
```

## Управление Сервисом

Старт:

```bash
sudo systemctl start gp-control-plane-core.service
sudo systemctl start gp-control-plane-web.service
```

Перезапуск:

```bash
sudo systemctl restart gp-control-plane-core.service
sudo systemctl restart gp-control-plane-web.service
```

Остановка:

```bash
sudo systemctl stop gp-control-plane-web.service
sudo systemctl stop gp-control-plane-core.service
```

Логи:

```bash
journalctl -u gp-control-plane-core.service -u gp-control-plane-web.service -f
```

Для headless-установки используйте только `gp-control-plane-core.service`.

## Установка zapret2 Отдельно

Полный установщик GP уже ставит `zapret2`. Если нужен только `zapret2`, запустите отдельный короткий скрипт:

```bash
curl -LfsS https://github.com/balbomush/GP-access-control-plane/raw/main/scripts/install-zapret2.sh | bash
```

После установки должны появиться:

```text
/opt/zapret2/blockcheck2.sh
/opt/zapret2/nfq2/nfqws2
```

## Как Пользоваться

1. Откройте web panel: `http://<ip-board>:8080/`.
2. Во вкладке `Подбор` выберите домены.
3. Запустите обычный или экспериментальный поиск.
4. Во вкладке `Терминал` смотрите ход работы.
5. Во вкладке `Кандидаты` смотрите найденные стратегии.
6. В `Настройки` -> `Бекапы и восстановление` скачайте архив, если нужен откат.
7. Скопируйте подходящую стратегию вручную и проверьте ее там, где планируете использовать.

Подбор может длиться несколько часов. Кнопка остановки сохраняет найденные к этому моменту стратегии.

## Что Умеет Текущая Версия

- запускать локальную web panel;
- запускать подбор стратегий через штатный `blockcheck2.sh`;
- проверять одну стратегию сразу на нескольких доменах;
- ограничивать количество параллельных `curl`;
- включать и выключать проверки HTTP, TLS 1.2, TLS 1.3, HTTP3/QUIC;
- использовать встроенные пресеты доменов;
- показывать прогресс, live-лог и историю запусков;
- сохранять найденные стратегии в локальную SQLite-БД;
- показывать стратегии по доменам и общие стратегии для выбранных доменов;
- останавливать долгий подбор без потери уже найденных успешных стратегий;
- создавать и восстанавливать локальные бекапы через UI.

## Обновление

Повторно запустите bootstrap:

```bash
curl -LfsS https://github.com/balbomush/GP-access-control-plane/raw/main/scripts/bootstrap-linux.sh | bash
```

Он установит последний стабильный git tag, обновит Python-окружение и перезапустит сервисы. Для явной установки ветки или tag задайте `GP_BRANCH`.

## Данные И Бекапы

По умолчанию локальные данные лежат здесь:

```text
~/gp/GP-access-control-plane/build/state/
```

Файловые бекапы лежат здесь:

```text
~/gp/GP-access-control-plane/build/backups/
```

Каталог состояния можно переопределить через `GP_STATE_DIR` или `--state-dir`. Данные остаются на хосте и никуда не публикуются.
