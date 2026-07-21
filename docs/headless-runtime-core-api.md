# Headless Runtime / Core API Research

Статус: результат research-этапа. Этот документ фиксирует архитектуру, API-границы и план миграции. Первый кодовый этап добавляет API-only команду `gp-control-plane core`; разделение systemd-сервисов и Web proxy остаются следующими этапами.

Постоянный API-контракт первого уровня находится в [`../openapi.json`](../openapi.json). При изменении API этот файл должен обновляться вместе с кодом.

## Решение По Runtime

Выбран вариант А: штатный Web UI остается доступен пользователю по одному внешнему адресу, а будущий Web service проксирует API-запросы в локальный Core service.

Целевая схема:

```text
Browser / штатный Web UI
        |
        | http://<board>:8080
        v
gp-control-plane-web.service
        |
        | proxy /api/* -> http://127.0.0.1:8081
        v
gp-control-plane-core.service
        |
        | privileged actions
        v
gp-root-helper
```

Headless-сценарий устанавливает только `gp-control-plane-core.service`. Штатный Web UI при этом не удаляется из проекта: пользователь может включить его установкой отдельного Web service или подключить собственный UI к Core API после решения auth-модели.

## Цели

- Сделать `core service` единственным источником истины для логики GP, job-runner, storage, состояния, результатов подбора, backup/restore, диагностики GP и API.
- Оставить штатный Web UI как обычного клиента Core API, а не как владельца продуктовой логики.
- Дать продвинутому пользователю возможность поставить сервис без штатного Web UI и подключить собственный UI/дашборд.
- Не ломать текущие установки и текущую команду `gp-control-plane web` до отдельного решения о миграции.

## Не Цели Research-Этапа

- Не разделять текущий процесс на два systemd-сервиса в коде.
- Не удалять штатный Web UI.
- Не вводить новую auth-модель.
- Не раскрывать Core API наружу без явной настройки пользователя и отдельного решения по авторизации.
- Не делать два разных формата состояния для headless и web.
- Не удалять legacy `/api/*` endpoint'ы без отдельного этапа совместимости.

## Текущее Состояние

Сейчас `gp-control-plane web` запускает единый HTTP-сервер. В нем находятся:

- HTML/CSS/JS штатного интерфейса.
- API `/api/*`.
- Запуск/остановка подбора.
- Чтение storage, backups, candidates, runs, settings, release metadata.
- Вызов `gp-root-helper` для привилегированных операций.

Такой режим остается compatibility-mode до отдельного решения о полном переносе на split-runtime.

## Ответственность Процессов

### Core Service

`gp-control-plane-core.service` владеет продуктовой логикой и состоянием:

- запуск, остановка и прогресс подбора;
- единая нормализация входа/выхода для всех режимов подбора;
- storage, доменные списки, связи домен-стратегия, результаты, кандидаты, история запусков;
- backup/export/import/restore внутренних данных GP;
- run settings, которые реально влияют на подбор;
- нормализованные product events;
- диагностика готовности GP к работе;
- служебные операции GP, которые не являются UI-state.

Core service не должен отдавать готовые UI-карточки, локализованные подсказки, цветовые severity или системный мониторинг платы вроде CPU/RAM/load.

### Web Service

`gp-control-plane-web.service` владеет штатным Web UI:

- отдает статические ресурсы интерфейса;
- проксирует `/api/*` в локальный Core API;
- может иметь `/api/web/...` для UI-оптимизированных срезов данных, если они нужны штатному UI;
- не владеет продуктовой логикой, storage и job-runner.

`/api/web/...` допускается только там, где штатному UI нужен отдельный экранный срез: пагинация, ограниченная выборка, сортировка или формат списка под конкретную вкладку. Если данные являются полным продуктовым фактом, они должны идти через `/api/core/...` или `/api/service/...`.

### Root Helper

`gp-root-helper` остается отдельной привилегированной границей:

- запуск `blockcheck2`/`nfqws2` с нужными правами;
- остановка процессов и cleanup сетевых артефактов;
- установка/обновление/rollback через root-level действия;
- операции, которые нельзя безопасно выполнять из непривилегированного Core process.

Core API вызывает root-helper через явные команды. Web service не должен вызывать root-helper напрямую.

## Network И Bind Model

Целевой default для split-runtime:

- `gp-control-plane-web.service`: `0.0.0.0:8080` или другой явно заданный Web bind/port. Это внешний адрес пользователя.
- `gp-control-plane-core.service`: `127.0.0.1:8081` по умолчанию при установленном Web service.
- Web service проксирует все `/api/*` в Core service.
- Headless/non-interactive install может не ставить Web service. Внешний bind для Core API включается только явной настройкой пользователя и после решения auth-модели.

Причина такого выбора: пользователь продолжает работать по одному адресу платы, CORS не появляется, Web UI не требует знания второго порта, а headless-сценарий остается возможным.

