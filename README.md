# GP Access Control Plane

Веб-интерфейс для Raspberry Pi, который помогает подбирать рабочие стратегии `zapret2` через `blockcheck2.sh`.

## Установка

Самый простой вариант для Raspberry Pi OS:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/v0.3.2/scripts/install-raspberry-pi.sh | bash
```

Что сделает скрипт:

- обновит систему через `apt`;
- установит нужные пакеты: `git`, `python3`, `venv`, `curl`, `nftables`, `dnsutils` и другие;
- установит `zapret2` в `/opt/zapret2`;
- скачает этот проект в `~/gp/GP-access-control-plane`;
- создаст Python-окружение;
- установит команду `gp-control-plane`;
- подготовит локальный каталог групп `v2fly/domain-list-community` для импорта доменных списков без live-запросов из web UI;
- установит root-helper для запуска `blockcheck2` без интерактивного sudo-пароля;
- создаст и включит systemd-сервис;
- запустит веб-интерфейс автоматически сейчас и при каждой загрузке Raspberry Pi.

Установка рассчитана на Raspberry Pi OS. Скрипт можно запускать из-под любого пользователя с правом `sudo`.

После установки откройте в браузере:

```text
http://<ip-raspberry-pi>:8080/
```

По умолчанию проект ставится в домашний каталог пользователя, от имени которого запущена установка.

Если запустить через `sudo`, установщик возьмет исходного пользователя из `SUDO_USER` и поставит проект ему, а не в `/root`:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/v0.3.2/scripts/install-raspberry-pi.sh | sudo bash
```

Если нужно явно выбрать пользователя:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/v0.3.2/scripts/install-raspberry-pi.sh | sudo env GP_INSTALL_USER=pi bash
```

Путь установки можно поменять через `GP_INSTALL_DIR`, но выбранный пользователь должен иметь право записи в этот каталог. Для обычной установки лучше оставить путь по умолчанию: `~/gp/GP-access-control-plane`.

Установщик добавляет systemd-ограничители памяти для web-сервиса:

```text
MemoryHigh=512M
MemoryMax=1G
```

Если для вашей платы нужны другие значения, задайте их при установке:

```bash
GP_SERVICE_MEMORY_HIGH=768M GP_SERVICE_MEMORY_MAX=1500M curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/v0.3.2/scripts/install-raspberry-pi.sh | bash
```

Если репозиторий приватный, сначала настройте SSH-доступ к GitHub. Затем запустите установку через `git clone`:

```bash
GP_REPO_URL=git@github.com:balbomush/GP-access-control-plane.git bash -lc 'SUDO=sudo; [ "$(id -u)" -eq 0 ] && SUDO=; $SUDO apt-get update && $SUDO apt-get install -y git && tmp="$(mktemp -d)" && git clone "$GP_REPO_URL" "$tmp" && bash "$tmp/scripts/install-raspberry-pi.sh"'
```

## Установка zapret2 отдельно

Полный установщик выше уже ставит `zapret2` автоматически. Если нужно установить только `zapret2` без установки веб-интерфейса, выполните:

```bash
bash -lc 'SUDO=sudo; [ "$(id -u)" -eq 0 ] && SUDO=; $SUDO apt-get update && $SUDO apt-get install -y git bsdextrautils && if [ -d /opt/zapret2/.git ]; then $SUDO git -C /opt/zapret2 pull --ff-only; else $SUDO git clone https://github.com/bol-van/zapret2.git /opt/zapret2; fi && cd /opt/zapret2 && $SUDO ./install_bin.sh'
```

После этого должны появиться файлы:

```text
/opt/zapret2/blockcheck2.sh
/opt/zapret2/nfq2/nfqws2
```

## Проверка zapret2

Установщик сам скачивает `zapret2` из `https://github.com/bol-van/zapret2.git` и кладет его в:

```text
/opt/zapret2
```

Также он создает wrappers в `~/.local/bin`, чтобы системе были доступны команды:

```bash
blockcheck2.sh
nfqws2
```

