# GP Access Control Plane

Локальный control plane для Raspberry Pi. На текущем этапе проект только читает правила и стратегии, валидирует их, собирает dry-run артефакты, делает проверки доступности с самой Raspberry Pi и показывает состояние через веб-панель.

## Статус MVP

Реализовано:

- чтение репозиториев `GP-traffic-policy-rules` и `GP-zapret-strategy-catalog`;
- локальная валидация YAML/JSON и правил маршрутизации;
- dry-run render в `build/rendered/`;
- прямой healthcheck доменов с Raspberry Pi;
- локальная запись evidence без push;
- проверка наличия `zapret2`/`nfqws2` в системе;
- веб-панель на HTTP-порту.

На этом этапе намеренно не реализовано:

- установка на Keenetic;
- SSH/API/RCI вызовы к Keenetic;
- `apply`, `restart` и любые изменения роутера;
- runtime-интеграция с `sign-craze`.

Если Raspberry Pi выключена, зависла или недоступна, с сетью ничего происходить не должно. Роутер должен продолжать работать на последней успешной конфигурации.

## Подготовка Raspberry Pi

По умолчанию считаем, что на Raspberry Pi уже установлена Raspberry Pi OS/Raspbian и плата подключена к домашней сети.

### 1. Первичная настройка системы

Запустите штатную настройку:

```bash
sudo raspi-config
```

Рекомендуемые пункты:

- включить SSH, если планируете работать с платы удаленно;
- выставить корректный timezone;
- выставить locale, например `en_US.UTF-8` или `ru_RU.UTF-8`;
- при необходимости изменить hostname, например `gp-control-plane`;
- убедиться, что файловая система расширена на всю SD-карту.

После изменений перезагрузите плату:

```bash
sudo reboot
```

После перезагрузки проверьте IP-адрес:

```bash
hostname -I
```

Этот адрес понадобится для веб-панели:

```text
http://RASPBERRY_PI_IP:8080
```

### 2. Обновление системы

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

После перезагрузки:

```bash
sudo apt autoremove -y
```

### 3. Системные пакеты

Установите базовые пакеты:

```bash
sudo apt install -y \
  git \
  curl \
  ca-certificates \
  openssh-client \
  python3 \
  python3-venv \
  python3-pip
```

Проверка:

```bash
python3 --version
git --version
```

Для этого проекта нужен Python `3.11+`. Если `python3 --version` показывает версию ниже `3.11`, лучше обновить Raspberry Pi OS до более свежей версии. Не рекомендуется собирать Python вручную на рабочей плате только ради MVP, потому что это усложнит сопровождение.

### 4. Пользователь и рабочая директория

Дальше в примерах используется текущий пользователь и директория `~/gp`.

Создайте рабочую директорию:

```bash
mkdir -p ~/gp
cd ~/gp
```

Если пользователь не `pi`, это нормально. В дальнейшем в `systemd` unit нужно будет заменить `/home/pi` на путь вашего пользователя.

### 5. Доступ к GitHub

Для приватных репозиториев удобнее использовать SSH-ключ.

Проверьте, есть ли ключ:

```bash
ls -la ~/.ssh
```

Если ключа нет, создайте:

```bash
ssh-keygen -t ed25519 -C "raspberry-pi-gp-control-plane"
```

Показать публичный ключ:

```bash
cat ~/.ssh/id_ed25519.pub
```

Добавьте этот публичный ключ в GitHub:

```text
GitHub -> Settings -> SSH and GPG keys -> New SSH key
```

Проверка доступа:

```bash
ssh -T git@github.com
```

GitHub может ответить, что shell-доступ не предоставляется. Это нормально, если при этом он распознал пользователя.

Минимально настройте Git identity:

```bash
git config --global user.name "your-name"
git config --global user.email "your-email@example.com"
```

### 6. Сеть и порт веб-панели

MVP веб-панель слушает порт `8080`.

Проверить, свободен ли порт:

```bash
ss -ltnp | grep 8080 || true
```

Если на плате включен firewall, разрешите вход на порт `8080` только из локальной сети. Для стандартной Raspberry Pi OS firewall обычно не включен по умолчанию.

Проверить доступ с самой Raspberry Pi после запуска веб-панели:

```bash
curl -I http://127.0.0.1:8080/
```

Эта команда сработает только если web-сервер уже запущен отдельным процессом. Для ручной проверки откройте два SSH-терминала.

