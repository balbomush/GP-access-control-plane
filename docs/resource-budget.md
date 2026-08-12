# Resource Budget

Инженерный бюджет для Raspberry Pi 2. Цель - не вводить изменения, которые незаметно раздувают память, сетевые буферы или параллелизм слабой платы.

## Runtime

| Показатель | Бюджет | Статус |
|---|---:|---|
| Core service RSS | до 180 MiB | требует фактического замера на main/release gate |
| Web proxy RSS | до 120 MiB | требует фактического замера на main/release gate |
| Core + Web proxy RSS суммарно | до 300 MiB | требует фактического замера на main/release gate |

Feature-ветки не проверяются на Raspberry Pi 2. Фактический замер RSS выполняется только после попадания изменений в main/release-candidate.

## Ручные проверки на платах

Gates запускаются оператором на реальной плате после установки целевого тега. Они не выполняют установку, reimage или сброс данных. Пароль передавайте через переменную окружения, а не как часть команды из истории shell:

```bash
read -rsp 'Пароль API: ' GP_GATE_PASSWORD; echo
```

### Raspberry Pi 2

Pi2 gate измеряет RSS Core, Web и их сумму. Чистая установка/reimage остаётся ручной предпосылкой:

```bash
sudo GP_GATE_PASSWORD="$GP_GATE_PASSWORD" bash scripts/release-gates/pi2-gate.sh --ref vX.Y.Z
unset GP_GATE_PASSWORD
```

Обязателен `--ref` с существующим тегом релиза; `--mode installed` используется по умолчанию. `--mode dirty-update` проверяет поддерживаемое строгое обновление с собственным временным dirty marker. Дополнительные безопасные настройки: `--password-env NAME`, `--base-url URL`, `--core-url URL`, `--state-dir PATH` и `--poll-timeout SECONDS`. Без `--state-dir` gate читает `GP_STATE_DIR` из root-owned `0600` `/etc/default/gp-control-plane-install-profile`; для `v0.4.0` и новее отсутствие или небезопасный профиль останавливает gate.

### Raspberry Pi 5

Pi5 gate функционально проверяет установленную топологию, циклы `standard`/`multi_domain` и отсутствие оставшихся процессов. Укажите фактическую топологию:

```bash
sudo --preserve-env=GP_GATE_PASSWORD bash scripts/release-gates/pi5-gate.sh \
  --ref vX.Y.Z --topology web --mode installed
unset GP_GATE_PASSWORD
```

`--topology web|headless` и `--ref` обязательны. Помимо `installed`, режим `dirty-update` проверяет строгое обновление. Режим `clean-install` только проверяет результат ручной чистой установки: запускайте его лишь с `--mode clean-install --ack-clean-install`; сам gate не устанавливает и не переустанавливает систему.

Оба gate сохраняют JSONL-отчёты и логи в `/var/lib/gp-control-plane/release-gates`. Пароль и bearer token не записываются в отчёты и не передаются в аргументах `curl`.

## Backup And Streaming

| Показатель | Бюджет | Где задан |
|---|---:|---|
| Максимальный JSON request body | 1 MiB | `resource_budget.JSON_REQUEST_MAX_BYTES` |
| Максимальный upload backup | 64 MiB | `resource_budget.BACKUP_UPLOAD_MAX_BYTES` |
| Chunk чтения backup/download/checksum | 256 KiB | `resource_budget.BACKUP_STREAM_CHUNK_BYTES` |
| Chunk proxy streaming | 64 KiB | `resource_budget.PROXY_STREAM_CHUNK_BYTES` |

Обычные JSON API-запросы ограничены отдельно от backup upload: это защищает основной сервис от больших случайных payload. Текущий upload backup остается memory-backed, поэтому верхний лимит снижен с 512 MiB до 64 MiB. Потоковый upload через временный файл - отдельная будущая доработка, если backup начнут приближаться к этому лимиту.

## Diagnostics

| Показатель | Бюджет | Статус |
|---|---:|---|
| Diagnostics response | до 256 KiB | контролировать при расширении diagnostics |
| Host CPU/RAM/load в diagnostics | запрещено | внешняя диагностика должна собираться внешними средствами |

Diagnostics API должен возвращать факты о GP-сервисе и его данных, а не метрики всей системы.

## Strategy Discovery

| Показатель | Бюджет | Статус |
|---|---:|---|
| Pi2-safe recommended `curl_parallelism_max` | 10 | дефолт настроек запуска |

Это не жесткий верхний предел: пользователь может поднять максимум через настройки. До фактических замеров на Raspberry Pi 2 дефолт должен оставаться 10.
