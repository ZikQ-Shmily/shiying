"""Run with python -S inside the executor container after copying the addon to /config.

Updates the existing bind-mounted plugin in place, retaining its other features.
Restoring the backup and restarting CMS removes the addon. No credentials here.
"""
import ast
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

target = Path('/cms/cms-api/usercustomize.py')
addon = Path('/config/shiying-executor.py').read_text()
bridge = Path('/config/shiying-bridge.py')
if bridge.exists():
    addon = bridge.read_text() + '\n' + addon
source = target.read_text()
begin, end = '# BEGIN SHIYING EXECUTOR V1\n', '# END SHIYING EXECUTOR V1\n'
if begin in source:
    # Preserve fail-closed behavior when upgrading an existing registry.
    with sqlite3.connect('file:/config/cms-online.db?mode=ro', uri=True) as db:
        row = db.execute('SELECT config_json FROM cms_config WHERE key=?', ('cms_shiying_subscriptions_v1',)).fetchone()
    if row:
        registry = json.loads(row[0])
        assert isinstance(registry.get('entries'), dict) and isinstance(registry.get('owners'), dict)
        Path('/config/shiying-registry-established').touch(mode=0o600, exist_ok=True)
    before, rest = source.split(begin, 1)
    _, after = rest.split(end, 1)
    source = before + after
marker = 'def _make_panso_middleware(original_app):'
assert source.count(marker) == 1
source = source.replace(marker, begin + addon + '\n' + end + '\n' + marker, 1)
injection = ('        if path.startswith("/api/shiying-executor/"):\n'
             '            await _shiying_executor(scope, receive, send)\n'
             '            return\n')
if injection not in source:
    marker = '        path = scope["path"]\n'
    assert source.count(marker) == 1
    source = source.replace(marker, marker + injection, 1)
bridge_injection = ('        if path.startswith("/api/shiying/"):\n'
                    '            await _shiying_bridge(scope, receive, send)\n'
                    '            return\n')
if bridge.exists() and bridge_injection not in source:
    source = source.replace('        path = scope["path"]\n', '        path = scope["path"]\n' + bridge_injection, 1)
ast.parse(source)
backup = Path('/config/shiying-backups')
backup.mkdir(mode=0o700, exist_ok=True)
saved = backup / ('usercustomize-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.py')
saved.write_bytes(target.read_bytes())
saved.chmod(0o600)
target.write_text(source)
print('EXECUTOR_PLUGIN_UPDATED', hashlib.sha256(source.encode()).hexdigest(), 'BACKUP', str(saved))
