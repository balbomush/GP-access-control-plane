# testing.md — как тестировать и гонять гейты

Документ описывает тестовые слои, инструменты аудита и правила качества для
репозитория GP-access-control-plane. См. также `README.md` (структура кода) и
`dev/gate_all.sh` (единый гейт).

## Окружение (dev-установка)

```bash
# venv + установка пакета в editable + ВСЕ dev/test-инструменты
bash scripts/dev-install.sh

# JS/CSS-линтер (web/ui/parts — реальные .js/.css файлы)
npm install            # ставит @biomejs/biome
```

`requirements-dev.txt` содержит все тест/аудит-инструменты
(pytest, pytest-timeout, pytest-cov, coverage, ruff, vulture, pyright).
Продакшн-зависимостей у пакета нет (`[project].dependencies` пуст), поэтому
runtime-`requirements.txt` не существует.

## Слои тестов (маркеры pytest)

Всё задано в `pyproject.toml → [tool.pytest.ini_options]`; дефолт запускает
**unit** (всё, что не отмечено `integration`/`quality`):

| Маркер | Что это | Команда |
|---|---|---|
| `unit` (дефолт) | headless-тесты без root и внешних сервисов | `pytest` / `pytest tests/ -q` |
| `quality` | структурные/AST-гварды: лимит строк, ui-parts, split-runtime, cli-safety | `pytest -m quality` |
| `integration` | root/sudo/привилегированные процессы/внешние сервисы (zapret2 root-helper, clean-install-vault, privileged-сабтесты strategy_finder/web_ui, Edge-E2E) | `sudo -n pytest -m integration --timeout=600` |

Правила:
- default `addopts = -m "not integration and not quality"` — обычный прогон
  всегда чистый и не требует привилегий.
- Интеграционные тесты, запущенные без root/сервисов, **не** входят в дефолт и
  в CI/гейт; при желании гоняются под sudo отдельно.
- Edge-E2E в `test_ui_auth.py` дополнительно `SkipTest`, если Edge-headless не
  установлен.

## Инструменты аудита (гейт)

Единый гейт: `bash dev/gate_all.sh` (см. ниже порядок и «жёсткость»).

| Инструмент | Что проверяет | Гейт |
|---|---|---|
| `pytest` (unit) | функциональные headless-тесты | жёсткий |
| `pytest -m quality` | структурные гварды | жёсткий |
| `ruff` | выбранный набор: E4/E9, F, I, UP, B (см. pyproject `[tool.ruff.lint]`) | жёсткий |
| `vulture` | мёртвый код (`[tool.vulture]`, paths=src) | жёсткий |
| `pyright` | статик-тайп-чек (`pyrightconfig.json`) | **мягкий** (best-effort; не останавливает гейт) |
| `biome` | lint/parse JS/CSS в `web/ui/parts` | жёсткий (если установлен) |
| `coverage` | покрытие по unit-набору, `fail_under = 70` | жёсткий |

Замечания:
- ruff-набор намеренно исключает низкоинформативный легаси-стиль
  (ARG/RET/SIM, pycodestyle E7 «несколько операторов на строке»/E741) — см.
  комментарий в pyproject.
- pyright: в коде много `dict`-payload'ов, поэтому часть категорий
  (`reportAttributeAccessIssue`, `reportOptional*`) понижена до warning в
  `pyrightconfig.json`. Если pyright не установлен (нет сети) — гейт пишет
  «skip», не падает.

## Правила кода (аудиторные)

- **Нет тихих подмен (silent fallback)**: любой `except`, который «проглатывает»
  ошибку, обязан логировать причину (`log.warning/debug` с контекстом), либо
  явно `raise … from`. Проверка: grep-скан «except → pass» должен давать 0.
- **Нет мёртвого кода**: `vulture --config pyproject.toml` чист (unused-параметры
  интерфейсов либо `_`-префикс, либо явный комментарий «legacy»).
- **Лимит размера**: ни один `.py` в `src/` не превышает 500 физических строк —
  гвард `tests/test_src_line_limit.py` (маркер `quality`).
- **Слои импортов**: `engine_common` не импортирует `bc2_engine`/`bs_engine`;
  движки импортируют `engine_common`. Проверяется import-smoke в гейте.
- **JS/CSS не менять семантику**: правки в `web/ui/parts` не должны ломать
  сборку страницы (guard-тесты `test_ui_parts.py`; sha страницы фиксирован).

## Как добавить тест

1. Новый файл `tests/test_<что>.py` — `unittest.TestCase`, без pytest-маркера
   (unit по умолчанию). При желании маркер на класс/метод.
2. Импорты по новым пакетам (`gp_control_plane.engine_common`,
   `gp_control_plane.bc2_engine`, `gp_control_plane.storage`, …).
3. Если тест требует root/внешний сервис — пометить
   `@pytest.mark.integration` (метод) или `pytestmark` (модуль) + импортировать
   `pytest`; не включать в unit-зависимость гейта.
4. Если это структурный/статический guard — маркер `quality`.

## Покрытие

- `bash dev/gate_all.sh` переснимает coverage по unit-набору; порог в
  `[tool.coverage.report].fail_under` = 70 (baseline 75%, 2026-09-04).
- Локально: `coverage run -m pytest tests/ -q && coverage report -m`.

## Troubleshooting

- `integration` тесты падают без sudo — это ожидаемо; гоняйте под sudo или
  не гоняйте вовсе (в дефолт не входят).
- Edge-E2E: без установленного `msedge`/chromium — `SkipTest`; если браузер
  есть, убедитесь в PATH.
- `pyright` не установлен (офлайн): `pip install pyright` (см.
  requirements-dev.txt) или пропуск гейта (soft).
- `biome` не установлен: `npm install`; без node — гейт пишет skip.
- Страница «поехала» после правок JS/CSS: сравнить `sha256` собранной страницы
  (`test_ui_parts`) и при необходимости перегенерировать части
  `python dev/split_ui.py`.