## API Namespace

| Namespace | Смысл |
| --- | --- |
| `/api/auth/...` | Будущая авторизация. В этом research-этапе namespace зарезервирован, механика не решается. |
| `/api/core/...` | Основной функционал продукта и внутренние данные GP. |
| `/api/service/...` | Состояние установленного GP, релизы, service/unit, внешние источники и репозитории. |
| `/api/web/...` | UI-оптимизированные срезы для штатного Web UI. |

Разделение `/api/core/public/...` и `/api/core/private/...` не используется. Граница доступа должна определяться будущей auth/token-моделью, а не названием URL.

Endpoint names должны быть человекочитаемыми. Для action endpoint'ов сохраняется прагматичный POST-action стиль: `POST /save-domain-list`, `POST /delete-user-domain-list`, `POST /check-updates`.

## Core API Контуры

### Strategy Discovery

Основной внешний endpoint запуска:

- `POST /api/core/strategy-discovery/start-run`

Он принимает `mode` и работает как маршрутизатор режима подбора. Отдельные низкоуровневые endpoint'ы для конкретного режима допускаются только когда режим имеет самостоятельный продуктовый смысл для внешнего клиента.

В `start-run` уходят итоговые домены и параметры запуска:

- конкретный массив доменов;
- протоколы;
- timeout values;
- `curl_parallelism`;
- общие run settings;
- mode-specific settings.

В запуск не уходит `preset id`, filter id или живая ссылка на список. Список доменов является только шаблоном заполнения пользовательского поля. История запуска хранит итоговые домены и настройки, но не хранит `source_preset`.

Сопутствующие endpoint'ы:

- `POST /api/core/strategy-discovery/stop-current-run`;
- `GET /api/core/strategy-discovery/current-run-progress`;
- `GET /api/core/strategy-discovery/current-run-latest-log`;
- `GET /api/core/strategy-discovery/preflight`.

### Presets И Domain Lists

Пресеты доменов являются сохраненными доменными списками:

- обязательный список;
- желательный список;
- пользовательские списки.

Будущая замена текущего `GET /api/strategy-finder/domains`:

- `GET /api/core/presets/domain-lists`

Метод возвращает только реально существующие сохраненные списки: первым обязательный, вторым желательный, далее пользовательские списки в порядке хранения. Старые hardcoded discovery-наборы не переносятся.

Операции:

- `POST /api/core/presets/save-domain-list` - сохранить один системный или пользовательский список;
- `POST /api/core/presets/delete-user-domain-list` - удалить один пользовательский список;
- `POST /api/core/presets/delete-user-lists` - удалить все пользовательские списки.

Bulk-save всех пользовательских списков и включение/выключение отдельного домена внутри списка в новую схему не переносятся. Продуктовая модель простая: домен либо входит в список, либо не входит.

### v2fly Domain Helper

`v2fly/domain-list-community` трактуется как помощник для наполнения пользовательских списков, а не отдельная сущность пресетов.

Core read-only методы:

- `GET /api/core/presets/v2fly/categories`;
- `GET /api/core/presets/v2fly/category-domains?category=...`.

Preview/import endpoint'ы не переносятся. UI или внешний клиент читает домены категории, редактирует итоговый набор и сохраняет обычным `POST /api/core/presets/save-domain-list`.

Если локальное хранилище v2fly не готово, эти Core endpoint'ы должны вернуть структурированную ошибку. Отдельный Core readiness endpoint для v2fly не нужен.

### Backups

Backup/export/import/restore внутренних данных GP относится к Core:

- `POST /api/core/backups/create`;
- `GET /api/core/backups/list`;
- `POST /api/core/backups/restore`;
- `POST /api/core/backups/delete`;
- `GET /api/core/backups/download-file`;
- `POST /api/core/backups/upload`.

Отдельный restore-preview endpoint не переносится. Штатный UI перед восстановлением показывает простое окно подтверждения выбранного snapshot.

### Run Settings

Настройки, влияющие на реальный подбор, относятся к Core:

- `GET /api/core/run-settings`;
- `POST /api/core/run-settings/save`.

Минимальный состав:

- `curl_parallelism_default`;
- `curl_parallelism_max`;
- `curl_max_time`;
- `curl_max_time_quic`;
- `curl_max_time_doh`;
- `enable_ipv6`;
- `debug_stdout`.

UI-state вроде выбранной вкладки, раскрытых панелей, фильтров экрана и page size не хранится на сервере.

### Runs, Candidates, Events

Полные продуктовые данные:

- история запусков - `/api/core/runs/...`;
- run logs и latest-log - `/api/core/runs/...`;
- кандидаты общих стратегий - `/api/core/strategy-candidates`;
- нормализованные product events - `/api/core/events`.

Пагинация и экранные срезы для штатного UI относятся к `/api/web/...`.

