"""9527 executor addon, inserted before _make_panso_middleware in its plugin.

Website-owned subscriptions use the selected query mode. Local mode reuses this
CMS's OAuth client directly. No OAuth tokens move and no remote fallback occurs.
"""
import hmac as _sx_hmac
import threading as _sx_threading
import time as _sx_time
from pathlib import Path as _SX_Path

_sx_lock = _sx_threading.RLock()
_SX_KEY = 'cms_shiying_subscriptions_v1'
_sx_transfer_lock = _sx_threading.Lock()
_sx_running_ids = set()


def _sx_established():
    return _SX_Path('/config/shiying-registry-established').exists()


def _sx_mark_established():
    marker = _SX_Path('/config/shiying-registry-established')
    marker.touch(mode=0o600, exist_ok=True)


def _sx_registry():
    value = _cms_config_get_json(_SX_KEY)
    if value is None and not _sx_established():
        return dict(entries={}, owners={})
    if not isinstance(value, dict) or not isinstance(value.get('entries'), dict) or not isinstance(value.get('owners'), dict):
        raise RuntimeError('拾影订阅规则无法读取，停止查源以防错误回退')
    return dict(entries=value['entries'], owners=value['owners'])


def _sx_save(value):
    if not _cms_config_set_json(_SX_KEY, value):
        raise RuntimeError('CMS 订阅规则保存失败')
    _sx_mark_established()


def _sx_gateway_config():
    value = json.loads(_SX_Path('/config/shiying-executor.json').read_text())
    from urllib.parse import urlsplit
    if value.get('query_mode') == 'local':
        parsed = urlsplit(value.get('public_gateway_url', ''))
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('CMS 本机查询地址无效')
        if not callable(globals().get('_shiying_operation')):
            raise RuntimeError('CMS 本机 RE0 查询模块未安装')
        return dict(value, gateway_url=value['public_gateway_url'])
    parsed = urlsplit(value['gateway_url'])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or len(value.get('gateway_token', '')) < 32:
        raise ValueError('查询端配置无效')
    return value


def _sx_guards_ready():
    try:
        from app.modules.tg.tg_scraper import TelegramScraper
        return bool(_sub_sync_patched and _sx_native_tuple is not None and
                    all(getattr(getattr(TelegramScraper, name), '_shiying_source_guard', False)
                        for name in ('search', 'search_all')))
    except Exception:
        return False


def _sx_gateway(action, payload=None):
    config = _sx_gateway_config()
    if config.get('query_mode') == 'local':
        if action not in ('status', 'search', 'resolve'):
            raise ValueError('RE0 操作无效')
        return _shiying_operation('/api/shiying/' + action, payload or {})
    from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = Request(config['gateway_url'].rstrip('/') + '/api/shiying/' + action,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Authorization': 'Bearer ' + config['gateway_token'], 'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({}), NoRedirect).open(request, timeout=65 if payload else 12) as response:
        raw = response.read(6 * 1024 * 1024 + 1)
    if len(raw) > 6 * 1024 * 1024:
        raise RuntimeError('查询端响应过大')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('success') is not True:
        raise RuntimeError('RE0 查询端不可用')
    return result


def _sx_media_key(value):
    return str(value.get('media_type') or value.get('type') or '') + ':' + str(value.get('tmdb_id') or '')


def _sx_rows(key):
    kind, tmdb_id = key.split(':')
    return _cms_find_submedia_rows_db(tmdb_id=tmdb_id, media_type=kind, status=None)


def _sx_result(registry, item_id):
    entry = registry['entries'].get(item_id)
    if not entry:
        return dict(success=True, data=dict(state='absent', active=False))
    key = _sx_media_key(entry)
    owner = registry['owners'].get(key)
    rows = _sx_rows(key)
    row = next((r for r in rows if str(r['id']) == str(owner)), None)
    return dict(success=True, data=dict(state='submitted' if row else 'missing',
        subscription_id=str(owner or ''), active=entry['active'], uploader=entry['uploader'],
        slug=entry['slug'], season=entry['season'], native_status=row.get('status') if row else None,
        scope='selected_source', revision=entry['revision'], run=_sx_run_status(entry)))