В первом терминале:

```bash
cd ~/gp/GP-access-control-plane
PYTHONPATH=src python3 -m gp_control_plane.cli web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Во втором терминале:

```bash
curl -I http://127.0.0.1:8080/
```

Ожидаемый ответ:

```text
HTTP/1.0 200 OK
Content-Type: text/html; charset=utf-8
```

Проверить доступ с компьютера:

```text
http://RASPBERRY_PI_IP:8080
```

### 7. Подготовка под zapret2

Для первого smoke-теста `zapret2` не обязателен. Control plane можно запустить и проверить без него.

На этапе подбора стратегий нужно будет установить `zapret2` так, чтобы команды были доступны пользователю, от которого запущен control plane:

```bash
which nfqws2 || true
which blockcheck2.sh || which blockcheck.sh || true
```

После установки `zapret2` проверка в этом проекте должна вернуть `true` для найденных компонентов:

```bash
gp-control-plane zapret2 check-install --config configs/orchestrator.example.yaml
```

Важно: текущий MVP не меняет маршрутизацию роутера и не запускает apply на Keenetic. Даже при установленном `zapret2` проверки остаются локальными для Raspberry Pi.

## Репозитории

Ожидаемая структура на Raspberry Pi:

```text
~/gp/
  GP-access-control-plane/
  GP-traffic-policy-rules/
  GP-zapret-strategy-catalog/
```

Клонирование:

```bash
mkdir -p ~/gp
cd ~/gp

git clone git@github.com:balbomush/GP-traffic-policy-rules.git
git clone git@github.com:balbomush/GP-zapret-strategy-catalog.git
git clone git@github.com:balbomush/GP-access-control-plane.git
```

Если репозитории приватные, на Raspberry Pi заранее должен быть настроен доступ к GitHub: SSH key или другой выбранный способ авторизации.

## Требования

Минимально нужно:

- Raspberry Pi OS или другой Debian-like Linux;
- Python `3.11+`;
- `git`;
- доступ к трем GitHub-репозиториям.

Проверка:

```bash
python3 --version
git --version
```

`zapret2` на первом smoke-тесте не обязателен. Без него веб-панель и CLI просто покажут, что `nfqws2`/`blockcheck` не найдены.

## Быстрый запуск без установки пакета

Этот способ удобен для первой проверки после копирования или клонирования:

```bash
cd ~/gp/GP-access-control-plane

PYTHONPATH=src python3 -m gp_control_plane.cli validate --config configs/orchestrator.example.yaml
PYTHONPATH=src python3 -m gp_control_plane.cli render --dry-run --config configs/orchestrator.example.yaml
PYTHONPATH=src python3 -m gp_control_plane.cli zapret2 check-install --config configs/orchestrator.example.yaml
```

Ожидаемый результат:

- `validate` возвращает `ok: true`;
- `render --dry-run` создает файлы в `build/rendered/`;
- `zapret2 check-install` показывает, найдены ли `nfqws2` и `blockcheck`.

Запуск веб-панели:

```bash
cd ~/gp/GP-access-control-plane
PYTHONPATH=src python3 -m gp_control_plane.cli web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Открыть с компьютера в той же сети:

```text
http://RASPBERRY_PI_IP:8080
```

Остановить ручной запуск можно через `Ctrl+C`.

## Установка в virtualenv

Для постоянной работы удобнее поставить CLI в виртуальное окружение:

```bash
cd ~/gp/GP-access-control-plane

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

После этого команды можно запускать так:

```bash
gp-control-plane validate --config configs/orchestrator.example.yaml
gp-control-plane render --dry-run --config configs/orchestrator.example.yaml
gp-control-plane zapret2 check-install --config configs/orchestrator.example.yaml
gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

`--config` можно указывать как до команды, так и после команды.

## Локальная конфигурация

Пример общего конфига находится здесь:

```text
configs/orchestrator.example.yaml
```

Он ожидает соседние репозитории:

```yaml
repos:
  rules: ../GP-traffic-policy-rules
  strategies: ../GP-zapret-strategy-catalog
```

Локальные файлы конкретной сети хранятся в `site-local-config/` и не должны попадать в Git:

```text
site-local-config/
  local-overrides.yaml
  devices.yaml
  selected-strategy.yaml
```

Для первого запуска эти файлы можно не создавать.

Если нужно выбрать локальную стратегию, создайте:

```bash
mkdir -p site-local-config
nano site-local-config/selected-strategy.yaml
```

