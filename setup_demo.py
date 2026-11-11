"""One-time local demo setup. Never commit the generated .env."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent
file = root / '.env'
if file.exists():
    raise SystemExit('.env already exists; refusing to overwrite credentials or configuration')
password = secrets.token_urlsafe(18)
content = '\n'.join([
    '# Generated local demo credentials. Do not commit this file.',
    'APP_ADMIN_USER=admin',
    f'APP_ADMIN_PASSWORD={password}',
    f'APP_SECRET={secrets.token_urlsafe(48)}',
    f'ENGINE_EVENT_KEY={secrets.token_urlsafe(48)}',
    'APP_DEMO_MODE=true',
    'APP_TIMEZONE=Africa/Cairo',
    'APP_DB_PATH=./data/queue.db',
    '',
])
fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(content)
print('Created .env with random credentials.')
print('Username: admin')
print('Password:', password)
print('Store this password safely. Run: python run.py')