def _sx_run_status(entry):
    run = entry.get('run')
    if not isinstance(run, dict):
        return None
    result = {k: run.get(k) for k in ('request_id', 'state', 'message', 'requested_at', 'updated_at')}
    if run.get('state') in ('queued', 'running') and run.get('request_id') not in _sx_running_ids:
        result.update(state='unknown', message='CMS 已重启，执行结果待核对；请先查看转存记录再重试')
    return result


def _sx_same_source(entry, params):
    return all(entry.get(k) == params.get(k) for k in ('slug', 'uploader', 'season'))


def _sx_stamp():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _sx_transfer_url(row):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parsed = urlsplit(str(row.get('share_url') or ''))
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in
            ('115.com', 'www.115.com', '115cdn.com', 'www.115cdn.com', 'anxia.com', 'www.anxia.com') or
            parsed.username or parsed.password or len(parsed.geturl()) > 2000):
        raise ValueError('CMS 未返回有效的 115 分享链接')
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if row.get('code') and not any(k == 'password' and v for k, v in query):
        query = [(k, v) for k, v in query if k != 'password'] + [('password', str(row['code']))]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _sx_transfer(url):
    """Use CMS's normal transfer pipeline, including its auto-tidy setting."""
    import requests
    import os
    try:
        response = requests.post('http://127.0.0.1:9527/api/cloud/add_share_down_by_token',
            json=dict(url=url, token=os.environ.get('CMS_API_TOKEN', 'cloud_media_sync')), timeout=(5, 120))
        data = response.json()
        if response.status_code == 200 and data.get('code') == 200:
            return 'submitted', 'CMS 已接收转存任务，整理及实际入库请以 CMS 和 Emby 为准'
        message = str(data.get('msg') or data.get('message') or '')
        if '已存在' in message or '已添加' in message:
            return 'exists', 'CMS 已有该链接的任务或记录，请查看转存记录确认进度'
        if '登录' in message or response.status_code in (401, 403):
            return 'error', 'CMS 或 115 登录状态异常，请在 CMS 检查后重试'
        return 'error', 'CMS 未接受转存任务，请查看 CMS 转存记录与日志'
    except Exception:
        # A timeout after submission does not prove that CMS rejected the task.
        return 'unknown', 'CMS 转存请求结果未确认，请先核对转存记录再重试'


def _sx_run_worker(item_id, request_id):
    try:
        with _sx_transfer_lock:
            with _sx_lock:
                registry = _sx_registry()
                entry = registry['entries'][item_id]
                if (entry.get('run') or {}).get('request_id') != request_id:
                    return
                snapshot = dict(entry)
                entry['run'].update(state='running', updated_at=_sx_stamp())
                _sx_save(registry)
            try:
                row = _sx_gateway('resolve', dict(media_type=snapshot['media_type'], tmdb_id=snapshot['tmdb_id'],
                    slug=snapshot['slug'], uploader=snapshot['uploader']))['data']
                if not _sx_same_source(dict(row, season=snapshot['season']), snapshot):
                    raise ValueError('来源已变化')
                url = _sx_transfer_url(row)
                with _sx_lock:
                    registry = _sx_registry()
                    current = registry['entries'][item_id]
                    if (not current['active'] or not _sx_same_source(current, snapshot) or
                            (current.get('run') or {}).get('request_id') != request_id or not _sx_guards_ready()):
                        state, message = 'cancelled', '来源或追更状态已变化，本次执行已取消'
                    else:
                        state, message = 'ready', ''
                # Once admitted, this request is in flight; status/pause remain responsive.
                if state == 'ready':
                    state, message = _sx_transfer(url)
            except Exception:
                state, message = 'error', '固定来源读取失败，未提交转存；请检查 RE0 授权或稍后重试'
            with _sx_lock:
                registry = _sx_registry()
                entry = registry['entries'].get(item_id)
                if entry and (entry.get('run') or {}).get('request_id') == request_id:
                    entry['run'].update(state=state, message=message, updated_at=_sx_stamp())
                    _sx_save(registry)
    finally:
        with _sx_lock:
            _sx_running_ids.discard(request_id)


