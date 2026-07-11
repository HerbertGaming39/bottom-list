#!/usr/bin/env bash
set +e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
mkdir -p .pids
if [ -f .pids/app.pid ]; then
  kill -TERM "$(cat .pids/app.pid)" 2>/dev/null
fi
if [ -f .pids/bot.pid ]; then
  kill -TERM "$(cat .pids/bot.pid)" 2>/dev/null
fi
sleep 1
pkill -f "app.py" 2>/dev/null
pkill -f "discord_bot.py" 2>/dev/null
rm -f .pids/app.pid .pids/bot.pid
echo "killed"
