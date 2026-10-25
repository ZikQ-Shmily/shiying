"""TMDB, Emby library and user authentication clients. Errors omit credentials."""
import datetime as dt
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class IntegrationError(Exception):
    pass


class EmbyAuthError(IntegrationError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, headers=None, binary=False, maximum=6 * 1024 * 1024, body=None, timeout=12, error_messages=None):
    """No redirects: an Emby token must never follow a redirect to another host."""
    try:
        request_headers = {'User-Agent': 'Shiying/3.0', 'Accept': 'application/json', **(headers or {})}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=request_headers)
        with build_opener(NoRedirect).open(req, timeout=timeout) as response:
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise IntegrationError('远端响应过大，本次未更新进度')
            if binary:
                content_type = response.headers.get_content_type()
                if content_type not in ('image/jpeg', 'image/png', 'image/webp'):
                    raise IntegrationError('海报格式不受支持')
                return data, content_type
            if not data and body is not None:
                return {}
            result = json.loads(data)
            if not isinstance(result, (dict, list)):
                raise IntegrationError('远端返回的数据格式不正确')
            return result
    except HTTPError as error:
        messages = {401: '凭据无效或权限不足，请检查连接设置',
                    403: '远端拒绝访问，请检查凭据及代理设置',
                    404: '远端没有找到该内容或 API 路径',
                    429: '远端请求过于频繁，请稍后再试'}
        messages.update(error_messages or {})
        if 300 <= error.code < 400:
            message = '连接发生重定向，请填写服务最终地址（包含实际路径）'
        else:
            message = messages.get(error.code, '远端服务暂时不可用（HTTP %s）' % error.code)
        error_class = EmbyAuthError if error.code in (401, 403) or (
            error.code == 400 and urlsplit(url).path.endswith('/Users/AuthenticateByName')) else IntegrationError
        raise error_class(message) from None
    except (URLError, socket.timeout, TimeoutError, OSError):
        raise IntegrationError('无法连接远端服务，请检查地址、证书和网络') from None
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise IntegrationError('远端未返回有效的 API 数据，请检查服务地址') from None


def safe_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return ''
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return ''
    return value


def poster_path(value):
    return value if isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png|webp)', value) else ''


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9]\d{0,11}', str(value)):
        raise IntegrationError('作品标识无效')
    return int(value)


def valid_emby_url(value):
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise IntegrationError('请填写有效的 EMBY 服务地址，不要在地址中放入 API Key')
    if any(character.isspace() for character in value):
        raise IntegrationError('EMBY 地址不能包含空白字符')
    try:
        parsed.port
    except ValueError:
        raise IntegrationError('EMBY 地址端口无效') from None
    return value


class TMDBClient:
    def __init__(self, credential):
        self.credential = credential.strip()
        if not self.credential:
            raise IntegrationError('请先在设置中填写 TMDB API 凭据')

    def get(self, path, **params):
        params = {'language': 'zh-CN', **params}
        headers = {}
        if re.fullmatch(r'[a-fA-F0-9]{32}', self.credential):
            params['api_key'] = self.credential
        else:
            headers['Authorization'] = 'Bearer ' + self.credential
        return fetch('https://api.themoviedb.org/3' + path + '?' + urlencode(params), headers)

    def test(self):
        result = self.get('/authentication')
        if result.get('success') is not True:
            raise IntegrationError('TMDB 未确认凭据有效，请检查 API Key 或 Read Access Token')
        return 'TMDB 连接正常，可以搜索作品'

    @staticmethod
    def summary(value, media_type=None):
        kind = media_type or value.get('media_type')
        release = safe_date(value.get('first_air_date' if kind == 'tv' else 'release_date'))
        return dict(tmdb_id=positive_id(value.get('id')), media_type=kind,
                    title=str(value.get('name' if kind == 'tv' else 'title') or ''),
                    original_title=str(value.get('original_name' if kind == 'tv' else 'original_title') or ''),
                    year=int(release[:4]) if release else None,
                    overview=str(value.get('overview') or '')[:10000], poster_path=poster_path(value.get('poster_path')),
                    release_date=release)

    def search(self, query, page=1):
        result = self.get('/search/multi', query=query, page=page, include_adult='false')
        values = result.get('results')
        if not isinstance(values, list):
            raise IntegrationError('TMDB 搜索结果格式异常')
        return dict(results=[self.summary(value) for value in values if value.get('media_type') in ('tv', 'movie')],
                    page=result.get('page', page), total_pages=min(int(result.get('total_pages', 1)), 500))

    def details(self, media_type, item_id):
        if media_type not in ('tv', 'movie'):
            raise IntegrationError('请选择电影或剧集')
        item_id = positive_id(item_id)
        raw = self.get('/%s/%s' % (media_type, item_id))
        if raw.get('id') != item_id:
            raise IntegrationError('TMDB 作品详情不匹配')
        result = self.summary(raw, media_type)
        result['status'] = str(raw.get('status') or '')
        result['seasons'] = [dict(season_number=int(s['season_number']), name=str(s.get('name') or ''),
                                  episode_count=int(s.get('episode_count') or 0), air_date=safe_date(s.get('air_date')))
                             for s in raw.get('seasons', []) if isinstance(s.get('season_number'), int) and 1 <= s['season_number'] <= 1000]
        return result

    def tracking_metadata(self, media_type, item_id, season):
        details = self.details(media_type, item_id)
        air_dates = {}
        if media_type == 'movie':
            air_dates['1'] = details['release_date']
            total = 1
        else:
            if not any(value['season_number'] == season for value in details['seasons']):
                raise IntegrationError('TMDB 中没有这一季，请重新选择')
            raw = self.get('/tv/%s/season/%s' % (positive_id(item_id), season))
            if raw.get('season_number') != season or not isinstance(raw.get('episodes'), list):
                raise IntegrationError('TMDB 季详情格式异常')
            for episode in raw['episodes']:
                number = episode.get('episode_number')
                if type(number) is int and 1 <= number <= 5000:
                    air_dates[str(number)] = safe_date(episode.get('air_date'))
            total = max((int(number) for number in air_dates), default=0) or None
        return dict(id=details['tmdb_id'], media_type=media_type, poster_path=details['poster_path'],
                    overview=details['overview'], original_title=details['original_title'], status=details['status'],
                    air_dates=air_dates, total_episodes=total, title=details['title'], year=details['year'])