def _sx_start_run_worker(item_id, request_id):
    _sx_threading.Thread(target=_sx_run_worker, args=(item_id, request_id), daemon=True,
                         name='shiying-immediate-transfer').start()


def _sx_run_once(params):
    import re
    item_id, request_id = str(params.get('item_id') or ''), str(params.get('request_id') or '')
    if not re.fullmatch(r'[a-f0-9-]{36}', request_id):
        raise ValueError('执行请求标识无效')
    if not _sx_guards_ready():
        raise ValueError('固定来源保护未就绪，未执行转存')
    with _sx_lock:
        registry = _sx_registry()
        entry = registry['entries'].get(item_id)
        if not entry or not entry['active'] or not _sx_same_source(entry, params):
            raise ValueError('订阅未激活或来源已变化，请刷新后重试')
        previous = entry.get('run') or {}
        same_source = _sx_same_source(previous, entry)
        if previous.get('request_id') == request_id or (same_source and previous.get('request_id') in _sx_running_ids):
            return _sx_result(registry, item_id)
        if same_source and _sx_time.time() - previous.get('requested_ts', 0) < 60:
            raise ValueError('刚刚已执行过，请稍后刷新进度，避免重复提交')
        entry['run'] = dict(request_id=request_id, state='queued', message='已排队，立即执行当前固定来源',
                            requested_at=_sx_stamp(), updated_at=_sx_stamp(), requested_ts=_sx_time.time(),
                            slug=entry['slug'], uploader=entry['uploader'], season=entry['season'])
        _sx_save(registry)
        _sx_running_ids.add(request_id)
        try:
            _sx_start_run_worker(item_id, request_id)
        except Exception:
            _sx_running_ids.discard(request_id)
            entry['run'].update(state='error', message='执行线程未启动，请重试')
            _sx_save(registry)
        return _sx_result(registry, item_id)


