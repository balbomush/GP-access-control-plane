# GP Access Control Plane

Текущий этап проекта: Raspberry Pi web UI для подбора рабочих стратегий `zapret2` через штатный `blockcheck2.sh`.

Что реализовано сейчас:

- запуск веб-интерфейса на Raspberry Pi;
- запуск обычного `blockcheck2` подбора по доменам;
- экспериментальный режим `стратегия -> домены`, где одна стратегия проверяется параллельными `curl` по нескольким доменам;
- настройка реально влияющих параметров `blockcheck2`;
- остановка долгого подбора с сохранением уже найденных стратегий;
- просмотр кандидатов, общих стратегий, истории запусков и live-лога;
- хранение локального состояния в `build/state`.

Что не реализуется в этой ветке:

- установка или изменение настроек роутера;
- автоматическая публикация результатов;
- синхронизация правил маршрутизации;
- генерация конфигов для других компонентов.

Критерий приемки текущего этапа: открыть web UI, запустить подбор, найти стратегию, вручную скопировать ее в целевую систему и вручную проверить, что она работает.

## Подготовка Raspberry Pi

Предполагается чистая Raspberry Pi OS.

1. Обновить систему:

```bash
sudo apt update
sudo apt upgrade -y
```

2. Поставить базовые пакеты:

```bash
sudo apt install -y git python3 python3-venv python3-pip curl nftables iproute2 dnsutils
```

3. Установить и проверить `zapret2`.

В текущей реализации ожидается, что в `PATH` доступны:

```bash
nfqws2
blockcheck2.sh
```

Проверка:

```bash
command -v nfqws2
command -v blockcheck2.sh
```

Если `zapret2` установлен в `/opt/zapret2`, но команды не находятся, добавьте wrappers в `~/.local/bin`:

```bash
mkdir -p ~/.local/bin

cat > ~/.local/bin/blockcheck2.sh <<'EOF'
#!/bin/sh
exec /opt/zapret2/blockcheck2.sh "$@"
EOF

cat > ~/.local/bin/nfqws2 <<'EOF'
#!/bin/sh
exec /opt/zapret2/nfq2/nfqws2 "$@"
EOF

chmod +x ~/.local/bin/blockcheck2.sh ~/.local/bin/nfqws2
```

Добавьте `~/.local/bin` в `PATH`, если его там нет:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
. ~/.profile
```

## Установка проекта

```bash
mkdir -p ~/gp
cd ~/gp
git clone git@github.com:balbomush/GP-access-control-plane.git
cd GP-access-control-plane
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Проверка команд:

```bash
gp-control-plane zapret2 check-install --config configs/orchestrator.example.yaml
gp-control-plane strategy-finder domains --config configs/orchestrator.example.yaml
```

## Запуск web UI

Ручной запуск:

```bash
cd ~/gp/GP-access-control-plane
. .venv/bin/activate
gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
```

Открыть с компьютера:

```text
http://<ip-raspberry-pi>:8080/
```

Проверка с самой Raspberry Pi:

```bash
curl -I http://127.0.0.1:8080/
```

## Автозапуск через systemd

Создать service:

```bash
sudo tee /etc/systemd/system/gp-control-plane-web.service >/dev/null <<'EOF'
[Unit]
Description=GP Strategy Finder Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
WorkingDirectory=/home/%i/gp/GP-access-control-plane
Environment=PATH=/home/%i/gp/GP-access-control-plane/.venv/bin:/home/%i/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/%i/gp/GP-access-control-plane/.venv/bin/gp-control-plane web --config configs/orchestrator.example.yaml --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Включить сервис для текущего пользователя:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gp-control-plane-web@$USER.service
sudo systemctl status gp-control-plane-web@$USER.service
```

Логи:

```bash
journalctl -u gp-control-plane-web@$USER.service -f
```

## Подбор стратегий

В web UI доступны два режима:

- обычный поиск: штатный порядок `blockcheck2`, домены проверяются по очереди;
- эксперимент: одна стратегия запускается один раз, затем выбранные домены проверяются параллельными `curl`.

Настройки:

- HTTP, TLS 1.2, TLS 1.3, HTTP3/QUIC;
- уровень поиска `quick`, `standard`, `force`;
- повторы проверки стратегии;
- параллельные повторы;
- пропуск DNS-проверки;
- пропуск проверки IP/port-блокировки;
- лимит параллельных `curl` для экспериментального режима.

По умолчанию лимит времени выключен. Подбор может длиться несколько часов. Кнопка остановки завершает текущий запуск и сохраняет уже найденные успешные стратегии.

## Локальные данные

Основные файлы создаются в:

```text
build/state/
  state.json
  jobs.jsonl
  strategy-finder/
    candidates.json
    runs.jsonl
    logs/
```

Эти данные локальные и не требуют публикации.

## Тесты

```bash
cd ~/gp/GP-access-control-plane
. .venv/bin/activate
python -m unittest discover -s tests
```
