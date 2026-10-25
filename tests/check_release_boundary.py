"""Check the exact Git publication list and CI secret/build boundaries."""
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
files = [name for name in files if name]
assert files, 'No release files found'
for name in files:
    path = Path(name)
    assert path.name == '.env.example' or not path.name.startswith('.env'), 'Runtime environment file in Git: ' + name
    assert not any(part in {'data', '.venv', '.venv-runtime', '__pycache__', 'backups', 'local-notes'} for part in path.parts), 'Runtime/local directory in Git: ' + name
    assert not re.search(r'\.(?:sqlite3?|db|pem|key|p12|pfx)(?:$|[-.])', name, re.I), 'Private file in Git: ' + name
    if path.suffix.lower() in {'.png', '.jpg', '.webp'}:
        continue
    contents = (root / name).read_text()
    assert not re.search(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)', contents), 'Credential material in Git: ' + name

dockerfile = (root / 'Dockerfile').read_text()
assert not re.search(r'^\s*(?:COPY|ADD)\s+\.\s', dockerfile, re.M), 'Docker must use explicit source copies'
assert not re.search(r'^\s*(?:ENV|ARG)\s+.*(?:SECRET_KEY|ADMIN_PASSWORD|EMBY_|SMTP_|TMDB_|CMS_TOKEN)', dockerfile, re.M), 'Runtime credentials must not be Docker build inputs'
ignore = (root / '.dockerignore').read_text().splitlines()
assert '**' in ignore and '!app.py' in ignore and '!static/' in ignore, 'Build context must use an allowlist'
assert not any(line in ignore for line in ('!data/', '!.env', '!**')), 'Build context must exclude runtime data'
workflow = (root / '.github/workflows/container.yml').read_text()
assert set(re.findall(r'secrets\.([A-Za-z0-9_]+)', workflow)) <= {'GITHUB_TOKEN'}, 'Build may use only the ephemeral registry token'
assert 'build-args:' not in workflow and 'secret-files:' not in workflow, 'Runtime configuration must not be passed to the image build'
example = dict(line.split('=', 1) for line in (root / '.env.example').read_text().splitlines() if '=' in line and not line.startswith('#'))
for key in ('SECRET_KEY', 'ADMIN_PASSWORD'):
    assert example[key].startswith('replace-with-'), 'Environment example must contain placeholders'
print('Release boundary verified: %s Git files; no runtime configuration or credentials in build inputs.' % len(files))
