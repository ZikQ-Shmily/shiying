"""CMS RE0 queries and fixed-source subscriptions; OAuth stays in CMS."""
import re
import math
from urllib.parse import urlsplit, urlunsplit
from integrations import fetch, IntegrationError, valid_emby_url


def canonical_re0_post_url(value):
    """Keep RE0 resource permalinks on its web origin, independent of CMS API settings."""
    parsed = urlsplit(value)
    if (parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password and
            (parsed.hostname or '').lower() in ('hdhive.com', 'www.hdhive.com') and
            re.fullmatch(r'/resource/(?:115/)?[\w-]+/?', parsed.path)):
        return urlunsplit(('https', 're0.me', parsed.path, parsed.query, parsed.fragment))
    return value


class CMSClient:
    def __init__(self, url, token):
        self.url = valid_emby_url(url) if url else ''
        self.token = token
        if not self.url or not self.token:
            raise IntegrationError('请先配置用于 RE0 查询的 CMS 地址与连接密钥')

    def call(self, action, payload=None, timeout=65):
        result = fetch(self.url + '/api/shiying/' + action,
                       headers={'Authorization': 'Bearer ' + self.token}, body=payload, timeout=timeout,
                       error_messages={400: 'RE0 条目或积分已变化，请刷新来源列表后重试'})
        if not isinstance(result, dict) or result.get('success') is not True:
            raise IntegrationError('CMS / RE0 查询失败，请检查 CMS 授权')
        return result

    def test(self):
        result = self.call('status', timeout=12)
        if result.get('version') != 1 or not result.get('available'):
            raise IntegrationError('CMS 已连接，RE0 尚未授权，请在 CMS 中完成授权')
        return 'RE0 授权正常，查询通过已配置的 CMS 完成'

    def candidates(self, item):
        meta = item['tmdb']
        rows = self.call('search', dict(media_type=meta['media_type'], tmdb_id=meta['id'])).get('data')
        if not isinstance(rows, list):
            raise IntegrationError('CMS 返回的资源列表无效')
        return [self.clean(row, meta) for row in rows[:1000]]

    @staticmethod
    def clean(row, meta):
        if not isinstance(row, dict) or str(row.get('tmdb_id')) != str(meta['id']) or row.get('media_type') != meta['media_type'] or not re.fullmatch(r'[\w-]{1,200}', str(row.get('slug', ''))):
            raise IntegrationError('RE0 资源与当前作品不匹配')
        result = {key: str(row.get(key) or '')[:limit] for key, limit in dict(slug=200, uploader=200, title=500,
                  remark=2000, resolution=500, source=500, size=100, resource_url=2000).items()}
        parsed = urlsplit(result['resource_url'])
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise IntegrationError('RE0 原帖地址无效')
        result['resource_url'] = canonical_re0_post_url(result['resource_url'])
        points = row.get('unlock_points')
        result.update(is_unlocked=row.get('is_unlocked') is True,
                      unlock_points=points if isinstance(points, (int, float)) and not isinstance(points, bool) and math.isfinite(points) and points >= 0 else None)
        result['can_bind'] = bool(result['uploader']) and (result['is_unlocked'] or result['unlock_points'] == 0)
        result['can_unlock'] = bool(result['uploader']) and result['unlock_points'] is not None and not result['can_bind']
        return result

    def resolve(self, item, slug, uploader, max_points=None):
        meta = item['tmdb']
        payload = dict(media_type=meta['media_type'], tmdb_id=meta['id'], slug=slug, uploader=uploader)
        if max_points is not None:
            payload['max_points'] = max_points
        row = self.call('resolve', payload).get('data')
        result = self.clean(row, meta)
        if result['slug'] != slug or result['uploader'] != uploader or not result['can_bind']:
            raise IntegrationError('来源或发布者已变化，或该条目需要先解锁')
        url = str(row.get('share_url') or '').strip()
        parsed = urlsplit(url)
        if len(url) > 2000 or parsed.scheme not in ('http', 'https') or (parsed.hostname or '').lower() not in ('115.com','www.115.com','115cdn.com','www.115cdn.com','anxia.com','www.anxia.com') or parsed.username or parsed.password:
            raise IntegrationError('CMS 未返回有效的 115 分享链接')
        result.update(share_url=url, code=str(row.get('code') or '')[:100])
        return result


class CMSExecutor:
    def __init__(self, url, token, native_re0=False):
        self.url = valid_emby_url(url) if url else ''
        self.token = token
        self.native_re0 = native_re0
        if not self.url or not token:
            raise IntegrationError('请先配置 CloudMediaSync 地址与连接密钥')

    def call(self, action, payload=None):
        result = fetch(self.url + '/api/shiying-executor/' + action,
            headers={'Authorization': 'Bearer ' + self.token}, body=payload, timeout=80 if payload else 15,
            error_messages={400: 'CMS 拒绝下发：请检查是否已有独立订阅，或固定来源、发布者已变化'})
        if not isinstance(result, dict) or result.get('success') is not True:
            raise IntegrationError('CMS 未确认请求成功，请在 CMS 核对执行记录')
        return result

    def test(self):
        result = self.call('status')
        if result.get('version') != 1 or not result.get('executor') or not result.get('available') or not result.get('source_guard'):
            raise IntegrationError('CMS 订阅服务、RE0 授权或固定来源保护尚未就绪')
        if self.native_re0 and result.get('query_mode') != 'local':
            raise IntegrationError('CMS 尚未使用自身的 RE0 授权，请检查 CMS 中的拾影连接配置')
        return 'CMS 连接正常，固定来源订阅与转存服务已就绪'

    def subscribe(self, payload):
        data = self.call('subscribe', payload).get('data')
        if not isinstance(data, dict) or data.get('active') is not payload['active'] or (
                payload['active'] and (data.get('state') != 'submitted' or data.get('uploader') != payload['uploader'] or
                data.get('slug') != payload['slug'] or data.get('season') != payload['season'])):
            raise IntegrationError('CMS 返回的订阅与固定来源不一致，请重新下发')
        return {key: data.get(key) for key in ('subscription_id', 'active', 'native_status', 'state')}

    def run(self, payload, start=False):
        data = self.call('run-once' if start else 'subscription-status', payload).get('data')
        if (not isinstance(data, dict) or any(data.get(k) != payload.get(k) for k in ('slug', 'uploader', 'season'))):
            raise IntegrationError('CMS 执行记录与当前固定来源不一致，请刷新后重试')
        run = data.get('run')
        if run is None and not start:
            return None
        if (not isinstance(run, dict) or run.get('state') not in
                ('queued', 'running', 'submitted', 'exists', 'error', 'unknown', 'cancelled') or
                not re.fullmatch(r'[a-f0-9-]{36}', str(run.get('request_id') or ''))):
            raise IntegrationError('CMS 未确认本次执行状态，请在 CMS 核对转存记录')
        return {k: str(run.get(k) or '')[:300] for k in ('request_id', 'state', 'message', 'requested_at', 'updated_at')}
