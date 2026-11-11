import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

class Config:
    admin_user = os.getenv('APP_ADMIN_USER', 'admin')
    admin_password = os.getenv('APP_ADMIN_PASSWORD', '')
    reviewer_user = os.getenv('APP_REVIEWER_USER', 'reviewer')
    reviewer_password = os.getenv('APP_REVIEWER_PASSWORD', '')
    secret = os.getenv('APP_SECRET', '')
    engine_event_key = os.getenv('ENGINE_EVENT_KEY', '')
    demo_mode = os.getenv('APP_DEMO_MODE', 'false').lower() == 'true'
    recording_engine_key = os.getenv('RECORDING_ENGINE_KEY', '')
    monitor_engine_key = os.getenv('MONITOR_ENGINE_KEY', '')
    recordings_enabled = os.getenv('RECORDINGS_ENABLED', 'false').lower() == 'true'
    monitoring_enabled = os.getenv('MONITORING_ENABLED', 'false').lower() == 'true'
    recording_dir = Path(os.getenv('RECORDING_DIR', str(PROJECT_ROOT / 'data' / 'recordings'))).expanduser()
    if not recording_dir.is_absolute():
        recording_dir = PROJECT_ROOT / recording_dir
    timezone = os.getenv('APP_TIMEZONE', 'Africa/Cairo')
    db_path = Path(os.getenv('APP_DB_PATH', str(PROJECT_ROOT / 'data' / 'queue.db'))).expanduser()
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

config = Config()

def validate_config():
    if len(config.admin_password) < 12 or config.admin_password == 'REPLACE_WITH_STRONG_PASSWORD':
        raise RuntimeError('Configure APP_ADMIN_PASSWORD with at least 12 characters in .env')
    if len(config.secret) < 32 or config.secret.startswith('REPLACE_'):
        raise RuntimeError('Configure APP_SECRET with >=32 random characters in .env')
    if len(config.engine_event_key) < 32 or config.engine_event_key.startswith('REPLACE_'):
        raise RuntimeError('Configure ENGINE_EVENT_KEY with >=32 random characters in .env')
    if config.recordings_enabled and (len(config.recording_engine_key) < 32 or config.recording_engine_key.startswith('REPLACE_') or config.recording_engine_key in (config.engine_event_key,config.monitor_engine_key)):
        raise RuntimeError('RECORDING_ENGINE_KEY must be unique and >=32 characters')
    if config.monitoring_enabled and (config.admin_user == config.reviewer_user or len(config.reviewer_password) < 12 or config.reviewer_password == config.admin_password):
        raise RuntimeError('Monitoring needs a separate reviewer identity and strong unique password')
    if config.monitoring_enabled and (len(config.monitor_engine_key) < 32 or config.monitor_engine_key.startswith('REPLACE_') or config.monitor_engine_key in (config.engine_event_key,config.recording_engine_key)):
        raise RuntimeError('MONITOR_ENGINE_KEY must be unique and >=32 characters')