def _sx_subscribe(params):
    import re as _sx_re
    item_id = str(params.get('item_id') or '')
    if not _sx_re.fullmatch(r'[a-f0-9-]{36}', item_id):
        raise ValueError('追更记录标识无效')
    if params.get('media_type') not in ('tv', 'movie') or not str(params.get('tmdb_id', '')).isdecimal() or not 0 < int(params['tmdb_id']) < 10**12:
        raise ValueError('作品标识无效')
    if type(params.get('active')) is not bool or type(params.get('revision')) is not int or params['revision'] < 1:
        raise ValueError('订阅状态无效')
    season = params.get('season')
    if type(season) is not int or not 1 <= season <= 1000:
        raise ValueError('季数无效')
    title, uploader, slug = (str(params.get(k) or '').strip() for k in ('title', 'uploader', 'slug'))
    if not title or len(title) > 150 or len(uploader) > 200 or (params['active'] and (not uploader or not _sx_re.fullmatch(r'[\w-]{1,200}', slug))):
        raise ValueError('标题或固定来源无效')
    config = _sx_gateway_config()  # Reject a mismatched gateway before creating anything.
    if not _sx_guards_ready():
        raise RuntimeError('CMS 固定来源保护尚未就绪，未激活订阅')
    if str(params.get('expected_gateway_url') or '').rstrip('/') != config.get('public_gateway_url', config['gateway_url']).rstrip('/'):
        raise ValueError('网站与执行端的 RE0 查询网关不一致，请核对连接配置')
    with _sx_lock:
        registry = _sx_registry()
        previous = registry['entries'].get(item_id)
        key = _sx_media_key(params)
        if previous and _sx_media_key(previous) != key:
            raise ValueError('不能更换已下发记录的 TMDB 身份')
        if previous and previous['revision'] > params['revision']:
            raise ValueError('订阅已更新，请重新提交最新状态')
        rows = _sx_rows(key)
        owner = registry['owners'].get(key)
        if rows and (not owner or any(str(r['id']) != str(owner) for r in rows)):
            raise ValueError('CMS 已有独立维护的同一作品订阅，请先在 CMS 整理，拾影不会覆盖它')
        if not previous and not params['active']:
            return dict(success=True, data=dict(state='absent', active=False))
        # Verify source and exact publisher again, without paid-unlock authority.
        if params['active']:
            resolved = _sx_gateway('resolve', dict(media_type=params['media_type'], tmdb_id=params['tmdb_id'], slug=slug, uploader=uploader))['data']
            if resolved.get('slug') != slug or resolved.get('uploader') != uploader:
                raise ValueError('RE0 条目或发布者已变化')
        entry = dict(item_id=item_id, media_type=params['media_type'], tmdb_id=str(params['tmdb_id']),
                     title=title, year=str(params.get('year') or '')[:4], season=season,
                     slug=slug or previous['slug'], uploader=uploader or previous['uploader'],
                     active=params['active'], revision=params['revision'])
        if previous and previous.get('run'):
            entry['run'] = previous['run']
        registry['entries'][item_id] = entry
        # Persist exact source restrictions before the native record can become active.
        _sx_save(registry)
        active = [e for e in registry['entries'].values() if _sx_media_key(e) == key and e['active']]
        authors = sorted({e['uploader'] for e in active})
        ok, _ = _set_enhanced_sub_item(media_type=params['media_type'], tmdb_id=params['tmdb_id'],
                                      sources=['hdhive_author'] if active else [], authors=authors)
        readback = _get_enhanced_sub_item(media_type=params['media_type'], tmdb_id=params['tmdb_id'], fallback_default=False)
        if not ok or readback['sources'] != (['hdhive_author'] if active else []) or set(readback['authors']) != set(authors):
            raise RuntimeError('固定发布者规则未保存，CMS 订阅未激活')
        if not rows and active:
            ok, _ = _cms_add_submedia_db(dict(title=title, year=entry['year'], tmdb_id=entry['tmdb_id'],
                type=entry['media_type'], poster_path=str(params.get('poster_path') or '')[:200],
                backdrop_path='', status=0))
            rows = _sx_rows(key)
            if not ok or len(rows) != 1:
                raise RuntimeError('CMS 媒体订阅创建失败')
            registry['owners'][key] = rows[0]['id']
            _sx_save(registry)
        if rows:
            ok, _ = _cms_update_submedia_status_db([rows[0]['id']], 1 if active else 0)
            if not ok:
                raise RuntimeError('CMS 媒体订阅状态更新失败')
        return _sx_result(registry, item_id)


def _sx_managed(rows, registry):
    return [r for r in rows if _sx_media_key(r) in registry['owners']]


_sx_original_fetch = _subscription_fetch_hdhive_merged


def _subscription_fetch_hdhive_merged(keyword, rows, mode=''):
    registry = _sx_registry()
    managed = _sx_managed(rows, registry)
    if not managed:
        return _sx_original_fetch(keyword, rows, mode=mode)
    if mode != 'author':
        return {}
    keys = {_sx_media_key(r) for r in managed}
    merged, seen = {}, set()
    for entry in registry['entries'].values():
        if not entry['active'] or _sx_media_key(entry) not in keys or entry['slug'] in seen:
            continue
        seen.add(entry['slug'])
        try:
            row = _sx_gateway('resolve', dict(media_type=entry['media_type'], tmdb_id=entry['tmdb_id'],
                slug=entry['slug'], uploader=entry['uploader']))['data']
            if row.get('slug') != entry['slug'] or row.get('uploader') != entry['uploader']:
                continue
            merged.setdefault('115', []).append(dict(url=row['share_url'], password=row.get('code', ''),
                title=row['title'], note=row['title'], remark=row.get('remark', ''), size=row.get('size', ''),
                resolution=row.get('resolution', ''), tmdb_id=entry['tmdb_id'], media_type=entry['media_type'],
                year=entry['year'], uploader=entry['uploader'], slug=entry['slug'], source='hdhive'))
        except Exception:
            _log('拾影固定来源检查失败，保留订阅，未切换其他条目或发布者')
    return merged


_sx_original_available = _has_hdhive_lookup_source


