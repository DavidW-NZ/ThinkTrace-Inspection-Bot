# VM Runbook

This repo already contains the Telegram inspection bot implementation in `main.py` and the export worker in `worker.py`.

This VM setup keeps that implementation unchanged and runs it as two `systemd` services:

- `inspection-bot.service` for Telegram long polling
- `inspection-bot-worker.service` for queued export jobs

## Install

```bash
cd "/home/ubuntu/projects/Inspection Bot_local_v1"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
sudo mkdir -p /etc/inspection-bot
sudo mkdir -p /var/lib/inspection-bot
sudo cp .env.example /etc/inspection-bot/inspection-bot.env
sudo cp projects.json /var/lib/inspection-bot/projects.json
sudo mkdir -p /var/lib/inspection-bot/templates
sudo cp templates/report_template.docx /var/lib/inspection-bot/templates/report_template.docx
sudo cp deploy/systemd/inspection-bot.service /etc/systemd/system/inspection-bot.service
sudo cp deploy/systemd/inspection-bot-worker.service /etc/systemd/system/inspection-bot-worker.service
sudo systemctl daemon-reload
sudo systemctl enable inspection-bot.service inspection-bot-worker.service
```

## Configure

Edit `/etc/inspection-bot/inspection-bot.env` and set:

```dotenv
TELEGRAM_BOT_TOKEN=...
THINKTRACE_BASE_URL=https://knowledge-accumulator-e9f2fgbuapfmdne8.australiaeast-01.azurewebsites.net
TELEGRAM_BRIDGE_TOKEN=...
TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN=...
INSPECTION_DATA_ROOT=/var/lib/inspection-bot
```

Notes:

- `TELEGRAM_BOT_TOKEN`, `THINKTRACE_BASE_URL`, and `TELEGRAM_BRIDGE_TOKEN` are required runtime configuration for the current bot/runtime flow.
- `TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN` is additionally required by the worker to POST generated report outputs to `/telegram/inspection-outputs`.
- `INSPECTION_DATA_ROOT` should contain `projects.json`, `templates/report_template.docx`, and all runtime state.

## Start

```bash
sudo systemctl start inspection-bot.service inspection-bot-worker.service
```

## Stop

```bash
sudo systemctl stop inspection-bot.service inspection-bot-worker.service
```

## Restart

```bash
sudo systemctl restart inspection-bot.service inspection-bot-worker.service
```

## Status

```bash
sudo systemctl status inspection-bot.service
sudo systemctl status inspection-bot-worker.service
```

## Logs

```bash
sudo journalctl -u inspection-bot.service -f
sudo journalctl -u inspection-bot-worker.service -f
```

## Command Scope Verified

Runtime setup was kept limited to the existing bot implementation already present in this repo.

- `/start` is implemented in `main.py`
- `/whoami` is not present in this branch
- `/setups` is not present in this branch

This VM work does not add new commands, setup selection, session injection, webhook hosting, or bridge-side architecture changes.
