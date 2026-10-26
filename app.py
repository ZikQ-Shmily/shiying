import datetime as dt
import base64
import copy
import hmac
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, session, Response, g
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from integrations import (TMDBClient, EmbyClient, EmbyUserClient, EmbyAuthError, IntegrationError, fetch,
                          valid_emby_url, poster_path, safe_date)
from cms_client import CMSClient, CMSExecutor, canonical_re0_post_url
from share_scanner import read_share_episodes
from email_notifications import (DEFAULTS as MAIL_DEFAULTS, MailError,
    validate_config as validate_mail_config, require_ready as require_mail_ready, send_notification)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).parent
DATA = Path(os.environ.get('DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / 'media.sqlite3'
TZ = ZoneInfo('Asia/Shanghai')
ADMIN = os.environ.get('ADMIN_USERNAME', 'admin')
SECRET = os.environ.get('SECRET_KEY', '')
if len(SECRET) < 32:
    raise RuntimeError('SECRET_KEY must contain at least 32 characters')

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config.update(SECRET_KEY=SECRET, MAX_CONTENT_LENGTH=8 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                  SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == 'true',
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=7))
app.json.ensure_ascii = False


@contextmanager
def database():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_re0_post_urls(db):
    changed = 0
    for row in db.execute('SELECT id, payload FROM items').fetchall():
        item = json.loads(row['payload'])
        repaired = False
        for source in item.get('sources', []):
            original = source.get('post_url', '')
            canonical = canonical_re0_post_url(original)
            if canonical != original:
                source['post_url'] = canonical
                repaired = True
        if repaired:
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), row['id']))
            changed += 1
    return changed


