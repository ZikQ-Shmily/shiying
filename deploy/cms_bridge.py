"""Inserted into the existing CMS plugin; reuses its in-process OAuth client."""
import math as _shiying_math
import hmac as _shiying_hmac
import threading as _shiying_threading
_shiying_lock = _shiying_threading.Lock()


def _shiying_resources(params):
    media_type, tmdb_id = params.get('media_type'), str(params.get('tmdb_id', ''))
    if media_type not in ('tv', 'movie') or not tmdb_id.isdecimal() or not 0 < int(tmdb_id) < 10**10:
        raise ValueError('作品标识无效')
    result = hdhive_client.search(media_type, tmdb_id)
    if not isinstance(result, dict) or result.get('success') is not True or not isinstance(result.get('data'), list):
        raise RuntimeError('RE0 查询失败，请检查 CMS 授权')
    rows = []
    for raw in result['data']:
        if not isinstance(raw, dict) or str(raw.get('pan_type', '')).lower() != '115':
            continue
        slug = str(raw.get('slug') or '')[:200]
        if not slug or not all(c.isalnum() or c in '-_' for c in slug):
            continue
        points = raw.get('unlock_points')
        try:
            points = float(points) if not isinstance(points, bool) else None
            if points is not None and (points < 0 or not _shiying_math.isfinite(points)): points = None
        except (ValueError, TypeError):
            points = None
        def joined(value):
            return ', '.join(map(str, value))[:500] if isinstance(value, list) else str(value or '')[:500]
        rows.append(dict(slug=slug, title=str(raw.get('title') or '')[:500],
            remark=str(raw.get('remark') or '')[:2000], uploader=str((raw.get('user') or {}).get('nickname') or '')[:200],
            resolution=joined(raw.get('video_resolution')), source=joined(raw.get('source')),
            size=str(raw.get('share_size') or '')[:100], unlock_points=points,
            is_unlocked=raw.get('is_unlocked') is True, media_type=media_type, tmdb_id=int(tmdb_id),
            resource_url=HDHIVE_WEB_BASE_URL + '/resource/115/' + slug))
    return rows[:1000]


def _shiying_operation(path, params):
    if path == '/api/shiying/status':
        return dict(success=True, version=1, available=bool(hdhive_client.available))
    with _shiying_lock:
        rows = _shiying_resources(params)
        if path == '/api/shiying/search':
            return dict(success=True, data=rows)
        slug, uploader = params.get('slug'), params.get('uploader')
        row = next((r for r in rows if r['slug'] == slug and r['uploader'] == uploader), None)
        if not row:
            raise ValueError('原条目或发布者已变化，请重新选择来源')
        if not row['uploader']:
            raise ValueError('该条目缺少发布者，暂不能绑定追更')
        if not row['is_unlocked'] and row['unlock_points'] != 0:
            maximum = params.get('max_points')
            if (isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or
                    not _shiying_math.isfinite(maximum) or maximum < 0 or
                    row['unlock_points'] is None or row['unlock_points'] > maximum):
                raise ValueError('该条目需要解锁或积分已变化，请刷新来源列表后点击解锁')
        result = hdhive_client.unlock(slug)
        if not isinstance(result, dict) or result.get('success') is not True:
            raise RuntimeError('RE0 链接读取失败，请稍后重试')
        data = result.get('data')
        if isinstance(data, dict):
            url = data.get('full_url') or data.get('url') or data.get('share_url') or data.get('link')
            code = data.get('access_code') or data.get('code') or ''
        else:
            url, code = data, ''
        from urllib.parse import urlsplit
        parsed = urlsplit(str(url or '').strip())
        if parsed.scheme not in ('http', 'https') or (parsed.hostname or '').lower() not in ('115.com', 'www.115.com', '115cdn.com', 'www.115cdn.com', 'anxia.com', 'www.anxia.com') or parsed.username or parsed.password:
            raise ValueError('RE0 没有返回有效的 115 分享链接')
        row.update(share_url=str(url).strip(), code=str(code)[:100], is_unlocked=True)
        return dict(success=True, data=row)


async def _shiying_bridge(scope, receive, send):
    import asyncio
    path = scope['path']
    status, result = 200, None
    try:
        from pathlib import Path
        token_path = Path('/config/shiying-bridge.token')
        if not token_path.exists():
            token_path = Path('/config/shiying-executor.token')
        token = token_path.read_text().strip()
        authorization = dict(scope.get('headers', [])).get(b'authorization', b'').decode('ascii', 'ignore')
        if len(token) < 32 or not _shiying_hmac.compare_digest(authorization, 'Bearer ' + token):
            status, result = 401, dict(success=False, message='桥接凭据无效')
        elif path not in ('/api/shiying/status', '/api/shiying/search', '/api/shiying/resolve'):
            status, result = 404, dict(success=False, message='接口不存在')
        elif scope.get('method') != ('GET' if path.endswith('/status') else 'POST'):
            status, result = 405, dict(success=False, message='请求方法无效')
        else:
            raw = b''
            while scope.get('method') != 'GET':
                message = await receive()
                if message.get('type') == 'http.disconnect': return
                raw += message.get('body', b'')
                if len(raw) > 8192: raise ValueError('请求过大')
                if not message.get('more_body'): break
            params = json.loads(raw) if raw else {}
            if not isinstance(params, dict): raise ValueError('请求格式无效')
            result = await asyncio.get_running_loop().run_in_executor(None, _shiying_operation, path, params)
    except ValueError as error:
        status, result = 400, dict(success=False, message=str(error))
    except Exception:
        status, result = 502, dict(success=False, message='CMS / RE0 暂不可用，请检查授权或稍后重试')
    payload = json.dumps(result, ensure_ascii=False).encode()
    await send(dict(type='http.response.start', status=status, headers=[(b'content-type', b'application/json; charset=utf-8'), (b'cache-control', b'no-store')]))
    await send(dict(type='http.response.body', body=payload))
