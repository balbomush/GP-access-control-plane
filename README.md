# GP Access Control Plane

Веб-интерфейс для Raspberry Pi, который помогает подбирать рабочие стратегии `zapret2` через `blockcheck2.sh`.

## Установка

Самый простой вариант для Raspberry Pi OS:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/main/scripts/install-raspberry-pi.sh | bash
```

Что сделает скрипт:

- обновит систему через `apt`;
- установит нужные пакеты: `git`, `python3`, `venv`, `curl`, `nftables`, `dnsutils` и другие;
- установит `zapret2` в `/opt/zapret2`;
- скачает этот проект в `~/gp/GP-access-control-plane`;
- создаст Python-окружение;
- установит команду `gp-control-plane`;
- создаст и включит systemd-сервис;
- запустит веб-интерфейс автоматически сейчас и при каждой загрузке Raspberry Pi.

После установки откройте в браузере:

```text
http://<ip-raspberry-pi>:8080/
```

Скрипт можно запускать из-под любого пользователя с правом `sudo`. По умолчанию проект ставится в домашний каталог пользователя, от имени которого запущена установка.

Если запустить через `sudo`, установщик возьмет исходного пользователя из `SUDO_USER` и поставит проект ему, а не в `/root`:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/main/scripts/install-raspberry-pi.sh | sudo bash
```

Если нужно явно выбрать пользователя:

```bash
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/main/scripts/install-raspberry-pi.sh | sudo env GP_INSTALL_USER=pi bash
```

Путь установки можно поменять через `GP_INSTALL_DIR`, но выбранный пользователь должен иметь право записи в этот каталог. Для обычной установки лучше оставить путь по умолчанию: `~/gp/GP-access-control-plane`.

Если репозиторий приватный, сначала настройте SSH-доступ к GitHub. Затем запустите установку через `git clone`:

```bash
GP_REPO_URL=git@github.com:balbomush/GP-access-control-plane.git bash -lc 'SUDO=sudo; [ "$(id -u)" -eq 0 ] && SUDO=; $SUDO apt-get update && $SUDO apt-get install -y git && tmp="$(mktemp -d)" && git clone "$GP_REPO_URL" "$tmp" && bash "$tmp/scripts/install-raspberry-pi.sh"'
```

## Установка zapret2 отдельно

Полный установщик выше уже ставит `zapret2` автоматически. Если нужно установить только `zapret2` без установки веб-интерфейса, выполните:

```bash
bash -lc 'SUDO=sudo; [ "$(id -u)" -eq 0 ] && SUDO=; $SUDO apt-get update && $SUDO apt-get install -y git bsdextrautils && if [ -d /opt/zapret2/.git ]; then $SUDO git -C /opt/zapret2 pull --ff-only; else $SUDO git clone https://github.com/bol-van/zapret2.git /opt/zapret2; fi && $SUDO /opt/zapret2/install_bin.sh'
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

## Что умеет текущая версия

- запускать веб-интерфейс на Raspberry Pi;
- запускать обычный подбор стратегий через штатный `blockcheck2.sh`;
- запускать экспериментальный режим, где одна стратегия проверяется сразу на нескольких доменах;
- ограничивать количество параллельных `curl`;
- включать и выключать проверки HTTP, TLS 1.2, TLS 1.3, HTTP3/QUIC;
- использовать встроенные пресеты доменов: критичные, покрытие, диагностика, Google/YouTube, Discord, Cloudflare, Amazon/AWS;
- показывать прогресс, live-лог и историю запусков;
- сохранять найденные стратегии в локальную SQLite-БД;
- показывать стратегии по доменам и общие стратегии для выбранных доменов;
- останавливать долгий подбор без потери уже найденных успешных стратегий;
- хранить пользовательские пресеты доменов на backend, а не только в браузере;
- создавать файловые сохранения стратегий и пресетов;
- скачивать бекапы через отдельную вкладку `Бекапы`.
- восстанавливать стратегии и пользовательские пресеты из бекапа, когда подбор не запущен.

Проект не меняет настройки роутера и не применяет стратегии автоматически.

## Как пользоваться

1. Откройте веб-интерфейс: `http://<ip-raspberry-pi>:8080/`.
2. Во вкладке `Подбор` выберите домены.
3. Нажмите обычный поиск или экспериментальный поиск.
4. Во вкладке `Терминал` смотрите ход работы.
5. Во вкладке `Кандидаты` смотрите найденные стратегии.
6. Во вкладке `Бекапы` скачайте архив или восстановите состояние из бекапа, если нужно откатиться.
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
curl -fsSL https://raw.githubusercontent.com/balbomush/GP-access-control-plane/main/scripts/install-raspberry-pi.sh | bash
```

Он подтянет свежий `main`, обновит Python-окружение и перезапустит сервис.

## Где лежат данные

Проект хранит локальные данные здесь:

```text
~/gp/GP-access-control-plane/build/state/
```

Основное рабочее хранилище:

- `strategy-finder/state.sqlite3` - SQLite-БД со стратегиями, связями стратегия-домен, историей запусков и пользовательскими пресетами;
- `strategy-finder/logs/` - stdout/stderr/progress логи `blockcheck2`.

Старые файлы `strategy-finder/candidates.json`, `strategy-finder/runs.jsonl`, `strategy-finder/available.ndjson` могут остаться после обновления. При первом чтении они импортируются в SQLite для совместимости.

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
      strategies/
      presets/
      settings/
  archives/
```

Хранятся последние 5 успешных snapshot-ов. Более старые удаляются автоматически только после успешного создания новой копии. Snapshot создается только в простое, когда подбор не запущен.

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
