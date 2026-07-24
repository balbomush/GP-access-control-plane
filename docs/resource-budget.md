# Resource Budget

Инженерный бюджет для Raspberry Pi 2. Цель - не вводить изменения, которые незаметно раздувают память, сетевые буферы или параллелизм слабой платы.

## Runtime

| Показатель | Бюджет | Статус |
|---|---:|---|
| Core service RSS | до 180 MiB | требует фактического замера на main/release gate |
| Web proxy RSS | до 120 MiB | требует фактического замера на main/release gate |
| Core + Web proxy RSS суммарно | до 300 MiB | требует фактического замера на main/release gate |

Feature-ветки не проверяются на Raspberry Pi 2. Фактический замер RSS выполняется только после попадания изменений в main/release-candidate.

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