Пример:

```yaml
strategy_path: ../../GP-zapret-strategy-catalog/examples/example-strategy
```

Путь в `selected-strategy.yaml` считается относительно директории `site-local-config/`. Можно указать абсолютный путь.

## Проверка правил и сборка dry-run

Валидация:

```bash
gp-control-plane validate --config configs/orchestrator.example.yaml
```

Dry-run render:

```bash
gp-control-plane render --dry-run --config configs/orchestrator.example.yaml
```

После render должны появиться:

```text
build/rendered/
  routing.json
  dpi-hostlist.txt
  manifest.yaml
  selected-zapret-strategy/   # только если выбрана стратегия
```

Эти файлы пока никуда не применяются. Это локальные артефакты для проверки будущей интеграции.

## Проверка доступности домена

Прямая проверка конкретного домена:

```bash
gp-control-plane healthcheck --direct-only --domain youtube.com --config configs/orchestrator.example.yaml
```

Проверка делает:

- DNS resolve;
- TCP connect на `443`;
- HTTPS `HEAD`.

Результат пишется в:

```text
build/state/healthchecks/
```

Если домен не открывается напрямую, это не считается фатальной ошибкой всего запуска. Результат должен попасть в отчет.

## Проверка zapret2

Проверить, видит ли control plane локальные бинарники:

```bash
gp-control-plane zapret2 check-install --config configs/orchestrator.example.yaml
```

Ожидаемые поля:

```json
{
  "nfqws2_found": true,
  "blockcheck_found": true
}
```

Если значения `false`, нужно установить `zapret2` и убедиться, что `nfqws2` и `blockcheck2.sh` или `blockcheck.sh` доступны в `PATH`.

Список локальных стратегий из каталога:

```bash
gp-control-plane zapret2 list-local --config configs/orchestrator.example.yaml
```

Ручной запуск проверки стратегии:

```bash
gp-control-plane zapret2 run-check \
  --domain youtube.com \
  --strategy ../GP-zapret-strategy-catalog/examples/example-strategy \
  --config configs/orchestrator.example.yaml
```

На текущем MVP это локальный helper. Он не меняет роутер.

## Веб-панель

Запуск:

```bash
gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

В браузере:

```text
http://RASPBERRY_PI_IP:8080
```

В панели доступны:

- статус Raspberry Pi control plane;
- проверка правил;
- pull-only синхронизация Git;
- dry-run сборка;
- прямой healthcheck домена;
- список стратегий zapret;
- запуск локальной проверки стратегии;
- журнал задач и проверок.

## Автозапуск веб-панели через systemd

Опционально, после проверки вручную:

```bash
sudo nano /etc/systemd/system/gp-control-plane-web.service
```

Пример unit-файла:

```ini
[Unit]
Description=GP Access Control Plane Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/gp/GP-access-control-plane
ExecStart=/home/pi/gp/GP-access-control-plane/.venv/bin/gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Если пользователь не `pi`, замените `/home/pi` на свой путь.

Включить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gp-control-plane-web.service
sudo systemctl status gp-control-plane-web.service
```

Логи:

```bash
journalctl -u gp-control-plane-web.service -f
```

Остановить:

```bash
sudo systemctl stop gp-control-plane-web.service
```

## Git sync

Синхронизация только подтягивает изменения:

```bash
gp-control-plane sync --pull-only --config configs/orchestrator.example.yaml
```

MVP не делает push. Если рабочее дерево любого из подключенных репозиториев грязное, sync откажется работать. Проверить:

```bash
git -C ../GP-traffic-policy-rules status --short
git -C ../GP-zapret-strategy-catalog status --short
```

## Evidence без публикации

Локальная запись evidence:

```bash
gp-control-plane evidence write \
  --no-push \
  --rule-id smoke-test \
  --result success \
  --checks 1 \
  --success-rate 1.0 \
  --config configs/orchestrator.example.yaml
