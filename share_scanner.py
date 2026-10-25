"""Read public 115 share directories and recognize episodes or available movie video."""
import copy
import datetime as dt
import hashlib
import json
import re
import threading
import time
import unicodedata
from collections import deque
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from integrations import IntegrationError, fetch

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.mov', '.wmv', '.flv', '.webm', '.mpg', '.mpeg'}
EXTRAS = re.compile(r'(?i)(?:\b(?:samples?|trailers?|featurettes?|extras?|behind.the.scenes)\b|花絮|预告|片花|彩蛋|先导|试看|特辑)')
CN_NUMBER = '零〇一二两三四五六七八九十百千'
_SCAN_LOCK = threading.Lock()
_CACHE = {}


def share_identity(url, code=''):
    """Never fetch a caller-provided origin; only extract a 115 share identifier."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or
                parsed.hostname not in ('115.com', 'www.115.com', '115cdn.com', 'www.115cdn.com', 'anxia.com', 'www.anxia.com') or
                parsed.port not in (None, 80, 443)):
            raise ValueError()
        match = re.fullmatch(r'/s/([A-Za-z0-9]{5,64})/?', parsed.path)
        if not match:
            raise ValueError()
        query = parse_qs(parsed.query)
        password = next((query[key][0] for key in ('password', 'receive_code') if query.get(key)), str(code or ''))
        if len(password) > 100:
            raise ValueError()
        return match[1], password
    except (ValueError, TypeError):
        raise IntegrationError('请先保存有效的 115 分享链接和提取码，再识别集数') from None


def chinese_number(value):
    if value.isdecimal():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    result = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        else:
            result += (number or 1) * {'十': 10, '百': 100, '千': 1000}.get(char, 0)
            number = 0
    return result + number


def seasons_in(value):
    result = {int(number) for number in re.findall(r'(?i)(?<![A-Za-z])S(?:eason)?[ ._-]*(\d{1,3})(?=[ ._Ee/\-]|$)', value)}
    result.update(chinese_number(number) for number in re.findall(r'第([\d' + CN_NUMBER + r']{1,5})季', value))
    return result


def skip_path(path, season, tmdb_id=None):
    value = unicodedata.normalize('NFKC', path)
    if EXTRAS.search(value):
        return True
    ids = {int(value) for value in re.findall(r'(?i)tmdb[- =](\d+)', value)}
    return bool((tmdb_id and ids and int(tmdb_id) not in ids) or any(number != season for number in seasons_in(value)))


def skip_movie_path(path, tmdb_id=None):
    value = unicodedata.normalize('NFKC', path)
    if EXTRAS.search(value.replace('_', ' ')):
        return True
    ids = {int(number) for number in re.findall(r'(?i)tmdb[- =](\d+)', value)}
    if tmdb_id and any(number != int(tmdb_id) for number in ids):
        return True
    # A movie share must not become available because a series episode is present.
    return bool(seasons_in(value) or re.search(
        r'(?i)(?<![A-Za-z0-9])(?:EP?(?:ISODE)?[ ._-]*\d{1,4}|\d{1,3}x\d{1,4})(?=[ ._/-]|$)|第[\d' + CN_NUMBER + r']{1,5}[集期]', value))


def movie_video_candidate(row):
    """Small clips and menu streams are not evidence of the feature being available."""
    raw_size = row.get('s', row.get('size', row.get('file_size')))
    if raw_size is None:
        return True  # Some public listings omit size; the filename/path rules still apply.
    if isinstance(raw_size, bool) or not re.fullmatch(r'\d{1,20}', str(raw_size)):
        return False
    return int(raw_size) >= 50 * 1024 * 1024


def episode_numbers(path, season=1, air_dates=None):
    value = unicodedata.normalize('NFKC', path)
    name = PurePosixPath(value).stem
    seasons = seasons_in(value)
    numbered = re.search(r'(?i)(?<!\d)(\d{1,3})x(\d{1,4})(?!\d)', name)
    if numbered:
        return {int(numbered[2])} if int(numbered[1]) == season else set()
    if any(number != season for number in seasons) or (season != 1 and not seasons):
        return None
    numbers = set()
    for match in re.finditer(r'(?i)(?<![A-Za-z])E(?:P(?:ISODE)?)?[ ._-]*(\d{1,4})(?:[ ._]*[-~][ ._]*E?(\d{1,4}))?(?=$|[^A-Za-z0-9]|E\d)', name):
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= 5000 or last - first > 20:
            return None
        numbers.update(range(first, last + 1))
    for number in re.findall(r'第([\d' + CN_NUMBER + r']{1,5})[集期]', name):
        numbers.add(chinese_number(number))
    if numbers:
        return numbers if all(1 <= number <= 5000 for number in numbers) else None
    if re.fullmatch(r'[\[（(【]?\d{1,3}[\]）)】]?', name.strip()):
        number = int(re.sub(r'\D', '', name))
        return {number} if 1 <= number <= 999 else None
    date = re.search(r'(?<!\d)(20\d{2})[.\-_]?(\d{2})[.\-_]?(\d{2})(?!\d)', name)
    if date:
        day = '-'.join(date.groups())
        matches = [int(number) for number, value in (air_dates or {}).items() if value == day]
        if len(matches) == 1:
            return set(matches)
    return None


def format_episode_numbers(values):
    groups = []
    for number in sorted(values):
        if groups and number == groups[-1][-1] + 1:
            groups[-1].append(number)
        else:
            groups.append([number])
    return ','.join(str(group[0]) if len(group) == 1 else f'{group[0]}-{group[-1]}' for group in groups)


def read_share_episodes(url, code='', *, season=1, tmdb_id=None, air_dates=None, minimum_interval=.7, wait=0, media_type='tv'):
    if media_type not in ('tv', 'movie'):
        raise IntegrationError('资源识别类型无效')
    share_code, password = share_identity(url, code)
    signature = hashlib.sha256(json.dumps([share_code, password, season, tmdb_id, air_dates, media_type], sort_keys=True).encode()).hexdigest()
    if not _SCAN_LOCK.acquire(timeout=wait):
        raise IntegrationError('另一个资源正在识别，请稍后重试')
    try:
        cached = _CACHE.get(signature)
        if cached and time.monotonic() - cached[0] < 60:
            return copy.deepcopy(cached[1])
        result = _read(share_code, password, season, tmdb_id, air_dates, minimum_interval, media_type)
        if len(_CACHE) >= 64:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[signature] = time.monotonic(), copy.deepcopy(result)
        return result
    finally:
        _SCAN_LOCK.release()


def _read(share_code, password, season, tmdb_id, air_dates, minimum_interval, media_type):
    pending, seen = deque([('0', '', 0)]), set()
    episodes, unknown = set(), []
    video_count = ignored = entries = pages = 0
    started, last_request = time.monotonic(), 0
    while pending:
        cid, parent, depth = pending.popleft()
        if cid in seen:
            continue
        seen.add(cid)
        if depth > 8:
            raise IntegrationError('分享目录层级过深，未修改原集数，请手动核对')
        offset = 0
        while True:
            elapsed = time.monotonic() - started
            if pages >= 32 or entries >= 5000 or elapsed >= 45:
                raise IntegrationError('分享目录较大，本次未能完整读取，未修改原集数')
            delay = minimum_interval - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
            params = dict(share_code=share_code, receive_code=password, cid=cid, limit=200, offset=offset, asc=1, o='file_name')
            data = fetch('https://webapi.115.com/share/snap?' + urlencode(params),
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=min(10, max(1, 45 - elapsed)))
            last_request = time.monotonic()
            pages += 1
            if not isinstance(data, dict) or data.get('state') is not True or not isinstance(data.get('data'), dict):
                raise IntegrationError('无法读取 115 分享目录，请核对链接、提取码和分享有效期；原集数已保留')
            body = data['data']
            rows = body.get('list')
            if not re.fullmatch(r'\d{1,6}', str(body.get('count', ''))):
                raise IntegrationError('115 返回的目录信息不完整，原集数已保留')
            count = int(body['count'])
            if not isinstance(rows, list) or count < 0 or (not rows and offset < count):
                raise IntegrationError('115 分享目录未完整读取，原集数已保留')
            entries += len(rows)
            if entries > 5000:
                raise IntegrationError('分享目录较大，本次未能完整读取，未修改原集数')
            for row in rows:
                if not isinstance(row, dict):
                    raise IntegrationError('115 分享目录格式异常，原集数已保留')
                name = str(row.get('n') or row.get('fn') or row.get('file_name') or '')
                if not name or len(name) > 1024:
                    raise IntegrationError('115 返回的文件名无效，原集数已保留')
                path = parent + '/' + name
                if (skip_movie_path(path, tmdb_id) if media_type == 'movie' else skip_path(path, season, tmdb_id)):
                    ignored += 1
                    continue
                directory = str(row.get('fc')) == '0' if 'fc' in row else not row.get('fid')
                if directory:
                    child = str(row.get('cid') or '')
                    if not re.fullmatch(r'\d{1,30}', child):
                        raise IntegrationError('115 返回的目录标识无效，原集数已保留')
                    pending.append((child, path, depth + 1))
                    continue
                if PurePosixPath(name).suffix.lower() not in VIDEO_EXTENSIONS:
                    ignored += 1
                    continue
                if media_type == 'movie':
                    if movie_video_candidate(row):
                        video_count += 1
                        episodes.add(1)  # Alternate encodes and disc segments remain one movie.
                    else:
                        ignored += 1
                    continue
                video_count += 1
                values = episode_numbers(path, season, air_dates)
                if values is None:
                    unknown.append(name)
                else:
                    episodes.update(values)
            offset += len(rows)
            if offset >= count:
                break
    return dict(state='partial' if unknown else 'complete' if episodes else 'empty',
                episodes=format_episode_numbers(episodes), episode_count=len(episodes), video_count=video_count,
                unrecognized_count=len(unknown), examples=unknown[:5], ignored_count=ignored,
                checked_at=dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'))
