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
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-access-control-plane/releases/download/v0.4.0/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.0 bash
```

Headless-установка без штатного Web UI:

```bash
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-access-control-plane/releases/download/v0.4.0/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.0 GP_INSTALL_WEB=off bash
```

Укажите exact annotated release tag в `GP_BRANCH`. До единственного запроса `sudo` bootstrap сначала проверяет уже существующий device-local vault; при его отсутствии экспортирует legacy-state. На чистом хосте без legacy-state он выполняет non-destructive initial install без vault. Затем один штатный installer-process останавливает сервисы, удаляет только прежнюю GP-поверхность и ставит fresh версию из того же tag. При ошибке vault до удаления ничего не меняется; после удаления разрешён только повтор fresh-install. Откат не поддерживается.

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

GP Control Plane v0.4.0 рассчитан на доверенную локальную сеть. Веб-интерфейс
слушает адрес платы на порту `8080`, а при первом запуске намеренно использует
учётные данные `admin` / `admin`. Это не способ безопасно публиковать сервис в
интернете или в недоверенной общей сети: не открывайте этот порт наружу и не
размещайте плату в такой сети.

Если состав или доверенность локальной сети меняются, сразу смените пароль.
Начальные учётные данные известны всем, кто читает эту документацию.

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

### Параметры Установки

Поддерживаются только topology-параметры, переданные в окружении bootstrap:

```bash
cat > gp-install.env <<'EOF'
GP_INSTALL_WEB=on
EOF

set -a; . ./gp-install.env; set +a
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-access-control-plane/releases/download/v0.4.0/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.0 bash
```

Проект ставится в `~/gp/GP-access-control-plane`; clean-install не принимает внешний путь состояния. Для новой рабочей установки постоянные данные хранятся рядом с каталогом проекта: состояние — в `~/gp/.GP-access-control-plane.data/state`, файловые бекапы — в `~/gp/.GP-access-control-plane.data/backups`.

Миграция legacy-state в v0.4.0 поддерживает только стандартный путь `$HOME/gp/GP-access-control-plane/build/state`. Настроенный или внешний путь состояния не входит в scope этой миграции.

Топология выбирается только перед запуском: `GP_INSTALL_WEB=on` ставит Core и Web, `off` — только Core. После fresh-install восстановите vault в UI/API, подтвердив `confirm_restore=true`; источник удаляется только после semantic-проверки и готовности/integrity SQLite.

Что делает установщик:

- ставит нужные пакеты через `apt-get install`;
- устанавливает `zapret2` в `/opt/zapret2`;
- скачивает GP и создает Python-окружение;
- устанавливает команду `gp-control-plane`;
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
GP_ZAPRET_INSTALLER_URL='https://github.com/balbomush/GP-access-control-plane/releases/latest/download/install-zapret2.sh'
curl -LfsS "$GP_ZAPRET_INSTALLER_URL" | bash
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

## Два движка подбора (fork: blockcheck2 + blockcheckS)

Ветка `feat/dual-engine-main` добавляет движок **blockcheckS** (`bs`) наряду со
штатным `blockcheck2.sh`. Переключатель «Движок подбора» (панель подбора и
Настройки → Параметры подбора) меняет webui:

| | `blockcheck2` (по умолчанию) | `blockcheckS` |
|---|---|---|
| Run | `blockcheck2.sh` через root-helper | `bs scan` (subprocess, повторяемый `-d`) |
| Preflight | GP `check-install` (root-helper) | `check_blockchecks_install` (bs/nfqws2/run.lock) |
| Результаты | маркеры stdout → кандидаты | harvest `strategies.config_path` из per-run `--db` BS |
| Экспорт nfqws2 | нет | `bc-nfconf` (кнопка видна только при blockcheckS) |

Требования для `blockcheckS`:

- `bs` и `bc-nfconf` доступны (`BLOCKCHECKS_BS`/`BLOCKCHECKS_NFCONF`, PATH или
  `/usr/local/libexec/gp-control-plane/{bs,bc-nfconf}`);
- сервис-юзер GP имеет passwordless `sudo -n` (BS сам поднимает netns/nfqws2);
- `ZAPRET_DIR`/`BLOCKCHECKS_ZAPRET2` указывают на дерево zapret2 (по умолчанию
  `/opt/zapret2`);
- контракт вызова и схема результатов: blockcheckS `docs/api.md` §10a и
  `docs/cookbook/gp-bridge.md` (ветка `integration/1.4.1`).

`bs scan` пишет результат в per-run БД `~/.local/state/blockcheckS/bs-runs/` —
кандидаты и экспорт не пересекаются с другими кампаниями BS.

Настройки blockcheckS (finder → Расширенные параметры): `strategy_preset`
(`-M`, gp-verified / flowseal-fast…), `repeats_mode` (fast|stable),
`bs_adaptive` (AQ), TLS-протокол (tls12/tls13), `--debug`/`--skip-ip-block`,
«Тип подбора» TCP | **TCP + UDP/пары** (`bs pair`).
Большие списки (v2fly, >50 доменов) передаются в `bs scan --domains-file` —
единый источник — GP-хранилище пресетов (v2fly-группы тоже domain-нейтральны).

Кандидаты от config-файлов BS разворачиваются в `--lua-desync=` строки
(не .conf-пути). На вкладке «Найденные стратегии» также есть read-only блоки
**DNS-pins / hosts** (domain→IP против hijack из `data_block/providers/*/hosts`)
и **Пары TCP×UDP** (результаты `bs pair`).

### Структура web UI (`src/gp_control_plane/web/ui/`)

Страница собирается серверно из маленьких статичных частей
`web/ui/parts/**` (`.html`/`.css`/`.js`, каждая ≤500 строк) — порядок задан
`parts/__init__.py::PART_ORDER`, сборка — `_assemble.index_html()` (кэш).
Части **не отдаются как статические файлы** (единая инлайн-страница как
раньше, байт-в-байт). Нарезка/перегенерация: `python dev/split_ui.py`.

## Обновление

Повторно запустите bootstrap:

```bash
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-access-control-plane/releases/download/v0.4.0/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.0 bash
```

Это односторонний clean-install маршрут только из exact annotated tag. Ветки, `dev`, cache/candidate routes и rollback не являются пользовательскими способами установки.

## Данные И Бекапы

По умолчанию локальные данные лежат здесь:

```text
~/gp/.GP-access-control-plane.data/state/
```

Файловые бекапы лежат здесь:

```text
~/gp/.GP-access-control-plane.data/backups/
```

При строгом обновлении релиза прежние данные, находившиеся внутри каталога проекта, экспортируются в device-local vault и восстанавливаются только после явного подтверждения.

Откат кода не откатывает пользовательские данные и не отменяет этот перенос. Для возврата данных используйте созданный ранее бекап. Данные остаются на хосте и никуда не публикуются.