```

Файлы пишутся в:

```text
build/evidence/
```

Evidence не должен содержать внешний IP, локальные IP/MAC, город, провайдера в открытом виде, токены или endpoint-данные.

## Локальные тесты

В репозитории:

```bash
cd ~/gp/GP-access-control-plane
python3 -m unittest discover -s tests
```

При установленном virtualenv:

```bash
. .venv/bin/activate
python -m unittest discover -s tests
```

## Быстрый чек-лист после переноса на Raspberry Pi

1. `raspi-config` выполнен, SSH/timezone/locale настроены.
2. Система обновлена через `apt update` и `apt full-upgrade`.
3. Установлены `git`, `python3`, `python3-venv`, `python3-pip`.
4. `python3 --version` показывает `3.11+`.
5. GitHub SSH-доступ проверен через `ssh -T git@github.com`.
6. Репозитории лежат рядом в `~/gp/`.
7. `gp-control-plane validate --config configs/orchestrator.example.yaml` возвращает `ok: true`.
8. `gp-control-plane render --dry-run --config configs/orchestrator.example.yaml` создает `build/rendered/`.
9. `gp-control-plane healthcheck --direct-only --domain youtube.com --config configs/orchestrator.example.yaml` создает отчет.
10. `gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080` открывается с компьютера.
11. `gp-control-plane zapret2 check-install --config configs/orchestrator.example.yaml` показывает реальный статус `zapret2`.

## Troubleshooting

`cd: ~/gp/GP-access-control-plane: No such file or directory`

Это означает, что репозиторий `GP-access-control-plane` не лежит в `~/gp` на Raspberry Pi или называется иначе.

Сначала посмотрите, что реально есть в `~/gp`:

```bash
pwd
ls -la ~/gp
find ~/gp -maxdepth 2 -type d -name "GP-access-control-plane"
```

Если каталога нет, клонируйте репозитории:

```bash
cd ~/gp
git clone git@github.com:balbomush/GP-traffic-policy-rules.git
git clone git@github.com:balbomush/GP-zapret-strategy-catalog.git
git clone git@github.com:balbomush/GP-access-control-plane.git
```

После клонирования проверьте наличие CLI-файла:

```bash
test -f ~/gp/GP-access-control-plane/src/gp_control_plane/cli.py && echo "control plane code exists"
```

Если `git clone` скачал пустой или старый репозиторий, значит актуальные изменения еще не были закоммичены и запушены с рабочей машины в GitHub. Сначала нужно сделать commit/push в `GP-access-control-plane`, а также в репозиториях rules и strategies, если они тоже нужны на Raspberry Pi.

`ModuleNotFoundError: No module named gp_control_plane`

Запустите через `PYTHONPATH=src ...` или установите пакет:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

`sync` отказывается работать из-за dirty repository

Это ожидаемая защита. Проверьте `git status --short` в репозиториях rules и strategies, затем закоммитьте или уберите локальные изменения.

Веб-панель не открывается с компьютера

Проверьте:

```bash
hostname -I
ss -ltnp | grep 8080
```

Сервер должен быть запущен с `--host 0.0.0.0`, а не только `127.0.0.1`.

`curl: (7) Failed to connect to 127.0.0.1 port 8080`

Это означает, что web-сервер control plane не запущен или сразу завершился с ошибкой.

Проверьте, есть ли процесс на порту:

```bash
ss -ltnp | grep 8080 || echo "port 8080 is not listening"
```

Запустите web-сервер в foreground и посмотрите ошибку:

```bash
cd ~/gp/GP-access-control-plane
PYTHONPATH=src python3 -m gp_control_plane.cli web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Если пакет установлен в virtualenv:

```bash
cd ~/gp/GP-access-control-plane
. .venv/bin/activate
gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Если команда сразу завершилась, сначала проверьте базовые команды:

```bash
cd ~/gp/GP-access-control-plane
PYTHONPATH=src python3 -m gp_control_plane.cli validate --config configs/orchestrator.example.yaml
PYTHONPATH=src python3 -m gp_control_plane.cli render --dry-run --config configs/orchestrator.example.yaml
```

Частые причины:

- web-сервер еще не запускали;
- команда запущена не из `~/gp/GP-access-control-plane`;
- не указан `PYTHONPATH=src`, если пакет не установлен через `pip install -e .`;
- репозитории `GP-traffic-policy-rules` и `GP-zapret-strategy-catalog` лежат не рядом с `GP-access-control-plane`;
- порт `8080` уже занят другим процессом.

`nfqws2_found` или `blockcheck_found` равны `false`

Установите `zapret2` и добавьте его бинарники/скрипты в `PATH` пользователя, от которого запускается control plane.

## Безопасность MVP

На текущем этапе control plane не должен содержать и выполнять команды, которые меняют Keenetic. Все команды ограничены Raspberry Pi/local machine и локальными dry-run артефактами.