def _has_hdhive_lookup_source():
    try:
        _sx_gateway_config()
        return True
    except Exception:
        return _sx_original_available()


_sx_original_patch = _patch_share_down_service
_sx_native_tuple = None


def _patch_share_down_service(module):
    global _sx_native_tuple
    from app.modules.tg.tg_scraper import TelegramScraper
    import inspect as _sx_inspect
    if _sx_native_tuple is None and not _sub_sync_patched:
        _sx_native_tuple = 'before' in _sx_inspect.signature(TelegramScraper.search).parameters
    _sx_original_patch(module)
    if not _sub_sync_patched:
        return
    if _sx_native_tuple is None:
        raise RuntimeError('拾影来源保护必须在 CMS 订阅 Hook 初始化时安装')
    for name in ('search', 'search_all'):
        original = getattr(TelegramScraper, name)
        if getattr(original, '_shiying_source_guard', False):
            continue
        def wrap(method, method_name):
            def guarded(self, q=None, *args, **kwargs):
                if _subscription_sync_active():
                    registry = _sx_registry()
                    rows = _subscription_rows_for_filter(q or '')
                    if _sx_managed(rows, registry):
                        key = SUB_SYNC_CHANNEL_TO_SOURCE.get(getattr(self, 'channel', ''))
                        if key != 'hdhive_author':
                            # Native Telegram and other virtual sources cannot become fallbacks.
                            tuple_result = method_name == 'search' and _sx_native_tuple
                            return ([], None) if tuple_result else []
                return method(self, q, *args, **kwargs)
            guarded._shiying_source_guard = True
            return guarded
        setattr(TelegramScraper, name, wrap(original, name))


def _sx_operation(path, params):
    if path == '/api/shiying-executor/status':
        config = _sx_gateway_config()
        _sx_registry()
        guard = _sx_guards_ready()
        ready = guard and _sx_gateway('status').get('available') is True
        return dict(success=True, version=1, available=ready, executor=True,
                    query_mode=config.get('query_mode', 'remote'),
                    gateway_url=config.get('public_gateway_url', config['gateway_url']), source_guard=guard,
                    immediate_run=True)
    if path == '/api/shiying-executor/subscribe':
        return _sx_subscribe(params)
    if path == '/api/shiying-executor/run-once':
        return _sx_run_once(params)
    with _sx_lock:
        return _sx_result(_sx_registry(), str(params.get('item_id') or ''))


async def _shiying_executor(scope, receive, send):
    import asyncio
    status = 200
    try:
        token = _SX_Path('/config/shiying-executor.token').read_text().strip()
        supplied = dict(scope.get('headers', [])).get(b'authorization', b'').decode('ascii', 'ignore')
        path = scope['path']
        if len(token) < 32 or not _sx_hmac.compare_digest(supplied, 'Bearer ' + token):
            status, result = 401, dict(success=False)
        elif path not in ('/api/shiying-executor/status', '/api/shiying-executor/subscribe', '/api/shiying-executor/subscription-status', '/api/shiying-executor/run-once'):
            status, result = 404, dict(success=False)
        elif scope['method'] != ('GET' if path.endswith('/status') else 'POST'):
            status, result = 405, dict(success=False)
        else:
            raw = b''
            if scope['method'] != 'GET':
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect': return
                    raw += message.get('body', b'')
                    if len(raw) > 8192: raise ValueError('请求过大')
                    if not message.get('more_body'): break
            params = json.loads(raw) if raw else {}
            if not isinstance(params, dict): raise ValueError('请求格式无效')
            result = await asyncio.get_running_loop().run_in_executor(None, _sx_operation, path, params)
    except ValueError as error:
        status, result = 400, dict(success=False, message=str(error))
    except Exception:
        status, result = 502, dict(success=False, message='CMS 下发失败，请检查查询端或执行端配置')
    payload = json.dumps(result, ensure_ascii=False).encode()
    await send(dict(type='http.response.start', status=status, headers=[(b'content-type', b'application/json; charset=utf-8'), (b'cache-control', b'no-store')]))
    await send(dict(type='http.response.body', body=payload))
