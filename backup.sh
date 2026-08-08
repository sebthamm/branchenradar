#!/bin/bash
# Daily backup of Branchenradar data directory
# Setup: chmod +x /opt/branchenradar/backup.sh
# Cron:  0 2 * * * /opt/branchenradar/backup.sh

BACKUP_DIR="/opt/branchenradar/backups"
DATA_DIR="/opt/branchenradar/data"
KEEP_DAYS=30
TS=$(date +"%Y%m%d_%H%M%S")

mkdir -p "$BACKUP_DIR"
tar -czf "$BACKUP_DIR/data_$TS.tar.gz" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"

# Delete backups older than KEEP_DAYS
find "$BACKUP_DIR" -name "data_*.tar.gz" -mtime +$KEEP_DAYS -delete