Проверка после установки:

```bash
gp-control-plane zapret2 check-install --config ~/gp/GP-access-control-plane/configs/orchestrator.example.yaml
```

В выводе должны быть `root_helper_found: true` и `root_helper_ready: true`. Это важно: подбор запускается из web-сервиса без терминала, поэтому он не может вводить sudo-пароль. Установщик решает это через отдельный root-helper:

```text
/usr/local/libexec/gp-control-plane/gp-root-helper
/etc/sudoers.d/gp-control-plane-root-helper
```

Проверка сценария после истечения sudo-сессии:

```bash
sudo -k
curl -I http://127.0.0.1:8080/
```

После этого запуск подбора из web UI должен стартовать без ошибки `sudo: a terminal is required`.

## Что умеет текущая версия

- запускать веб-интерфейс на Raspberry Pi;
- запускать обычный подбор стратегий через штатный `blockcheck2.sh`;
- запускать экспериментальный режим, где одна стратегия проверяется сразу на нескольких доменах;
- ограничивать количество параллельных `curl`;
- включать и выключать проверки HTTP, TLS 1.2, TLS 1.3, HTTP3/QUIC;
- использовать встроенные пресеты доменов: критичные, покрытие, Google/YouTube, Discord, Cloudflare, Amazon/AWS;
- воспринимать сервисные пресеты как публично известный проверяемый набор доменов, а не как гарантию полного покрытия сервиса;
- показывать прогресс, live-лог и историю запусков;
- сохранять найденные стратегии в локальную SQLite-БД;
- показывать стратегии по доменам и общие стратегии для выбранных доменов;
- быстро подгружать большие списки кандидатов частями, без полной загрузки всего списка в браузер;
- останавливать долгий подбор без потери уже найденных успешных стратегий;
- хранить пользовательские пресеты доменов на backend, а не только в браузере;
- создавать файловые бекапы доменов, стратегий и связей стратегия-домен;
- скачивать бекапы через отдельную вкладку `Бекапы`;
- перед восстановлением показывать, какие данные будут заменены;
- восстанавливать стратегии и связи стратегия-домен из бекапа, когда подбор не запущен.

Проект не меняет настройки роутера и не применяет стратегии автоматически.

## Как пользоваться

1. Откройте веб-интерфейс: `http://<ip-raspberry-pi>:8080/`.
2. Во вкладке `Подбор` выберите домены.
3. Нажмите обычный поиск или экспериментальный поиск.
4. Во вкладке `Терминал` смотрите ход работы.
5. Во вкладке `Кандидаты` смотрите найденные стратегии.
6. Во вкладке `Бекапы` скачайте архив или восстановите стратегии из бекапа, если нужно откатиться.
7. Скопируйте подходящую стратегию вручную и проверьте ее там, где планируете использовать.

Подбор может длиться несколько часов. Кнопка остановки сохраняет найденные к этому моменту стратегии.

## Управление сервисом

Проверить состояние:

```bash
sudo systemctl status gp-control-plane-web.service
```

Перезапустить:

```bash
sudo systemctl restart gp-control-plane-web.service
```

Остановить:

```bash
sudo systemctl stop gp-control-plane-web.service
```

Посмотреть логи сервиса:

```bash
journalctl -u gp-control-plane-web.service -f
```

## Обновление