with database() as db:
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                                     deleted_at TEXT);
    CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS attempts_time ON attempts(created);
    CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS email_outbox (
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, recipient TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        last_error TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS email_outbox_ready ON email_outbox(state, next_attempt);
    ''')
    if 'identity' not in {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}:
        db.execute('ALTER TABLE sessions ADD COLUMN identity TEXT')
    migrate_re0_post_urls(db)
    if not db.execute("SELECT value FROM settings WHERE key='password'").fetchone():
        initial = os.environ.get('ADMIN_PASSWORD', '')
        if len(initial) < 12:
            raise RuntimeError('Set ADMIN_PASSWORD with at least 12 characters for first startup')
        db.execute('INSERT INTO settings VALUES (?,?)', ('password', generate_password_hash(initial)))
        db.execute('INSERT INTO settings VALUES (?,?)', ('auth_version', secrets.token_hex(16)))


def now():
    return dt.datetime.now(TZ).isoformat(timespec='seconds')


def setting(key):
    with database() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else ''


def authenticated():
    if hasattr(g, 'principal'):
        return bool(g.principal)
    sid = session.get('sid', '')
    if not sid:
        return False
    with database() as db:
        row = db.execute('SELECT expires, identity FROM sessions WHERE id=?',
                         (hashlib.sha256(sid.encode()).hexdigest(),)).fetchone()
    if not row or row['expires'] <= time.time():
        g.principal = None
        return False
    identity = json.loads(row['identity']) if row['identity'] else dict(
        id='local:admin', username=ADMIN, provider='local', role='admin')
    if identity['provider'] == 'local':
        if session.get('user') != ADMIN or not hmac.compare_digest(
                str(session.get('version', '')), setting('auth_version')):
            return False
    else:
        config = integration_config()
        if identity.get('emby_url') != config.get('emby_url'):
            return False
        if time.time() - identity.get('checked_at', 0) >= 60:
            try:
                user = EmbyUserClient(identity['emby_url'], decrypt_secret(identity['token']), identity.get('device_id', 'shiying')).user(identity['emby_user_id'])
            except EmbyAuthError:
                with database() as db:
                    db.execute('DELETE FROM sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
                session.clear()
                return False
            identity.update(username=str(user['Name']), role='admin' if user['Policy'].get('IsAdministrator') is True else 'user',
                            checked_at=time.time())
            with database() as db:
                db.execute('UPDATE sessions SET identity=? WHERE id=?',
                           (json.dumps(identity), hashlib.sha256(sid.encode()).hexdigest()))
    g.principal = identity
    return True


def principal():
    if not authenticated():
        return None
    return g.principal


def is_admin():
    return (principal() or {}).get('role') == 'admin'


def public_identity(identity):
    config = integration_config()
    return dict(username=identity['username'], user_id=identity['id'], role=identity['role'],
                provider=identity['provider'], tmdb_configured=bool(config.get('tmdb_credential')),
                emby_available=bool(config.get('emby_url')))


def fail(message, code=400):
    return jsonify(error=message), code


@app.before_request
def protect():
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return fail('请求来源不匹配，请刷新页面重试', 403)
    if not request.path.startswith('/api/') or request.path in ('/api/login', '/api/session'):
        return None
    if not authenticated():
        return fail('请先登录', 401)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-CSRF-Token', '')
        if not supplied or not hmac.compare_digest(supplied, session.get('csrf', '')):
            return fail('登录状态已更新，请刷新页面重试', 403)
    if not is_admin():
        allowed = ((request.method == 'GET' and (request.path == '/api/items' or request.path.startswith('/api/tmdb/'))) or
                   (request.method == 'POST' and (request.path in ('/api/logout', '/api/tmdb/track') or
                    re.fullmatch(r'/api/items/[^/]+/withdraw', request.path))))
        if not allowed or (request.path == '/api/items' and request.args.get('trash') == '1'):
            return fail('此功能仅限管理员使用', 403)


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'")
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.errorhandler(Exception)
def error_handler(error):
    if isinstance(error, (IntegrationError, MailError)):
        return fail(str(error), 502)
    if isinstance(error, HTTPException):
        return fail('请求内容过大' if error.code == 413 else '请求格式错误或地址不存在', error.code)
    if isinstance(error, ValueError):
        return fail(str(error))
    app.logger.exception('Request failed')
    return fail('服务器暂时无法处理，请稍后重试', 500)


def body():
    data = request.get_json()
    if not isinstance(data, dict):
        raise ValueError('请提交有效的数据对象')
    return data


def text(value, label, max_length=2000):
    if not isinstance(value, str):
        raise ValueError(label + '必须为文字')
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(label + '过长')
    return value


def integer(value, label, minimum=1, maximum=5000, nullable=True):
    if value is None or value == '':
        if nullable:
            return None
        raise ValueError(label + '不能为空')
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError(label + '必须为整数')
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(label + f'须在 {minimum}～{maximum} 之间')
    return number


def boolean(value, label):
    if type(value) is not bool:
        raise ValueError(label + '必须为是或否')
    return value


def parse_episodes(value):
    value = text(value, '集数', 20000).replace('，', ',').replace('、', ',').replace('～', '-').replace('—', '-')
    if not value:
        return set()
    result = set()
    for part in value.split(','):
        match = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+))?\s*', part)
        if not match:
            raise ValueError('集数格式应为 1-8,10-12；单填 12 仅代表第 12 集')
        first = int(match[1])
        last = int(match[2] or first)
        if first < 1 or last < first or last > 5000:
            raise ValueError('集数范围须在 1～5000 之间，且起点不大于终点')
        result.update(range(first, last + 1))
    return result


def format_episodes(values):
    values = sorted(values)
    groups = []
    for value in values:
        if groups and value == groups[-1][1] + 1:
            groups[-1][1] = value
        else:
            groups.append([value, value])
    return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in groups)


def link(value):
    value = text(value, '链接', 2000)
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('链接须为有效的 http 或 https 地址')
    return value


def date_value(value):
    value = text(value, '检查日期', 10)
    if value:
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError('检查日期格式应为 YYYY-MM-DD')
    return value


def normalize(data, item_id=None, existing=None):
    title = text(data.get('title', ''), '作品名称', 150)
    if not title:
        raise ValueError('请填写作品名称')
    kind = data.get('kind', 'tv')
    if kind not in ('tv', 'movie', 'anime', 'documentary'):
        raise ValueError('作品类型无效')
    sources = data.get('sources', [])
    if not isinstance(sources, list) or len(sources) > 50:
        raise ValueError('每部作品最多保存 50 个来源')
    normalized_sources = []
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('来源格式无效')
        source_id = text(source.get('id') or str(uuid.uuid4()), '来源标识', 100)
        if source_id in seen:
            raise ValueError('来源标识重复')
        seen.add(source_id)
        normalized_sources.append(dict(
            id=source_id, sharer=text(source.get('sharer', ''), '分享者', 200),
            post_url=canonical_re0_post_url(link(source.get('post_url', ''))), share_url=link(source.get('share_url', '')),
            code=text(source.get('code', ''), '提取码', 100),
            version=text(source.get('version', ''), '版本', 500),
            episodes=format_episodes(parse_episodes(source.get('episodes', ''))),
            broken=boolean(source.get('broken', False), '来源失效'),
            note=text(source.get('note', ''), '来源备注', 2000)))
    active = text(data.get('active_source_id') or '', '当前来源', 100)
    if active and active not in seen:
        raise ValueError('当前使用的来源不存在')
    if normalized_sources and not active:
        active = normalized_sources[0]['id']
    total = integer(data.get('total_episodes'), '总集数')
    library = parse_episodes(data.get('library_episodes', ''))
    if kind == 'movie':
        total = 1
        if library - {1} or any(parse_episodes(s['episodes']) - {1} for s in normalized_sources):
            raise ValueError('电影的入库和来源进度使用 1 表示已有，留空表示没有')
    if total and not ((existing or {}).get('tmdb') or data.get('tmdb')) and (any(ep > total for ep in library) or any(
            ep > total for source in normalized_sources for ep in parse_episodes(source['episodes']))):
        raise ValueError('已记录的集数不能超过总集数；特别篇可单独建立记录')
    old = existing or {}
    for source in normalized_sources:
        previous = next((value for value in old.get('sources', []) if value['id'] == source['id']), {})
        if previous.get('episode_scan') and all(source[key] == previous.get(key, '') for key in ('share_url', 'code', 'episodes')):
            source['episode_scan'] = copy.deepcopy(previous['episode_scan'])
    # last_check is server controlled; imports may restore a valid historical timestamp.
    checked = old.get('last_check')
    candidate = data.get('last_check') if not existing else None
    if candidate:
        try:
            checked = dt.datetime.fromisoformat(candidate).isoformat(timespec='seconds')
        except (ValueError, TypeError):
            raise ValueError('上次检查时间无效')
    result = dict(id=item_id or str(uuid.uuid4()), title=title, kind=kind,
                year=integer(data.get('year'), '年份', 1888, 2200),
                season=integer(data.get('season', 1), '季数', 1, 1000, False),
                total_episodes=total, finished=boolean(data.get('finished', False), '作品完结'),
                completed=boolean(data.get('completed', False), '确认全集'),
                library_episodes=format_episodes(library), sources=normalized_sources,
                active_source_id=active, notes=text(data.get('notes', ''), '备注', 10000),
                last_check=checked, next_check=date_value(data.get('next_check', '')),
                created_at=old.get('created_at', now()), updated_at=now())
    result['tracking'] = boolean(data.get('tracking', old.get('tracking', True)), '自动追更')
    result['tmdb'] = normalize_tmdb(old.get('tmdb') if existing else data.get('tmdb'))
    result['emby'] = copy.deepcopy(old.get('emby', {'state': 'never'}))
    result['events'] = copy.deepcopy(old.get('events', []))
    if old.get('re0') and active == old.get('active_source_id'):
        result['re0'] = copy.deepcopy(old['re0'])
    if old.get('cms_delivery'):
        result['cms_delivery'] = copy.deepcopy(old['cms_delivery'])
    if old.get('cms_run'):
        result['cms_run'] = copy.deepcopy(old['cms_run'])
    result['requesters'] = normalize_requesters(old.get('requesters', []) if existing else data.get('requesters', []))
    if existing and result['tmdb'] and any(result[field] != old[field] for field in
            ('title', 'kind', 'year', 'season', 'total_episodes', 'finished')):
        raise ValueError('作品信息来自 TMDB，不能手动修改。请同步 TMDB 更新资料；更换作品或季请重新搜索添加')
    return result


def normalize_tmdb(meta):
    if not meta:
        return None
    if not isinstance(meta, dict):
        raise ValueError('TMDB 资料格式无效')
    identifier = integer(meta.get('id'), 'TMDB ID', 1, 999999999999, False)
    media_type = meta.get('media_type')
    if media_type not in ('tv', 'movie'):
        raise ValueError('TMDB 作品类型无效')
    dates = meta.get('air_dates', {})
    if not isinstance(dates, dict) or len(dates) > 5000:
        raise ValueError('TMDB 分集资料过大或格式无效')
    valid_dates = {}
    for key, value in dates.items():
        number = integer(key, 'TMDB 集号', 1, 5000, False)
        if value and not safe_date(value):
            raise ValueError('TMDB 播出日期无效')
        valid_dates[str(number)] = value or ''
    return dict(id=identifier, media_type=media_type, poster_path=poster_path(meta.get('poster_path')),
                overview=text(meta.get('overview', ''), '作品简介', 10000),
                original_title=text(meta.get('original_title', ''), '原始名称', 1000),
                status=text(meta.get('status', ''), 'TMDB 状态', 100), air_dates=valid_dates,
                synced_at=text(meta.get('synced_at', ''), 'TMDB 更新时间', 50),
                metadata_error=text(meta.get('metadata_error', ''), '资料更新提示', 500))


def normalize_requesters(values):
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError('提交用户记录格式无效')
    result = []
    seen = set()
    for entry in values:
        if not isinstance(entry, dict):
            raise ValueError('提交用户记录格式无效')
        uid = text(entry.get('user_id', ''), '提交用户标识', 400)
        if not uid or uid in seen:
            raise ValueError('提交用户标识为空或重复')
        seen.add(uid)
        result.append(dict(user_id=uid, username=text(entry.get('username', ''), '提交用户名', 200),
                           provider=text(entry.get('provider', ''), '账号来源', 20),
                           submitted_at=text(entry.get('submitted_at', ''), '提交时间', 50),
                           active=boolean(entry.get('active', True), '追更状态'),
                           withdrawn_at=text(entry.get('withdrawn_at', ''), '取消时间', 50)))
    return result


def join_request(item):
    actor = principal()
    requesters = item.setdefault('requesters', [])
    entry = next((entry for entry in requesters if entry['user_id'] == actor['id']), None)
    if entry and entry['active']:
        entry['username'] = actor['username']
        return False
    if entry:
        entry.update(active=True, withdrawn_at='', username=actor['username'], submitted_at=now())
    else:
        requesters.append(dict(user_id=actor['id'], username=actor['username'], provider=actor['provider'],
                               submitted_at=now(), active=True, withdrawn_at=''))
    event(item, 'requested', actor['username'] + ' 提交了追更')
    item['updated_at'] = now()
    return True


def item_for_user(item):
    if is_admin():
        return decorate(item)
    actor = principal()
    requester = next((entry for entry in item.get('requesters', []) if entry['user_id'] == actor['id']), None)
    if not requester:
        return None
    full = decorate(item)
    allowed = ('id', 'title', 'kind', 'year', 'season', 'total_episodes', 'library_episodes', 'status',
               'aired_count', 'library_count', 'not_aired_count', 'missing_episodes', 'aired_episodes',
               'airing_today_count', 'airing_today_episodes',
               'updated_at', 'created_at', 'tracking')
    result = {key: full[key] for key in allowed}
    metadata = full.get('tmdb')
    result['tmdb'] = {key: metadata.get(key) for key in ('id', 'media_type', 'poster_path', 'overview', 'synced_at')} if metadata else None
    result['emby'] = {key: full.get('emby', {}).get(key) for key in ('state', 'checked_at')}
    result['request_active'] = requester['active']
    result['requested_at'] = requester['submitted_at']
    result['withdrawn_at'] = requester.get('withdrawn_at', '')
    return result


def event(item, action, detail):
    item['events'] = (item.get('events', []) + [dict(at=now(), action=action, detail=detail)])[-100:]


def decorate(item):
    item = dict(item)
    item['sources'] = [dict(source, post_url=canonical_re0_post_url(source.get('post_url', '')))
                       for source in item['sources']]
    item.setdefault('tmdb', None)
    item.setdefault('emby', {'state': 'never'})
    item.setdefault('tracking', True)
    item.setdefault('events', [])
    item.setdefault('requesters', [])
    source = next((s for s in item['sources'] if s['id'] == item['active_source_id']), None)
    available = parse_episodes(source['episodes']) if source else set()
    library = parse_episodes(item['library_episodes'])
    today = dt.datetime.now(TZ).date().isoformat()
    dates = (item['tmdb'] or {}).get('air_dates', {})
    # TMDB provides dates, not release times: today's schedule is not proof of availability.
    aired = {int(number) for number, date in dates.items() if date and date < today}
    airing_today = {int(number) for number, date in dates.items() if date == today}
    missing = (available | aired) - library
    total = item['total_episodes']
    full = bool(total and set(range(1, total + 1)).issubset(library))
    manual_complete = not total and item['completed'] and bool(library) and not missing
    if full or manual_complete:
        status = 'complete'
    elif item['tmdb'] and airing_today and not aired and not library and not available:
        status = 'premiering'
    elif item['tmdb'] and dates and not aired and not library and not available:
        status = 'upcoming'
    elif not source:
        status = 'wanted'
    elif item['tmdb'] and missing:
        status = 'missing' if library else 'pending'
    elif item['tmdb'] and library and not missing and not item['finished']:
        status = 'updating'
    elif missing:
        status = 'missing' if library else 'pending'
    elif not library and not available:
        status = 'pending'
    elif item['finished']:
        status = 'waiting_source'
    else:
        status = 'updating'
    item.update(status=status, due=bool(status != 'complete' and item['next_check'] and
                                      item['next_check'] <= dt.datetime.now(TZ).date().isoformat()),
                missing_episodes=format_episodes(missing), available_count=len(available),
                library_count=len(library), source_broken=bool(source and source['broken']),
                aired_episodes=format_episodes(aired), aired_count=len(aired),
                airing_today_episodes=format_episodes(airing_today), airing_today_count=len(airing_today),
                not_aired_count=max(0, len(dates) - len(aired) - len(airing_today)))
    # Keep legacy check dates independent from actionable collection and sync issues.
    reasons = []
    verified = not item['tmdb'] or item['emby'].get('state') in ('matched', 'absent')
    if status == 'wanted':
        reasons.append(dict(code='source_missing', label='待找资源',
                            detail='尚未绑定资源来源，请先选择并绑定 RE0 资源'))
    elif status != 'complete' and missing and verified:
        reasons.append(dict(code='missing', label='待入库' if item['kind'] == 'movie' else '待补集',
                            detail='等待入库：' + format_episodes(missing)))
    if status != 'complete' and item['source_broken']:
        reasons.append(dict(code='source_error', label='来源异常', detail='当前来源已标记失效，请核对资源链接'))
    sync_errors = []
    if (item['tmdb'] or {}).get('metadata_error'):
        sync_errors.append('TMDB：' + item['tmdb']['metadata_error'])
    if item['emby'].get('state') == 'error':
        sync_errors.append('Emby：' + (item['emby'].get('error') or '入库比对失败'))
    if (item.get('re0') or {}).get('error'):
        sync_errors.append('RE0：' + item['re0']['error'])
    if (item.get('cms_delivery') or {}).get('error'):
        sync_errors.append('CMS：' + item['cms_delivery']['error'])
    if sync_errors:
        reasons.append(dict(code='sync_error', label='同步失败', detail='；'.join(sync_errors)))
    item['attention_reasons'] = reasons
    item['needs_attention'] = bool(reasons) and item['tracking'] is not False
    return item


def get_item(db, item_id, allow_deleted=False):
    row = db.execute('SELECT payload, deleted_at FROM items WHERE id=?', (item_id,)).fetchone()
    if not row or (row['deleted_at'] and not allow_deleted):
        return None
    return json.loads(row['payload'])


@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.get('/health')
def health():
    with database() as db:
        db.execute('SELECT 1').fetchone()
    return jsonify(status='ok')


@app.get('/api/session')
def session_info():
    if authenticated():
        return jsonify(authenticated=True, csrf=session['csrf'], **public_identity(principal()))
    return jsonify(authenticated=False, emby_available=bool(integration_config().get('emby_url')))


@app.post('/api/login')
def login():
    data = body()
    ip = request.remote_addr or 'unknown'
    with database() as db:
        db.execute('DELETE FROM attempts WHERE created < ?', (time.time() - 900,))
        count = db.execute('SELECT COUNT(*) FROM attempts WHERE ip=?', (ip,)).fetchone()[0]
        if count >= 10:
            return fail('尝试次数过多，请 15 分钟后重试', 429)
    password = data.get('password', '')
    username = text(data.get('username', ''), '用户名', 200)
    provider = data.get('provider', 'local')
    identity = None
    auth_error = ''
    valid_password_format = isinstance(password, str) and len(password) <= 1000
    if provider == 'local':
        valid = valid_password_format and check_password_hash(setting('password'), password)
        if valid and username == ADMIN:
            identity = dict(id='local:admin', username=ADMIN, provider='local', role='admin')
    elif provider == 'emby':
        base_url = integration_config().get('emby_url', '')
        if not base_url:
            return fail('管理员尚未配置 EMBY 地址，请先使用本地管理员登录设置', 503)
        if valid_password_format and username:
            device = secrets.token_hex(16)
            try:
                result = EmbyUserClient(base_url, device_id=device).authenticate(username, password)
                user = result['user']
                identity = dict(id='emby:' + result['server_id'] + ':' + str(user['Id']),
                                username=str(user['Name']), provider='emby', role='admin' if user['Policy'].get('IsAdministrator') is True else 'user',
                                emby_user_id=str(user['Id']), server_id=result['server_id'], emby_url=base_url,
                                token=encrypt_secret(result['token']), device_id=device, checked_at=time.time())
            except EmbyAuthError as error:
                auth_error = str(error)
    else:
        return fail('登录方式无效')
    if not identity:
        with database() as db:
            db.execute('INSERT INTO attempts VALUES (?,?)', (ip, time.time()))
        return fail(auth_error or '账号或密码不正确', 401)
    session.clear()
    sid = secrets.token_urlsafe(32)
    session.update(user=identity['username'], version=setting('auth_version'), csrf=secrets.token_urlsafe(32), sid=sid)
    session.permanent = True
    with database() as db:
        db.execute('DELETE FROM attempts WHERE ip=?', (ip,))
        db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        db.execute('INSERT INTO sessions (id, expires, identity) VALUES (?,?,?)',
                   (hashlib.sha256(sid.encode()).hexdigest(), time.time() + 7 * 86400, json.dumps(identity)))
    g.principal = identity
    return jsonify(csrf=session['csrf'], **public_identity(identity))


@app.post('/api/logout')
def logout():
    identity = principal()
    with database() as db:
        db.execute('DELETE FROM sessions WHERE id=?',
                   (hashlib.sha256(session['sid'].encode()).hexdigest(),))
    session.clear()
    if identity['provider'] == 'emby':
        try:
            EmbyUserClient(identity['emby_url'], decrypt_secret(identity['token']), identity.get('device_id', 'shiying')).logout()
        except IntegrationError:
            pass  # Local session has already been revoked even if Emby is unreachable.
    return jsonify(ok=True)


@app.post('/api/password')
def password():
    if principal()['provider'] != 'local':
        return fail('EMBY 账号密码请在 EMBY 中修改；此处仅修改本地管理员密码', 403)
    data = body()
    old = data.get('old_password', '')
    new = data.get('new_password', '')
    if not isinstance(old, str) or len(old) > 1000 or not check_password_hash(setting('password'), old):
        return fail('当前密码不正确')
    if not isinstance(new, str) or not 12 <= len(new) <= 200:
        return fail('新密码须为 12～200 个字符')
    version = secrets.token_hex(16)
    with database() as db:
        db.execute("UPDATE settings SET value=? WHERE key='password'", (generate_password_hash(new),))
        db.execute("UPDATE settings SET value=? WHERE key='auth_version'", (version,))
        db.execute('DELETE FROM sessions WHERE id != ?',
                   (hashlib.sha256(session['sid'].encode()).hexdigest(),))
    session.update(version=version, csrf=secrets.token_urlsafe(32))
    return jsonify(csrf=session['csrf'])


@app.get('/api/items')
def items():
    trash = request.args.get('trash') == '1'
    with database() as db:
        rows = db.execute('SELECT payload FROM items WHERE deleted_at IS ' + ('NOT NULL' if trash else 'NULL')).fetchall()
    result = [value for row in rows if (value := item_for_user(json.loads(row['payload']))) is not None]
    return jsonify(items=sorted(result, key=lambda item: item['updated_at'], reverse=True))


@app.post('/api/items')
def create_item():
    item = normalize(body())
    item['requesters'] = []
    join_request(item)
    event(item, 'created', '手动添加作品，等待整理来源与入库进度')
    with database() as db:
        db.execute('INSERT INTO items VALUES (?,?,NULL)', (item['id'], json.dumps(item, ensure_ascii=False)))
    start_source_refresh(item['id'])
    return jsonify(item=decorate(item)), 201


@app.post('/api/items/<item_id>/sources/<source_id>/episodes')
def source_episodes(item_id, source_id):
    with database() as db:
        item = get_item(db, item_id)
    if not item:
        return fail('记录不存在', 404)
    source = next((source for source in item['sources'] if source['id'] == source_id), None)
    if not source:
        return fail('请先保存资源来源，再识别集数', 404)
    meta = item.get('tmdb') or {}
    result = read_share_episodes(source['share_url'], source.get('code', ''), season=item['season'],
                                tmdb_id=meta.get('id'), air_dates=meta.get('air_dates', {}),
                                media_type='movie' if item['kind'] == 'movie' else 'tv')
    return jsonify(result=result, source_id=source_id)


@app.put('/api/items/<item_id>')
def update_item(item_id):
    data = body()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = get_item(db, item_id)
        if not existing:
            return fail('记录不存在', 404)
        item = normalize(data, item_id, existing)
        if item['library_episodes'] != existing['library_episodes']:
            event(item, 'progress', '手动更新入库集数：' + (item['library_episodes'] or '尚未入库'))
        if item['active_source_id'] != existing['active_source_id']:
            event(item, 'source', '切换当前资源来源，历史来源继续保留')
        if item['tracking'] != existing.get('tracking', True):
            event(item, 'tracking', '开启自动追更' if item['tracking'] else '暂停自动追更')
        db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item_id))
    sync_cms_item(item_id)
    if (source_scan_identity(item) != source_scan_identity(existing) or
            (item['tracking'] and not existing.get('tracking', True))):
        start_source_refresh(item_id)
    with database() as db:
        item = get_item(db, item_id)
    return jsonify(item=decorate(item))


@app.delete('/api/items/<item_id>')
def delete_item(item_id):
    with database() as db:
        if not get_item(db, item_id):
            return fail('记录不存在', 404)
        db.execute('UPDATE items SET deleted_at=? WHERE id=?', (now(), item_id))
    sync_cms_item(item_id)
    with database() as db:
        item = get_item(db, item_id, True)
    return jsonify(ok=True, cms_warning=(item.get('cms_delivery') or {}).get('error', ''))


@app.post('/api/items/<item_id>/restore')
def restore_item(item_id):
    with database() as db:
        if not get_item(db, item_id, True):
            return fail('记录不存在', 404)
        db.execute('UPDATE items SET deleted_at=NULL WHERE id=?', (item_id,))
    sync_cms_item(item_id)
    with database() as db:
        item = get_item(db, item_id, True)
    return jsonify(ok=True, cms_warning=(item.get('cms_delivery') or {}).get('error', ''))


@app.post('/api/items/<item_id>/check')
def check_item(item_id):
    data = body()
    next_check = date_value(data.get('next_check', ''))
    with database() as db:
        item = get_item(db, item_id)
        if not item:
            return fail('记录不存在', 404)
        item.update(last_check=now(), next_check=next_check, updated_at=now())
        event(item, 'checked', '已手动检查资源来源')
        db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item_id))
    return jsonify(item=decorate(item))


@app.post('/api/items/<item_id>/withdraw')
def withdraw_request(item_id):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        item = get_item(db, item_id)
        entry = next((entry for entry in (item or {}).get('requesters', []) if entry['user_id'] == principal()['id']), None)
        if not item or not entry:
            return fail('没有找到你的追更记录', 404)
        if entry['active']:
            entry.update(active=False, withdrawn_at=now())
            item['updated_at'] = now()
            event(item, 'withdrawn', principal()['username'] + ' 取消了自己的追更')
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item_id))
    return jsonify(item=item_for_user(item))


@app.get('/api/backup')
def export_backup():
    with database() as db:
        rows = db.execute('SELECT payload, deleted_at FROM items').fetchall()
    result = []
    for row in rows:
        item = json.loads(row['payload'])
        item['deleted_at'] = row['deleted_at']
        result.append(item)
    response = jsonify(schema_version=1, exported_at=now(), items=result)
    filename = dt.datetime.now(TZ).strftime('shiying-backup-%Y%m%d-%H%M%S.json')
    response.headers['Content-Disposition'] = 'attachment; filename=' + filename
    return response


@app.post('/api/import')
def import_backup():
    data = body()
    values = data.get('items')
    if data.get('schema_version') != 1 or not isinstance(values, list) or len(values) > 10000:
        return fail('请选择拾影导出的备份文件（最多 10000 条）')
    normalized = []
    for value in values:
        if not isinstance(value, dict):
            return fail('备份记录格式错误，未导入任何记录')
        item_id = value.get('id') or str(uuid.uuid4())
        try:
            uuid.UUID(item_id)
        except (ValueError, TypeError, AttributeError):
            return fail('备份中的记录标识无效')
        item = normalize(value, item_id)
        binding = value.get('re0')
        if binding:
            if not isinstance(binding, dict) or not item.get('tmdb') or binding.get('source_id') != item['active_source_id']:
                raise ValueError('备份中的 RE0 来源绑定无效')
            slug = text(binding.get('slug', ''), 'RE0 条目', 200)
            uploader = text(binding.get('uploader', ''), 'RE0 发布者', 200)
            if not re.fullmatch(r'[\w-]{1,200}', slug) or not uploader:
                raise ValueError('备份中的 RE0 条目无效')
            item['re0'] = dict(slug=slug, uploader=uploader, source_id=item['active_source_id'],
                               checked_at='', error='', title=text(binding.get('title', ''), 'RE0 标题', 500),
                               remark=text(binding.get('remark', ''), 'RE0 说明', 2000), size=text(binding.get('size', ''), 'RE0 大小', 100))
        imported_events = value.get('events', [])
        if isinstance(imported_events, list):
            item['events'] = [dict(at=text(entry.get('at', ''), '流程时间', 50),
                                   action=text(entry.get('action', ''), '流程操作', 100),
                                   detail=text(entry.get('detail', ''), '流程说明', 1000))
                              for entry in imported_events[-100:] if isinstance(entry, dict)]
        for field in ('created_at', 'updated_at'):
            if value.get(field):
                try:
                    restored_time = dt.datetime.fromisoformat(value[field])
                    if restored_time.tzinfo is None:
                        restored_time = restored_time.replace(tzinfo=TZ)
                    item[field] = restored_time.astimezone(TZ).isoformat(timespec='seconds')
                except (ValueError, TypeError):
                    return fail('备份中的记录时间无效，未导入任何记录')
        deleted = now() if value.get('deleted_at') else None
        normalized.append((item_id, json.dumps(item, ensure_ascii=False), deleted))
    imported = 0
    with database() as db:
        for record in normalized:
            changed = db.execute('INSERT OR IGNORE INTO items VALUES (?,?,?)', record).rowcount
            imported += changed
    return jsonify(imported=imported, skipped=len(normalized) - imported)


CONFIG_LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
SYNC_STATE_LOCK = threading.Lock()
SYNC_STATE = {'running': False, 'pending': set()}
SOURCE_REFRESH_LOCK = threading.Lock()
SOURCE_REFRESH_STATE = {'running': False, 'pending': set(), 'last_result_at': ''}


def source_scan_identity(item):
    source = next((source for source in item.get('sources', []) if source['id'] == item.get('active_source_id')), None)
    if not source or not source.get('share_url'):
        return None
    return (source['id'], source['share_url'], source.get('code', ''), item.get('season', 1),
            (item.get('tmdb') or {}).get('id'), item.get('kind'))


def refresh_source_episodes(item_id):
    """Read outside SQLite transactions and discard results if the source or manual input changed."""
    with database() as db:
        snapshot = get_item(db, item_id)
    identity = source_scan_identity(snapshot) if snapshot else None
    if not identity or not snapshot.get('tracking', True):
        return
    source = next(source for source in snapshot['sources'] if source['id'] == identity[0])
    meta = snapshot.get('tmdb') or {}
    try:
        result = read_share_episodes(source['share_url'], source.get('code', ''), season=snapshot['season'],
                                     tmdb_id=meta.get('id'), air_dates=meta.get('air_dates', {}), wait=50,
                                     media_type='movie' if snapshot['kind'] == 'movie' else 'tv')
    except IntegrationError as error:
        result = dict(state='error', checked_at=now(), message=str(error))
    except Exception:
        result = dict(state='error', checked_at=now(), message='本次目录识别未完成，上次集数已保留')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = get_item(db, item_id)
        if not current or not current.get('tracking', True) or source_scan_identity(current) != identity:
            return
        selected = next(source for source in current['sources'] if source['id'] == identity[0])
        if selected.get('episodes', '') != source.get('episodes', ''):
            return
        scan = dict(state=result['state'], checked_at=result['checked_at'],
                    last_success_at=(selected.get('episode_scan') or {}).get('last_success_at', ''))
        if result['state'] == 'complete' and result.get('episode_count', 0) > 0:
            recognized = format_episodes(parse_episodes(result['episodes']))
            scan['last_success_at'] = result['checked_at']
            if selected['episodes'] != recognized:
                selected['episodes'] = recognized
                event(current, 'source_episodes', '网盘来源已检测到电影正片' if current['kind'] == 'movie' else '网盘来源集数已更新：' + recognized)
        elif result['state'] == 'partial':
            scan['message'] = f"有 {result.get('unrecognized_count', 0)} 个视频集号不明确，上次集数已保留"
        else:
            scan['message'] = result.get('message') or ('未检测到电影正片，上次记录已保留' if current['kind'] == 'movie' else '未识别到当前季的正片集号，上次集数已保留')
        selected['episode_scan'] = scan
        current['updated_at'] = now()
        db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), item_id))
    with SOURCE_REFRESH_LOCK:
        SOURCE_REFRESH_STATE['last_result_at'] = now()


def source_refresh_worker():
    while True:
        with SOURCE_REFRESH_LOCK:
            if not SOURCE_REFRESH_STATE['pending']:
                SOURCE_REFRESH_STATE['running'] = False
                return
            item_id = SOURCE_REFRESH_STATE['pending'].pop()
        try:
            refresh_source_episodes(item_id)
        except Exception:
            pass  # A deleted record or temporary database failure must not stop later refreshes.


def start_source_refresh(item_id):
    with database() as db:
        item = get_item(db, item_id)
    if not item or not item.get('tracking', True) or not source_scan_identity(item):
        return False
    with SOURCE_REFRESH_LOCK:
        SOURCE_REFRESH_STATE['pending'].add(item_id)
        if SOURCE_REFRESH_STATE['running']:
            return True
        SOURCE_REFRESH_STATE['running'] = True
    threading.Thread(target=source_refresh_worker, daemon=True, name='source-episodes').start()
    return True
CIPHER = AESGCM(hashlib.sha256(('shiying-integrations-v1:' + SECRET).encode()).digest())


def encrypt_secret(value):
    nonce = secrets.token_bytes(12)
    return base64.urlsafe_b64encode(nonce + CIPHER.encrypt(nonce, value.encode(), b'shiying-config-v1')).decode()


def decrypt_secret(value):
    if not value:
        return ''
    try:
        raw = base64.urlsafe_b64decode(value)
        return CIPHER.decrypt(raw[:12], raw[12:], b'shiying-config-v1').decode()
    except Exception:
        raise IntegrationError('连接凭据无法解密，请重新填写；迁移时请一并保留原 SECRET_KEY') from None


MAIL_WORKER_LOCK = threading.Lock()
MAIL_WORKER_RUNNING = False
MAIL_CONFIG_LOCK = threading.RLock()


def notification_config(db=None):
    if db is None:
        value = setting('notification_config')
    else:
        row = db.execute("SELECT value FROM settings WHERE key='notification_config'").fetchone()
        value = row['value'] if row else ''
    return {**MAIL_DEFAULTS, **(json.loads(value) if value else {})}


def public_notification_status():
    config = notification_config()
    try:
        require_mail_ready(config)
        configured = True
    except ValueError:
        configured = False
    status = json.loads(setting('notification_delivery_status') or '{}')
    with database() as db:
        counts = {row['state']: row['count'] for row in db.execute(
            'SELECT state, count(*) AS count FROM email_outbox GROUP BY state')}
    return dict(**{key: config.get(key, default) for key, default in MAIL_DEFAULTS.items()},
                configured=configured, has_password=bool(config.get('smtp_password')),
                pending_count=sum(counts.get(key, 0) for key in ('pending', 'retry', 'sending')),
                failed_count=counts.get('failed', 0), **status)


def enqueue_request_email(db, item, actor, added_request):
    # Use the same transaction as the saved request. A duplicate click creates no job.
    config = notification_config(db)
    if not added_request or actor.get('role') != 'user' or not config.get('enabled'):
        return False
    payload = dict(title=item['title'], kind=item['kind'], season=item['season'], year=item.get('year'),
                   username=actor['username'], submitted_at=now(),
                   tmdb={key: (item.get('tmdb') or {}).get(key) for key in ('id', 'media_type')})
    timestamp = now()
    db.execute('INSERT INTO email_outbox (id,payload,recipient,created_at,updated_at) VALUES (?,?,?,?,?)',
               (str(uuid.uuid4()), json.dumps(payload, ensure_ascii=False), config['to_email'], timestamp, timestamp))
    return True


def record_mail_result(db, *, sent=False, error='', kind='request'):
    row = db.execute("SELECT value FROM settings WHERE key='notification_delivery_status'").fetchone()
    status = json.loads(row['value']) if row else {}
    status.update(last_attempt_at=now(), last_error=error)
    if sent:
        status.update(last_sent_at=now(), last_sent_kind=kind)
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
               ('notification_delivery_status', json.dumps(status)))


def deliver_mail(config, payload, recipient, message_id):
    try:
        send_notification(config, decrypt_secret(config.get('smtp_password')), payload, recipient, message_id)
    except MailError:
        raise
    except Exception:
        # Never return server replies or credentials in error messages.
        raise MailError('发信未完成，请检查邮件设置后重试') from None


def deliver_next_email():
    with MAIL_CONFIG_LOCK, database() as db:
        db.execute('BEGIN IMMEDIATE')
        config = notification_config(db)
        if not config.get('enabled'):
            return False
        clock = time.time()
        db.execute("UPDATE email_outbox SET state=CASE WHEN attempts>=4 THEN 'failed' ELSE 'retry' END, "
                   "last_error='发送任务中断，等待重试', lease_until=0 WHERE state='sending' AND lease_until<?", (clock,))
        row = db.execute("SELECT * FROM email_outbox WHERE state IN ('pending','retry') AND next_attempt<=? "
                         "ORDER BY created_at,id LIMIT 1", (clock,)).fetchone()
        if not row:
            return False
        if row['recipient'] != config.get('to_email'):
            db.execute("UPDATE email_outbox SET state='cancelled', updated_at=? WHERE id=?", (now(), row['id']))
            return True
        attempt = row['attempts'] + 1
        db.execute("UPDATE email_outbox SET state='sending', attempts=?, lease_until=?, updated_at=? WHERE id=?",
                   (attempt, clock + 180, now(), row['id']))
    failure = ''
    try:
        deliver_mail(config, json.loads(row['payload']), row['recipient'], row['id'])
    except MailError as error:
        failure = str(error)
    with database() as db:
        db.execute("UPDATE email_outbox SET state=?, next_attempt=?, lease_until=0, updated_at=?, "
                   "last_error=?, sent_at=? WHERE id=? AND state='sending'", (
                       ('failed' if attempt >= 4 else 'retry') if failure else 'sent',
                       time.time() + min(1800, 60 * 5 ** (attempt - 1)) if failure else 0,
                       now(), failure, '' if failure else now(), row['id']))
        record_mail_result(db, sent=not failure, error=failure)
    return True


def email_delivery_worker():
    global MAIL_WORKER_RUNNING
    try:
        for _ in range(50):
            if not deliver_next_email():
                break
    except Exception:
        pass  # Durable jobs are reclaimed after the lease; requests are already saved.
    finally:
        with MAIL_WORKER_LOCK:
            MAIL_WORKER_RUNNING = False


def start_email_delivery():
    global MAIL_WORKER_RUNNING
    if not notification_config().get('enabled'):
        return False
    with MAIL_WORKER_LOCK:
        if MAIL_WORKER_RUNNING:
            return False
        MAIL_WORKER_RUNNING = True
    threading.Thread(target=email_delivery_worker, daemon=True, name='request-email').start()
    return True


@app.get('/api/notifications')
def notifications_status():
    return jsonify(public_notification_status())


@app.put('/api/notifications')
def notifications_update():
    with MAIL_CONFIG_LOCK, database() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = notification_config(db)
        config = validate_mail_config(body(), previous, encrypt_secret)
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('notification_config', json.dumps(config)))
        if not config['enabled'] or config['to_email'] != previous.get('to_email'):
            db.execute("UPDATE email_outbox SET state='cancelled', updated_at=? WHERE state IN ('pending','retry','failed')", (now(),))
    start_email_delivery()
    return jsonify(public_notification_status())


@app.post('/api/notifications/test')
def notifications_test():
    config = notification_config()
    require_mail_ready(config)
    try:
        deliver_mail(config, {'test': True}, config['to_email'], str(uuid.uuid4()))
    except MailError as error:
        with database() as db:
            record_mail_result(db, error=str(error), kind='test')
        raise
    with database() as db:
        record_mail_result(db, sent=True, kind='test')
    return jsonify(ok=True, message='测试邮件已交给发信服务器，请检查收件箱或垃圾邮件。', **public_notification_status())


@app.post('/api/notifications/retry')
def notifications_retry():
    if not notification_config().get('enabled'):
        raise ValueError('请先开启邮件通知')
    with database() as db:
        db.execute("UPDATE email_outbox SET state='pending', attempts=0, next_attempt=0, updated_at=? "
                   "WHERE state IN ('failed','retry')", (now(),))
    start_email_delivery()
    return jsonify(public_notification_status())


def integration_config():
    value = setting('integration_config')
    return json.loads(value) if value else {'auto_sync': True, 'interval_minutes': 30, 'emby_url': ''}


def persist_config(config):
    with database() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('integration_config', json.dumps(config)))


def public_integration_status():
    config = integration_config()
    query_url, query_token = re0_connection(config)
    with SYNC_STATE_LOCK:
        running = SYNC_STATE['running']
    with SOURCE_REFRESH_LOCK:
        source_running = SOURCE_REFRESH_STATE['running']
        source_scan_at = SOURCE_REFRESH_STATE['last_result_at']
    return dict(tmdb_configured=bool(config.get('tmdb_credential')), emby_configured=bool(
        config.get('emby_url') and config.get('emby_api_key')), emby_url=config.get('emby_url', ''),
        auto_sync=config.get('auto_sync', True), interval_minutes=config.get('interval_minutes', 30),
        cms_url=config.get('cms_url', ''), cms_configured=bool(config.get('cms_url') and config.get('cms_token')),
        re0_gateway_url=config.get('re0_gateway_url', ''),
        re0_via_cms=bool(config.get('re0_via_cms')),
        re0_configured=bool(query_url and query_token),
        last_sync=config.get('last_sync'), last_error=config.get('last_error', ''), sync_running=running,
        source_scan_running=source_running, source_scan_at=source_scan_at)


def tmdb_client(config=None):
    config = config if config is not None else integration_config()
    return TMDBClient(decrypt_secret(config.get('tmdb_credential')))


def emby_client(config=None):
    config = config if config is not None else integration_config()
    return EmbyClient(config.get('emby_url', ''), decrypt_secret(config.get('emby_api_key')))


@app.get('/api/integrations')
def integrations_status():
    return jsonify(public_integration_status())


def re0_connection(config):
    if config.get('re0_via_cms'):
        return config.get('cms_url', ''), config.get('cms_token', '')
    return config.get('re0_gateway_url', ''), config.get('re0_gateway_token', '')


def cms_client(config=None):
    config = config if config is not None else integration_config()
    url, token = re0_connection(config)
    return CMSClient(url, decrypt_secret(token))


def executor_client(config=None):
    config = config if config is not None else integration_config()
    return CMSExecutor(config.get('cms_url', ''), decrypt_secret(config.get('cms_token')),
                       native_re0=bool(config.get('re0_via_cms')))


CMS_DELIVERY_LOCK = threading.RLock()


def cms_payload(item, deleted=False):
    binding = item.get('re0') or item.get('cms_delivery') or {}
    meta = item.get('tmdb') or {}
    return dict(item_id=item['id'], media_type=meta.get('media_type'), tmdb_id=meta.get('id'),
        title=item['title'], year=item.get('year'), season=item['season'], poster_path=meta.get('poster_path', ''),
        slug=binding.get('slug', ''), uploader=binding.get('uploader', ''),
        active=bool(item.get('re0') and item.get('tracking', True) and not deleted))


def cms_fingerprint(payload, config):
    fields = dict(payload, cms_url=config.get('cms_url', ''), cms_token=config.get('cms_token', ''),
                  re0_gateway_url=re0_connection(config)[0], re0_via_cms=bool(config.get('re0_via_cms')))
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sync_cms_item(item_id, force=False):
    """Idempotent reconciliation; failure never rolls back a saved RE0 binding.

    Serial delivery plus receiver revisions prevent late HTTP requests from
    restoring an older subscription. Re-read after a concurrent edit/unbind.
    """
    with CMS_DELIVERY_LOCK:
        for _ in range(3):
            with database() as db:
                record = db.execute('SELECT payload, deleted_at FROM items WHERE id=?', (item_id,)).fetchone()
            if not record:
                return
            item = json.loads(record['payload'])
            previous = item.get('cms_delivery') or {}
            if not item.get('tmdb') or (not item.get('re0') and not previous):
                return
            config = integration_config()
            payload = cms_payload(item, bool(record['deleted_at']))
            fingerprint = cms_fingerprint(payload, config)
            if not force and previous.get('fingerprint') == fingerprint and previous.get('state') in ('submitted', 'paused'):
                return
            failure, result = '', {}
            try:
                result = executor_client(config).subscribe(dict(payload, revision=time.time_ns(),
                    expected_gateway_url=re0_connection(config)[0]))
            except IntegrationError as error:
                failure = str(error)
            except Exception:
                failure = 'CMS 下发未完成，请稍后重试'
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT payload, deleted_at FROM items WHERE id=?', (item_id,)).fetchone()
                if not row:
                    return
                current = json.loads(row['payload'])
                if cms_fingerprint(cms_payload(current, bool(row['deleted_at'])), integration_config()) != fingerprint:
                    force = False
                    continue
                state = 'error' if failure else 'submitted' if payload['active'] else 'paused'
                current['cms_delivery'] = dict(state=state, error=failure, checked_at=now(), fingerprint=fingerprint,
                    address=config.get('cms_url', ''), slug=payload['slug'], uploader=payload['uploader'],
                    active=payload['active'], subscription_id=str(result.get('subscription_id') or previous.get('subscription_id') or ''),
                    native_status=result.get('native_status'))
                if state != previous.get('state') or fingerprint != previous.get('fingerprint'):
                    event(current, 'cms', 'CMS 下发失败，待重试' if failure else
                          ('已下发 CMS：固定 ' + payload['uploader']) if payload['active'] else '已暂停该记录的 CMS 追更')
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), item_id))
                return


@app.post('/api/items/<item_id>/cms')
def cms_retry(item_id):
    with database() as db:
        item = get_item(db, item_id, True)
    if not item or not item.get('tmdb'):
        return fail('追更记录不存在', 404)
    sync_cms_item(item_id, force=True)
    with database() as db:
        item = get_item(db, item_id, True)
    return jsonify(item=decorate(item))


def run_cms_item(item_id, start=False):
    """Manual execution only; background reconciliation never submits a transfer."""
    with CMS_DELIVERY_LOCK:
        with database() as db:
            item = get_item(db, item_id)
        if not item or not item.get('re0') or not item.get('tracking', True):
            raise ValueError('请先绑定来源并开启追更')
        payload = cms_payload(item)
        config = integration_config()
        fingerprint = cms_fingerprint(payload, config)
        previous = item.get('cms_run') or {}
        result = None
        if start:
            if ((item.get('cms_delivery') or {}).get('state') != 'submitted' or
                    (item.get('cms_delivery') or {}).get('fingerprint') != fingerprint):
                return  # A failed subscription must be retried before immediate execution.
            # Retries after an uncertain HTTP response reuse the same idempotency key.
            request_id = previous.get('request_id') if previous.get('state') in ('queued', 'running', 'unconfirmed') and previous.get('fingerprint') == fingerprint else str(uuid.uuid4())
            result = dict(request_id=request_id, state='unconfirmed', message='正在确认 CMS 是否已接收执行请求',
                          requested_at=now(), updated_at=now(), fingerprint=fingerprint)
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                current = get_item(db, item_id)
                if not current or cms_fingerprint(cms_payload(current), integration_config()) != fingerprint:
                    return
                current['cms_run'] = result
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), item_id))
            payload['request_id'] = request_id
        try:
            run = executor_client(config).run(payload, start=start)
            if run:
                result = dict(run, fingerprint=fingerprint)
            elif not start and previous.get('state') == 'unconfirmed':
                result = dict(previous, state='error', message='CMS 未找到执行记录，可以重试立即入库', updated_at=now())
        except IntegrationError as error:
            if start:
                result['message'] = str(error) + '；请先刷新执行状态，避免重复提交'
            else:
                raise
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            current = get_item(db, item_id)
            if not current or cms_fingerprint(cms_payload(current), integration_config()) != fingerprint:
                return
            if result:
                current['cms_run'] = result
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), item_id))


@app.route('/api/items/<item_id>/cms/run', methods=['GET', 'POST'])
def cms_run(item_id):
    if request.method == 'POST':
        # A retry also reconciles a previously failed subscription before executing.
        sync_cms_item(item_id)
    run_cms_item(item_id, start=request.method == 'POST')
    with database() as db:
        item = get_item(db, item_id)
    return jsonify(item=decorate(item))


# Reachability is independent of saved configuration and of per-resource sync failures.
CONNECTION_STATUS_LOCK = threading.Lock()
CONNECTION_STATUS = dict(signature='', checking=False, updated_monotonic=0, services=[])
CONNECTION_STATUS_TTL = 60


def connection_signature(config):
    fields = ('tmdb_credential', 'emby_url', 'emby_api_key', 'cms_url', 'cms_token', 're0_gateway_url', 're0_gateway_token', 're0_via_cms')
    return hashlib.sha256(json.dumps({key: config.get(key, '') for key in fields}, sort_keys=True).encode()).hexdigest()


def connection_services(config):
    def address(value):
        # Configured URLs have no userinfo/query; validate imported legacy values as well.
        try:
            return valid_emby_url(value) if value else ''
        except IntegrationError:
            return ''
    services = [dict(id='emby', name='Emby', address=address(config.get('emby_url', '')),
                 configured=bool(config.get('emby_url') and config.get('emby_api_key')),
                 route='拾影 → Emby', purpose='媒体库读取与入库比对'),
            dict(id='tmdb', name='TMDB', address='https://api.themoviedb.org/3',
                 configured=bool(config.get('tmdb_credential')),
                 route='拾影 → TMDB', purpose='搜索作品与读取播出排期'),
            dict(id='re0', name='RE0 查询端', address=address(config.get('re0_gateway_url', '')),
                 configured=bool(config.get('re0_gateway_url') and config.get('re0_gateway_token')),
                 route='拾影 → 已授权 CMS → RE0', purpose='仅查询与解锁选定来源，不在查询端创建下载任务'),
            dict(id='cms', name='CMS 执行端', address=address(config.get('cms_url', '')),
                 configured=bool(config.get('cms_url') and config.get('cms_token')),
                 route='拾影 → 我的 CMS → 115', purpose='接收订阅与固定发布者，复用查询端检查来源并执行转存')]
    if config.get('re0_via_cms'):
        services = [service for service in services if service['id'] != 're0']
        services[-1].update(name='CloudMediaSync', route='拾影 → 我的 CMS → RE0 / 115',
                            purpose='使用本机 RE0 授权查询、解锁、固定发布者追更并执行转存')
    return services


def probe_connection(service, config):
    result = dict(service, checked_at=now())
    if not service['configured']:
        return dict(result, state='unconfigured', message='请在设置中填写地址与凭据' if service['id'] != 'tmdb' else '请在设置中填写 TMDB 凭据')
    try:
        factory = {'tmdb': tmdb_client, 'emby': emby_client, 're0': cms_client, 'cms': executor_client}[service['id']]
        factory(config).test()
        message = {'emby': '系统 API 可达，读取凭据有效', 'tmdb': '官方 API 可达，读取凭据有效',
                   're0': 'CMS 连接正常，RE0 授权有效', 'cms': 'CMS 连接正常，固定来源订阅与转存服务已就绪'}[service['id']]
        return dict(result, state='connected', message=message, checked_at=now())
    except IntegrationError as error:
        return dict(result, state='error', message=str(error)[:300], checked_at=now())
    except Exception:
        # Never return raw exceptions, which may include URLs or credentials.
        return dict(result, state='error', message='连接检查失败，请稍后重试', checked_at=now())


def check_connections(config, signature):
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda service: probe_connection(service, config), connection_services(config)))
        with CONNECTION_STATUS_LOCK:
            if CONNECTION_STATUS['signature'] == signature:
                CONNECTION_STATUS.update(services=results, checking=False, updated_monotonic=time.monotonic())
    finally:
        with CONNECTION_STATUS_LOCK:
            if CONNECTION_STATUS['signature'] == signature:
                CONNECTION_STATUS['checking'] = False


def current_connections(force=False):
    config = integration_config()
    signature = connection_signature(config)
    launch = False
    with CONNECTION_STATUS_LOCK:
        if signature != CONNECTION_STATUS['signature']:
            services = [dict(service, state='unknown' if service['configured'] else 'unconfigured',
                             message='', checked_at='') for service in connection_services(config)]
            CONNECTION_STATUS.update(signature=signature, checking=False, updated_monotonic=0, services=services)
        expired = not CONNECTION_STATUS['updated_monotonic'] or time.monotonic() - CONNECTION_STATUS['updated_monotonic'] >= CONNECTION_STATUS_TTL
        if not CONNECTION_STATUS['checking'] and (force or expired):
            CONNECTION_STATUS['checking'] = True
            launch = True
        result = dict(checking=CONNECTION_STATUS['checking'], services=copy.deepcopy(CONNECTION_STATUS['services']))
    if launch:
        threading.Thread(target=check_connections, args=(config, signature), daemon=True).start()
    return result


@app.get('/api/connections')
def connections_status():
    return jsonify(current_connections())


@app.post('/api/connections/check')
def connections_check():
    return jsonify(current_connections(force=True))


def apply_re0(item, row):
    binding = item.get('re0') or {}
    source = next((s for s in item['sources'] if s['id'] == binding.get('source_id')), None)
    if source is None:
        if len(item['sources']) >= 50:
            raise ValueError('来源已达 50 个，请先整理历史来源')
        source = dict(id=str(uuid.uuid4()), episodes='', broken=False, note='')
        item['sources'].append(source)
    version = ' · '.join(filter(None, [row['title'], row['resolution'], row['source']]))[:500]
    values = dict(sharer=row['uploader'], post_url=row['resource_url'], share_url=row['share_url'], code=row['code'],
                  version=version, broken=False)
    changed = any(source.get(k) != v for k, v in values.items())
    source.update(values)
    item['active_source_id'] = source['id']
    item['re0'] = dict(slug=row['slug'], uploader=row['uploader'], source_id=source['id'],
                       checked_at=now(), error='', title=row['title'], remark=row['remark'], size=row['size'])
    if changed:
        event(item, 'source', 'RE0 来源已更新：' + row['uploader'] + ' · ' + row['title'])
    item['updated_at'] = now()


def re0_item(item_id):
    with database() as db:
        item = get_item(db, item_id)
    if not item or not item.get('tmdb'):
        raise ValueError('请先通过 TMDB 添加该作品')
    return item


@app.get('/api/items/<item_id>/re0')
def re0_candidates(item_id):
    item = re0_item(item_id)
    return jsonify(results=cms_client().candidates(item), season=item['season'])


@app.post('/api/items/<item_id>/re0')
def re0_bind(item_id):
    data = body()
    target = re0_item(item_id)
    slug = text(data.get('slug', ''), 'RE0 条目', 200)
    uploader = text(data.get('uploader', ''), '发布者', 200)
    # Resolve from fresh CMS metadata; never trust client-supplied URLs or prices.
    max_points = data.get('max_points')
    if max_points is not None and (isinstance(max_points, bool) or not isinstance(max_points, (int, float)) or
                                   not math.isfinite(max_points) or not 0 <= max_points <= 10**9):
        raise ValueError('解锁积分无效，请刷新来源列表')
    client = cms_client()
    row = client.resolve(target, slug, uploader, max_points=max_points) if max_points is not None else client.resolve(target, slug, uploader)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = get_item(db, item_id)
        if not current or current.get('tmdb') != target.get('tmdb') or current['season'] != target['season'] or current.get('re0') != target.get('re0') or current['active_source_id'] != target['active_source_id']:
            return fail('作品或来源刚刚发生变化，请刷新后重试', 409)
        if not current.get('re0') or current['re0'].get('slug') != slug or current['re0'].get('uploader') != uploader:
            current.pop('re0', None)  # A new binding creates a source; preserve the previous one.
        apply_re0(current, row)
        db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), item_id))
    start_source_refresh(item_id)
    sync_cms_item(item_id)
    if current.get('tracking', True):
        run_cms_item(item_id, start=True)
    with database() as db:
        current = get_item(db, item_id)
    return jsonify(item=decorate(current))


@app.delete('/api/items/<item_id>/re0')
def re0_unbind(item_id):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        item = get_item(db, item_id)
        if not item:
            return fail('作品不存在', 404)
        if item.pop('re0', None):
            event(item, 'source', '已停止自动检查 RE0，保留历史来源与链接')
            item['updated_at'] = now()
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item_id))
    sync_cms_item(item_id)
    with database() as db:
        item = get_item(db, item_id)
    return jsonify(item=decorate(item))


@app.get('/api/users')
def emby_users():
    config = integration_config()
    users = emby_client(config).users()
    with database() as db:
        rows = db.execute('SELECT payload FROM items WHERE deleted_at IS NULL').fetchall()
    records = [json.loads(row['payload']) for row in rows]
    for user in users:
        user['requests'] = sum(any(entry.get('provider') == 'emby' and entry['user_id'] == 'emby:' + user['server_id'] + ':' + user['id']
                                  and entry.get('active', True) for entry in item.get('requesters', [])) for item in records)
    return jsonify(users=users)


@app.put('/api/integrations')
def integrations_update():
    data = body()
    with CONFIG_LOCK:
        config = integration_config()
        for field in ('tmdb_credential', 'emby_api_key', 'cms_token', 're0_gateway_token'):
            value = text(data.get(field, ''), 'API 凭据', 4096)
            if any(character.isspace() for character in value):
                raise ValueError('API 凭据不能包含空白字符')
            if value:
                config[field] = encrypt_secret(value)
        if 'emby_url' in data:
            value = text(data['emby_url'], 'EMBY 地址', 2000)
            config['emby_url'] = valid_emby_url(value) if value else ''
        if 'cms_url' in data:
            value = text(data['cms_url'], 'CMS 地址', 2000)
            if value.rstrip('/') != config.get('cms_url', '').rstrip('/'):
                with database() as db:
                    deliveries = [json.loads(row['payload']).get('cms_delivery') or {} for row in db.execute('SELECT payload FROM items')]
                    active = any(d.get('state') == 'submitted' or (d.get('state') == 'error' and d.get('subscription_id')) for d in deliveries)
                if active:
                    raise ValueError('请先暂停已下发的 CMS 追更，再更换执行端地址')
                if not data.get('cms_token'):
                    config.pop('cms_token', None)
            config['cms_url'] = valid_emby_url(value) if value else ''
        if 're0_gateway_url' in data:
            value = text(data['re0_gateway_url'], 'RE0 查询端地址', 2000)
            if value.rstrip('/') != config.get('re0_gateway_url', '').rstrip('/') and not data.get('re0_gateway_token'):
                config.pop('re0_gateway_token', None)
            config['re0_gateway_url'] = valid_emby_url(value) if value else ''
        if 'auto_sync' in data:
            config['auto_sync'] = boolean(data['auto_sync'], '自动同步')
        if 'interval_minutes' in data:
            config['interval_minutes'] = integer(data['interval_minutes'], '同步间隔', 15, 1440, False)
        config['last_error'] = ''
        persist_config(config)
    return jsonify(public_integration_status())


@app.post('/api/integrations/test')
def integrations_test():
    target = body().get('target')
    if target == 'tmdb':
        message = tmdb_client().test()
    elif target == 'emby':
        message = emby_client().test()
    elif target == 're0':
        message = cms_client().test()
    elif target == 'cms':
        message = executor_client().test()
    else:
        return fail('请选择要测试的服务')
    return jsonify(ok=True, message=message)


@app.get('/api/tmdb/search')
def tmdb_search():
    query = text(request.args.get('q', ''), '搜索词', 200)
    if not query:
        return jsonify(results=[], page=1, total_pages=0)
    page = integer(request.args.get('page', 1), '搜索页码', 1, 500, False)
    return jsonify(tmdb_client().search(query, page))


@app.get('/api/tmdb/details/<media_type>/<int:tmdb_id>')
def tmdb_details(media_type, tmdb_id):
    return jsonify(tmdb_client().details(media_type, tmdb_id))


@app.get('/api/tmdb/poster')
def tmdb_poster():
    path = poster_path(request.args.get('path', ''))
    if not path:
        return fail('海报地址无效', 404)
    image_data, mime = fetch('https://image.tmdb.org/t/p/w185' + path, binary=True, maximum=2 * 1024 * 1024)
    return Response(image_data, mimetype=mime)


@app.post('/api/tmdb/track')
def tmdb_track():
    data = body()
    media_type = data.get('media_type')
    tmdb_id = integer(data.get('tmdb_id'), 'TMDB ID', 1, 999999999999, False)
    season = 1 if media_type == 'movie' else integer(data.get('season', 1), '季数', 1, 1000, False)
    metadata = tmdb_client().tracking_metadata(media_type, tmdb_id, season)
    item = normalize(dict(title=metadata['title'][:150], kind='movie' if media_type == 'movie' else 'tv',
                          year=metadata['year'], season=season, total_episodes=metadata['total_episodes'],
                          finished=metadata['status'] in ('Ended', 'Canceled', 'Released'),
                          next_check=(data.get('next_check') or '') if is_admin() else ''))
    metadata.update(synced_at=now(), metadata_error='')
    item['tmdb'] = normalize_tmdb(metadata)
    item['tracking'] = True
    event(item, 'tracked', '已确认 TMDB 作品' + ('与第 %s 季' % season if media_type == 'tv' else '') + '，加入追更')
    join_request(item)
    already_exists = False
    added_request = True
    queued_email = False
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT payload FROM items WHERE deleted_at IS NULL').fetchall()
        for row in rows:
            existing = json.loads(row['payload'])
            meta = existing.get('tmdb') or {}
            if meta.get('id') == tmdb_id and meta.get('media_type') == media_type and existing['season'] == season:
                added_request = join_request(existing)
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(existing, ensure_ascii=False), existing['id']))
                item = existing
                already_exists = True
                break
        else:
            db.execute('INSERT INTO items VALUES (?,?,NULL)', (item['id'], json.dumps(item, ensure_ascii=False)))
        queued_email = enqueue_request_email(db, item, principal(), added_request)
    if queued_email:
        start_email_delivery()
    # The queued comparison runs separately from the search request.
    if item.get('tracking', True) and public_integration_status()['emby_configured']:
        start_sync(item['id'])
    return jsonify(item=item_for_user(item), already_exists=already_exists, added_request=added_request), 200 if already_exists else 201


def run_sync(item_id=None):
    """Network work is outside transactions; merge only authoritative sync fields."""
    failures = []
    try:
        config = integration_config()
        tmdb = tmdb_client(config)
        emby = emby_client(config)
        with database() as db:
            rows = db.execute('SELECT payload FROM items WHERE deleted_at IS NULL').fetchall()
        targets = [json.loads(row['payload']) for row in rows]
        targets = [item for item in targets if item.get('tmdb') and item.get('tracking', True) and
                   (not item_id or item['id'] == item_id)]
        for target in targets:
            metadata = None
            metadata_error = ''
            comparison = None
            comparison_error = ''
            meta = target['tmdb']
            try:
                metadata = tmdb.tracking_metadata(meta['media_type'], meta['id'], target['season'])
            except IntegrationError as error:
                metadata_error = str(error)
                failures.append('TMDB：' + metadata_error)
            try:
                title_meta = metadata or meta
                comparison = emby.compare(meta['media_type'], meta['id'], target['season'],
                                          (target['title'], title_meta.get('original_title')))
            except IntegrationError as error:
                comparison_error = str(error)
                failures.append('EMBY：' + comparison_error)
            re0_result, re0_error = None, ''
            if target.get('re0') and all(re0_connection(config)):
                try:
                    re0_result = cms_client(config).resolve(target, target['re0']['slug'], target['re0']['uploader'])
                except IntegrationError as error:
                    re0_error = str(error)
                    failures.append('RE0：' + re0_error)
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                current = get_item(db, target['id'])
                if not current or not current.get('tracking', True) or current.get('tmdb', {}).get('id') != meta['id'] or current['season'] != target['season']:
                    continue
                if metadata:
                    metadata.update(synced_at=now(), metadata_error='')
                    current['tmdb'] = normalize_tmdb(metadata)
                    current['title'] = metadata['title'][:150]
                    current['year'] = metadata['year']
                    current['total_episodes'] = metadata['total_episodes']
                    current['finished'] = metadata['status'] in ('Ended', 'Canceled', 'Released')
                else:
                    current['tmdb']['metadata_error'] = metadata_error
                previous = current.get('emby') or {}
                if comparison:
                    if comparison['state'] == 'absent' and current['library_episodes']:
                        comparison.update(state='unmatched', error='库中未找到对应作品，但存在历史入库记录；已保留原进度，请核对是否移库或重新刮削')
                    values = set(comparison.pop('episodes'))
                    comparison['episodes'] = format_episodes(values)
                    current['emby'] = dict(comparison, checked_at=now())
                    if comparison['state'] == 'matched':
                        before = current['library_episodes']
                        current['library_episodes'] = comparison['episodes']
                        if before != current['library_episodes'] or previous.get('state') != 'matched':
                            event(current, 'emby_sync', 'EMBY 已匹配，实际入库：' + (current['library_episodes'] or '暂无已编号集数'))
                    elif comparison['state'] == 'absent':
                        if previous.get('state') != 'absent':
                            event(current, 'emby_absent', '已查询 Emby，库中未找到对应作品，等待入库')
                    elif previous.get('state') != 'unmatched':
                        event(current, 'emby_unmatched', '暂未匹配到 EMBY，保留原入库进度等待核对')
                else:
                    current['emby'] = dict(previous, state='error', checked_at=now(), error=comparison_error)
                    if previous.get('state') != 'error':
                        event(current, 'sync_error', 'Emby 入库比对失败，已保留上次进度。' + comparison_error)
                if target.get('re0') and current.get('re0') == target['re0'] and (re0_result or re0_error):
                    if re0_result:
                        apply_re0(current, re0_result)
                    else:
                        if not current['re0'].get('error'):
                            event(current, 'sync_error', 'RE0 检查失败，保留上次来源与链接')
                        current['re0'].update(error=re0_error, checked_at=now())
                current['updated_at'] = now()
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current, ensure_ascii=False), current['id']))
            # Read the saved active URL after RE0 refresh, independently of TMDB/Emby success.
            refresh_source_episodes(target['id'])
        # Manually maintained titles also refresh their active share on the same schedule.
        for target in [json.loads(row['payload']) for row in rows]:
            if not target.get('tmdb') and target.get('tracking', True) and (not item_id or target['id'] == item_id):
                refresh_source_episodes(target['id'])
        # Reconcile paused, unbound and trashed records too, including retries.
        with database() as db:
            deliveries = db.execute('SELECT id FROM items').fetchall()
        for row in deliveries:
            if not item_id or row['id'] == item_id:
                sync_cms_item(row['id'])
    except IntegrationError as error:
        failures.append(str(error))
    except Exception:
        # Do not log requests or credential-bearing URLs from external client errors.
        failures.append('本次同步未完成，请检查设置后重试；已有进度已保留')
    finally:
        try:
            with CONFIG_LOCK:
                config = integration_config()
                config.update(last_sync=now(), last_error='；'.join(dict.fromkeys(failures))[:1000], last_attempt=time.time())
                persist_config(config)
        finally:
            with SYNC_STATE_LOCK:
                pending = SYNC_STATE['pending']
                SYNC_STATE['pending'] = set()
                SYNC_STATE['running'] = False
                SYNC_LOCK.release()
            if pending:
                start_sync()  # Include records added while the previous snapshot was syncing.


def start_sync(item_id=None):
    with SYNC_STATE_LOCK:
        if not SYNC_LOCK.acquire(blocking=False):
            if item_id:
                SYNC_STATE['pending'].add(item_id)
            return False
        SYNC_STATE['running'] = True
    worker = threading.Thread(target=run_sync, args=(item_id,), daemon=True, name='media-sync')
    worker.start()
    return True


@app.post('/api/sync')
@app.post('/api/items/<item_id>/sync')
def sync_request(item_id=None):
    config = integration_config()
    if not config.get('tmdb_credential') or not config.get('emby_api_key') or not config.get('emby_url'):
        return fail('请先保存 TMDB 与 EMBY 连接设置')
    if item_id:
        with database() as db:
            item = get_item(db, item_id)
        if not item or not item.get('tmdb'):
            return fail('该作品尚未绑定 TMDB，请通过搜索添加追更', 400)
        if not item.get('tracking', True):
            return fail('该作品已暂停追更，请先开启自动追更')
    started = start_sync(item_id)
    return jsonify(started=started, running=True), 202


def scheduler_loop():
    while True:
        time.sleep(30)
        try:
            start_email_delivery()
        except Exception:
            pass
        try:
            config = integration_config()
            ready = all(config.get(key) for key in ('tmdb_credential', 'emby_api_key', 'emby_url'))
            if ready and config.get('auto_sync', True) and time.time() - config.get('last_attempt', 0) >= config.get('interval_minutes', 30) * 60:
                start_sync()
        except Exception:
            # The scheduler must not affect request serving or disclose secrets in logs.
            pass


if os.environ.get('ENABLE_SCHEDULER') == 'true':
    threading.Thread(target=scheduler_loop, daemon=True, name='media-scheduler').start()


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8787')), debug=False)