class EmbyClient:
    def __init__(self, base_url, api_key):
        if not base_url or not api_key:
            raise IntegrationError('请先填写 EMBY 地址和 API Key')
        self.base_url = valid_emby_url(base_url)
        self.api_key = api_key

    def get(self, path, **params):
        return fetch(self.base_url + path + ('?' + urlencode(params) if params else ''),
                     {'X-Emby-Token': self.api_key})

    def test(self):
        data = self.get('/System/Info')
        if not data.get('Version') or not data.get('ServerName'):
            raise IntegrationError('返回内容不是有效的 EMBY 系统信息，请检查地址路径')
        return 'EMBY 连接正常，已验证媒体库读取权限'

    def users(self):
        users = self.get('/Users')
        if not isinstance(users, list):
            raise IntegrationError('EMBY 用户列表格式异常')
        users = [user for user in users if isinstance(user, dict) and user.get('Id')]
        fallback_server = self.get('/System/Info').get('Id', '') if any(not user.get('ServerId') for user in users) else ''
        return [dict(id=str(user['Id']), server_id=str(user.get('ServerId') or fallback_server), username=str(user.get('Name') or ''),
                     is_admin=(user.get('Policy') or {}).get('IsAdministrator') is True,
                     disabled=(user.get('Policy') or {}).get('IsDisabled') is True)
                for user in users]

    def pages(self, path, **params):
        values = []
        for page in range(100):
            data = self.get(path, **params, StartIndex=page * 200, Limit=200)
            rows = data.get('Items')
            if not isinstance(rows, list):
                raise IntegrationError('EMBY 列表响应格式异常，本次未覆盖已有进度')
            values.extend(rows)
            total = data.get('TotalRecordCount')
            if type(total) is int and len(values) >= total:
                return values
            if len(rows) < 200:
                if type(total) is int and len(values) < total:
                    raise IntegrationError('EMBY 分页结果不完整，本次未覆盖已有进度')
                return values
        raise IntegrationError('EMBY 匹配结果过多，本次未覆盖已有进度')

    @staticmethod
    def playable(item):
        return str(item.get('LocationType', '')).lower() != 'virtual' and not any(
            item.get(flag) is True for flag in ('IsVirtualItem', 'IsMissing', 'IsPlaceHolder'))

    def compare(self, media_type, tmdb_id, season, titles=()):
        kind = 'Movie' if media_type == 'movie' else 'Series'
        raw = self.pages('/Items', Recursive='true', IncludeItemTypes=kind,
                         AnyProviderIdEquals='tmdb.' + str(positive_id(tmdb_id)),
                         Fields='ProviderIds,Path', IsPlaceHolder='false', EnableImages='false', EnableUserData='false')
        matches = []
        for item in raw:
            providers = item.get('ProviderIds') or {}
            identifier = next((value for key, value in providers.items() if key.lower() == 'tmdb'), None)
            if str(identifier) == str(tmdb_id) and str(item.get('Type', kind)).lower() == kind.lower() and item.get('Id'):
                if media_type != 'movie' or self.playable(item):
                    matches.append(item)
        if not matches:
            # A successful ID lookup with no result is normal for a requested title.
            # Only flag metadata ambiguity when a title search finds a candidate.
            names = list(dict.fromkeys(str(title).strip() for title in titles if title and str(title).strip()))
            normalized_names = {re.sub(r'[\W_]', '', name.casefold()) for name in names}
            if names:
                for title in names:
                    candidates = self.pages('/Items', Recursive='true', IncludeItemTypes=kind,
                                            SearchTerm=title, Fields='ProviderIds', IsPlaceHolder='false',
                                            EnableImages='false', EnableUserData='false')
                    if any(item.get('Id') and (not item.get('Name') or re.sub(r'[\W_]', '', item['Name'].casefold()) in normalized_names)
                           and str(item.get('Type', kind)).lower() == kind.lower()
                           and (media_type != 'movie' or self.playable(item)) for item in candidates):
                        return dict(state='unmatched', episodes=[], item_ids=[], unnumbered=0,
                                    error='库中发现同名或相似作品，但 TMDB ID 不一致，请核对刮削信息；原有进度已保留')
                return dict(state='absent', episodes=[], item_ids=[], unnumbered=0, error='')
            return dict(state='unmatched', episodes=[], item_ids=[], unnumbered=0,
                        error='未找到相同 TMDB ID 的 EMBY 项目，请核对刮削信息；原有进度已保留')
        if media_type == 'movie':
            return dict(state='matched', episodes=[1], item_ids=[str(item['Id']) for item in matches], unnumbered=0, error='')
        episodes = set()
        unnumbered = 0
        for item in matches:
            rows = self.pages('/Shows/' + quote(str(item['Id']), safe='') + '/Episodes', Season=season,
                              Fields='ProviderIds,Path', IsPlaceHolder='false', EnableImages='false', EnableUserData='false')
            for episode in rows:
                if not self.playable(episode):
                    continue
                if episode.get('ParentIndexNumber') is not None and episode['ParentIndexNumber'] != season:
                    continue
                start = episode.get('IndexNumber')
                end = episode.get('IndexNumberEnd') or start
                if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 5000:
                    unnumbered += 1
                    continue
                episodes.update(range(start, end + 1))
        return dict(state='matched', episodes=sorted(episodes), item_ids=[str(item['Id']) for item in matches],
                    unnumbered=unnumbered, error='存在 %s 个未编号项目，请手动核对' % unnumbered if unnumbered else '')


