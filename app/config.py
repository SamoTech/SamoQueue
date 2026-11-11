import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

class Config:
    admin_user = os.getenv('APP_ADMIN_USER', 'admin')
    admin_password = os.getenv('APP_ADMIN_PASSWORD', '')
    secret = os.getenv('APP_SECRET', '')
    ingest_key = os.getenv('AVAYA_INGEST_KEY', '')
    aura_command_key = os.getenv('AURA_COMMAND_BRIDGE_KEY', '')
    demo_mode = os.getenv('APP_DEMO_MODE', 'false').lower() == 'true'
    aura_control_enabled = os.getenv('AURA_CONTROL_ENABLED', 'false').lower() == 'true'
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
    if len(config.ingest_key) < 32 or config.ingest_key.startswith('REPLACE_'):
        raise RuntimeError('Configure AVAYA_INGEST_KEY with >=32 random characters in .env')
    if config.aura_control_enabled and (len(config.aura_command_key) < 32 or config.aura_command_key.startswith('REPLACE_') or config.aura_command_key == config.ingest_key):
        raise RuntimeError('Live Aura call control requires a distinct, >=32 character AURA_COMMAND_BRIDGE_KEY')
