# Telegram Aster Coin Price Monitor Bot

This repository contains a production-ready Python bot that monitors the
spread between the AsterDex and Dexscreener prices for a configurable trading
pair and posts alerts to a Telegram channel whenever the spread exceeds a
threshold.

## Features

- Concurrent price fetching from the AsterDex and Dexscreener public APIs.
- Exponential backoff with configurable retry counts for resilient networking.
- Markdown-formatted Telegram notifications that include the absolute and
  percentage spread.
- Graceful shutdown handling for container and CLI deployments.
- Environment-based configuration for tokens, channels, thresholds and
  polling cadence.

## Requirements

- Python 3.9+
- Dependencies listed in `requirements.txt`

Install dependencies with:

```bash
pip install -r requirements.txt
```

## Configuration

The bot is configured using environment variables. The following variables are
supported:

| Variable | Required | Description | Default |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram bot token obtained from BotFather | – |
| `TELEGRAM_CHANNEL_ID` | ✅ | Channel/chat ID to post alerts to | – |
| `PRICE_THRESHOLD` | ❌ | Minimum percentage spread (e.g. `0.05` for 5%) to trigger notifications | `0.05` |
| `CHECK_INTERVAL` | ❌ | Seconds between price checks | `60` |
| `ASTER_SYMBOL` | ❌ | Market symbol for the AsterDex API | `ADAUSDT` |
| `DEX_CHAIN_ID` | ❌ | Dexscreener chain identifier | `bsc` |
| `DEX_PAIR_ID` | ❌ | Dexscreener pair identifier | `0x3817fF61B34c5Ff5dc89709B2Db1f194299e3BA9` |
| `REQUEST_TIMEOUT` | ❌ | Timeout (seconds) applied to HTTP requests | `10` |
| `MAX_RETRIES` | ❌ | Maximum retries for failed HTTP requests | `3` |
| `BACKOFF_FACTOR` | ❌ | Multiplier for exponential backoff delays | `2.0` |

## Running the Bot

After setting the environment variables, start the bot with:

```bash
python price_monitor_bot.py
```

The bot will continue running until it is interrupted (Ctrl+C) or receives a
termination signal.