class EmbyUserClient:
    """Authenticate with the user's credentials, never the service API key."""
    def __init__(self, base_url, token='', device_id='shiying'):
        self.base_url = valid_emby_url(base_url)
        self.token = token
        self.authorization = ('Emby Client="Shiying", Device="Web", DeviceId="%s", Version="3.0"' % device_id)

    def request(self, path, body=None):
        headers = {'X-Emby-Authorization': self.authorization}
        if self.token:
            headers['X-Emby-Token'] = self.token
        return fetch(self.base_url + path, headers, body=body)

    @staticmethod
    def validate_user(user):
        if not isinstance(user, dict) or not user.get('Id') or not user.get('Name') or not isinstance(user.get('Policy'), dict):
            raise EmbyAuthError('EMBY 未返回有效的用户身份与权限')
        if user['Policy'].get('IsDisabled') is True:
            raise EmbyAuthError('该 EMBY 用户已被禁用')
        return user

    def authenticate(self, username, password):
        try:
            result = self.request('/Users/AuthenticateByName', {'Username': username, 'Pw': password})
        except EmbyAuthError:
            raise EmbyAuthError('EMBY 用户名或密码不正确，或账号不允许登录') from None
        if not isinstance(result, dict):
            raise EmbyAuthError('EMBY 未返回有效的登录信息')
        user = self.validate_user(result.get('User'))
        token = result.get('AccessToken')
        server_id = result.get('ServerId') or user.get('ServerId')
        if not isinstance(token, str) or not token or not server_id:
            raise EmbyAuthError('EMBY 未返回完整的登录信息')
        return dict(user=user, token=token, server_id=str(server_id))

    def user(self, user_id):
        user = self.validate_user(self.request('/Users/' + quote(str(user_id), safe='')))
        if str(user['Id']) != str(user_id):
            raise EmbyAuthError('EMBY 用户身份不匹配，请重新登录')
        return user

    def logout(self):
        return self.request('/Sessions/Logout', {})