Повторно запустите установщик:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/v0.3.2/scripts/install-raspberry-pi.sh | bash
```

Он установит текущий стабильный релиз `v0.3.2`, обновит Python-окружение и перезапустит сервис. Если нужно явно поставить другую ветку или тег, задайте `GP_BRANCH`, например `GP_BRANCH=main`.

Тестовые alpha/prerelease-сборки не ставятся этой командой. Для них используйте вкладку `Настройки` в web UI:

1. Выберите канал `Предрелизы`.
2. Проверьте доступную версию.
3. Запустите обновление только если понимаете, что это тестовая сборка.

Перед обновлением web UI создает pre-update бекап. После установки он показывает статус из update-log: поставлено в очередь, идет установка, успешно или ошибка. Если проверка версии не прошла, восстановите pre-update бекап во вкладке `Бекапы`.

## Где лежат данные

Проект хранит локальные данные здесь:

```text
~/gp/GP-access-control-plane/build/state/
```

Основное рабочее хранилище:

- `strategy-finder/state.sqlite3` - SQLite-БД со стратегиями, связями стратегия-домен, историей запусков и пользовательскими пресетами;
- `strategy-finder/logs/` - stdout/stderr/progress логи `blockcheck2`.

Внутри SQLite данные разделены на сущности:

- `domains` - домены;
- `strategies` - уникальные стратегии;
- `strategy_domain_results` - результат "стратегия работает на домене";
- `strategy_attempts` - техническая таблица совместимости/диагностики; новые успешные связи пишутся не сюда, а в `strategy_domain_results`;
- `domain_presets` и `preset_domains` - пользовательские пресеты доменов;
- `runs` - история запусков.

Списки кандидатов, стратегии домена и общие стратегии считаются SQL-запросами по этим таблицам, без полного обхода всех стратегий в Python.

Старые файлы `strategy-finder/candidates.json`, `strategy-finder/runs.jsonl`, `strategy-finder/available.ndjson` могут остаться после обновления. При первом чтении они импортируются в SQLite для совместимости, после чего legacy-файлы и старые строки попыток очищаются, а SQLite сжимается при необходимости.

Логи подбора в `strategy-finder/logs/` ротируются: активные stdout/debug-файлы ограничены по размеру, а старые крупные runtime-логи удаляются перед новым запуском по лимиту количества и суммарного размера.

Файловые бекапы лежат отдельно:

```text
~/gp/GP-access-control-plane/build/backups/
```

Структура сохранений:

```text
build/backups/
  latest.txt
  snapshots/
    <date>/
      manifest.yaml
      checksums.sha256
      domains/
      strategies/
  archives/
```

Бекап содержит домены, стратегии и связи стратегия-домен. Пользовательские списки и настройки не заменяются при восстановлении.

Хранятся последние 5 успешных snapshot-ов. Более старые удаляются автоматически только после успешного создания новой копии. Snapshot создается только в простое, когда подбор не запущен.

## Ручное восстановление из архива

Обычный способ - вкладка `Бекапы`. Если web UI недоступен, восстановить архив можно из терминала:

```bash
cd ~/gp/GP-access-control-plane
. .venv/bin/activate
sudo systemctl stop gp-control-plane-web.service
python - <<'PY'
from pathlib import Path
from gp_control_plane.backups import import_snapshot_archive, restore_snapshot, restore_snapshot_preview
from gp_control_plane.config import load_config

config = load_config(Path("configs/orchestrator.example.yaml"))
archive = Path("/path/to/backup.zip")
snapshot = import_snapshot_archive(config.output.state_dir, archive.read_bytes())["snapshot"]["id"]
print(restore_snapshot_preview(config.output.state_dir, snapshot))
restore_snapshot(config.output.state_dir, snapshot)
PY
sudo systemctl start gp-control-plane-web.service
```

Перед restore автоматически создается pre-restore бекап текущего состояния. Восстановление заменяет домены со стратегиями, стратегии и связи стратегия-домен. Пользовательские списки и настройки остаются текущими.

Эти данные остаются на Raspberry Pi и никуда не публикуются.

## Ручная установка

Если не хотите запускать установщик одной командой:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git python3 python3-venv python3-pip curl nftables iproute2 iptables ipset dnsutils ca-certificates

mkdir -p ~/gp
cd ~/gp
git clone https://github.com/balbomush/GP-access-control-plane.git
cd GP-access-control-plane

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Ручная установка выше не показывает все шаги установки `zapret2` и автозапуска. Для обычного использования проще и надежнее запускать `scripts/install-raspberry-pi.sh`.

## Тесты для разработчика

```bash
cd ~/gp/GP-access-control-plane
. .venv/bin/activate
python -m unittest discover -s tests
```
