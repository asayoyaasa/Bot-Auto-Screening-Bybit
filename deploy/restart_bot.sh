#!/bin/bash
sudo systemctl restart bybit-bot bybit-auto-trades
echo "Bot restarted at $(date)" >> /var/log/bot_restart.log
