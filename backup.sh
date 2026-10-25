#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose exec -T web python - <<'PY'
import datetime, pathlib, sqlite3
root = pathlib.Path('/app/data')
folder = root / 'backups'
folder.mkdir(mode=0o700, exist_ok=True)
path = folder / ('media-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
with sqlite3.connect(root / 'media.sqlite3') as source:
    with sqlite3.connect(path) as target:
        source.backup(target)
path.chmod(0o600)
print('数据库一致性备份已生成：data/backups/' + path.name)
PY
