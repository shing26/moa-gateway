# Ticket 2: Audit Log Persistence + Rotation

**Type**: implement
**Priority**: Medium
**Blocked by**: -

## Task

1. Add LogConfig with LOG_DIR, LOG_RETENTION_DAYS env vars
2. Create LogFileWriter that writes AsyncWal entries to daily JSONL files
3. Add log rotation (delete files older than retention days)
4. Wire into EsWriter._fallback_wal as persistent fallback

## Acceptance Criteria

- LOG_DIR defaults to ./logs/
- Log files named audit-YYYY-MM-DD.jsonl
- Old logs auto-deleted per LOG_RETENTION_DAYS
- New unit tests for rotation