## Service API Контуры

### Status И Diagnostics

- `GET /api/service/status` отвечает на вопрос: жив ли установленный GP service и в каком состоянии его данные/установка.
- `GET /api/core/status` отвечает на вопрос: что сейчас делает продуктовый контур GP.
- `GET /api/service/diagnostics` отдает нормализованные факты GP: версия, установленный ref/commit, состояние unit/service, готовность внешних источников, check-install, структурированные ошибки, ограниченный GP log tail при необходимости.

Системные метрики платы вроде CPU/RAM/load не входят в GP API.

### Releases

Управление релизами относится к service:

- `GET /api/service/releases/available`;
- `GET /api/service/releases/install-channel`;
- `POST /api/service/releases/set-install-channel`;
- `POST /api/service/releases/install`.

Отдельный update-plan endpoint не переносится. `POST /api/service/releases/install` должен либо запустить установку, либо вернуть структурированную причину отказа без запуска root-helper.

### v2fly Local Storage

Состояние и обновление локального v2fly storage относится к service:

- `GET /api/service/v2fly/local-storage-status`;
- `POST /api/service/v2fly/check-updates`;
- `POST /api/service/v2fly/update-local-storage`.

## Installer Flow

Interactive install:

1. Установщик спрашивает, ставить ли штатный Web UI.
2. Default: установить Web UI.
3. Если Web UI устанавливается, создаются Core service и Web service.
4. Если пользователь выбирает headless, создается только Core service.

Headless/non-interactive install:

1. `GP_INSTALL_WEB=off` отключает Web UI и устанавливает API-only Core service.
2. Установщик не зависает на вопросе в non-interactive режиме.
3. Core API bind по умолчанию остается локальным: `127.0.0.1:8081`.
4. Внешний bind для Core API задается явно через `GP_CORE_HOST` и `GP_CORE_PORT`.

Текущий install command должен продолжать работать. Это означает, что в переходный период установка по умолчанию сохраняет пользовательский опыт `http://<board>:8080/`.

## Systemd Flow

Целевая модель:

- `gp-control-plane-core.service`
  - запускает Core API;
  - владеет job-runner;
  - работает с state-dir;
  - вызывает root-helper;
  - пишет core/runtime логи.

- `gp-control-plane-web.service`
  - зависит от Core service;
  - отдает штатный UI;
  - проксирует `/api/*` в Core;
  - может быть не установлен в headless-сценарии.

Compatibility-mode:

- текущий `gp-control-plane-web.service` и команда `gp-control-plane web` остаются рабочими;
- старый единый web-режим не удаляется без отдельного решения;
- в первом implementation-этапе новые endpoint'ы можно добавить в текущий web process, чтобы не делать service split и API rename одновременно.

## Migration И Rollback

Безопасная последовательность внедрения после research:

1. Добавить OpenAPI validation в локальные проверки.
2. Добавить новые `/api/core/...`, `/api/service/...`, `/api/web/...` endpoint'ы в текущий единый process, не удаляя старые URL.
3. Перевести штатный Web UI на новые endpoint'ы.
4. Зафиксировать compatibility layer для legacy URL или явно согласовать его удаление.
5. Вынести Core API в отдельный внутренний server module.
6. Добавить `gp-control-plane core` как отдельную CLI-команду.
7. Добавить Web service как статический UI/proxy поверх Core.
8. Обновить installer и systemd units с default-установкой Web UI и explicit headless mode.

Rollback:

- перед миграцией installer создает backup текущего состояния;
- state-dir остается единым и совместимым;
- старый `gp-control-plane web` продолжает запускаться на том же state-dir;
- rollback возвращает прежний service unit и установленный ref/tag;
- backup restore остается через Core storage model.

## Compatibility Decisions

Зафиксировано:

- текущий `gp-control-plane web` остается рабочим;
- текущая команда установки остается рабочей;
- Web UI по умолчанию остается доступен на одном внешнем адресе;
- постоянный API-контракт - `openapi.json`;
- `api_inventory.md` является временным черновиком исследования.

Не решено в этом research-блоке:

- точная auth/token/password модель;
- время жизни токена;
- список endpoint'ов, доступных без авторизации;
- сроки удаления legacy URL;
- внешнее раскрытие Core API в LAN.

Эти решения должны попасть в отдельные planned/ordinary-блоки перед production-разделением сервисов.

## Минимальная Проверка Перед Переносом В Ordinary

Перед тем как переносить split-runtime из research в обычную разработку, нужно иметь:

- валидный `openapi.json`;
- список endpoint'ов первого implementation-этапа;
- решение по compatibility layer старых URL;
- план тестов для API aliasing;
- installer/systemd сценарии для default Web UI и headless install;
- rollback сценарий без потери state-dir;
- оценку нагрузки на слабой плате только как архитектурную проверку, без релизного использования feature-ветки на контрольной плате.
