"""CI-only: inspect a built image and replace a disposable container with runtime data.

No production address, password or database is used. Docker is needed on the runner.
"""
import argparse
import http.cookiejar
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time
import urllib.request


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (result.stdout + (result.stderr if args[0] == 'logs' else '')).strip()


def main(image):
    details = json.loads(docker('image', 'inspect', image))[0]
    env_names = {value.split('=', 1)[0] for value in details['Config'].get('Env', [])}
    assert not env_names.intersection({'SECRET_KEY','ADMIN_PASSWORD','EMBY_URL','EMBY_API_KEY','TMDB_API_KEY','CMS_TOKEN','SMTP_PASSWORD'}), 'Runtime credentials baked into image'
    docker('run', '--rm', '--entrypoint', 'python', image, '-c',
           "from pathlib import Path; root=Path('/app'); "
           "assert not (root/'data').exists(); assert not (root/'.env').exists(); "
           "assert not (root/'.git').exists(); assert not (root/'tests').exists(); "
           "assert not any(p.name.startswith('.env') or p.suffix in ('.sqlite3','.db','.pem','.key') for p in root.rglob('*') if p.is_file())")
    print('Image contains application code and public assets, with no runtime configuration or database.')
    name = 'shiying-ci-' + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix='shiying-container-test-') as folder:
        data = Path(folder) / 'data'; data.mkdir(); data.chmod(0o777)
        password = secrets.token_urlsafe(24)
        key = secrets.token_hex(32)
        environment = Path(folder) / 'runtime.env'
        environment.write_text('ADMIN_USERNAME=ci-admin\nADMIN_PASSWORD=' + password + '\nSECRET_KEY=' + key + '\nCOOKIE_SECURE=false\nENABLE_SCHEDULER=false\n')
        environment.chmod(0o600)
        cookie = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie))

        def start():
            docker('run','--detach','--name',name,'--read-only','--tmpfs','/tmp:rw,mode=1777',
                   '--cap-drop','ALL','--security-opt','no-new-privileges:true',
                   '--publish','127.0.0.1::8000','--env-file',str(environment),
                   '--mount','type=bind,source='+str(data)+',target=/app/data',image)
            port = docker('port', name, '8000/tcp').splitlines()[0].rsplit(':', 1)[1]
            base = 'http://127.0.0.1:' + port
            for _ in range(60):
                try:
                    with urllib.request.urlopen(base+'/health',timeout=2) as response:
                        if response.status == 200:
                            return base
                except OSError:
                    time.sleep(.5)
            raise RuntimeError('Disposable test container did not become healthy')

        def call(base, path, body=None, csrf=None):
            headers = {'Content-Type':'application/json'}
            if csrf: headers['X-CSRF-Token'] = csrf
            request = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            if path in ('/api/integrations','/api/notifications') and body is not None:
                request.method = 'PUT'
            with client.open(request,timeout=5) as response:
                return json.load(response)

        def login(base):
            return call(base,'/api/login',{'provider':'local','username':'ci-admin','password':password})['csrf']

        try:
            base = start()
            assert call(base,'/api/session')['emby_available'] is False
            csrf = login(base)
            config = call(base,'/api/integrations')
            assert config['emby_url'] == '' and config['cms_url'] == ''
            assert not any(config[field] for field in ('emby_configured','tmdb_configured','cms_configured','re0_configured'))
            mail = call(base,'/api/notifications')
            assert not mail['enabled'] and not mail['has_password'] and not mail['to_email']
            fake_emby_key = secrets.token_urlsafe(24)
            call(base,'/api/integrations',{'emby_url':'http://emby.example.invalid:8096','emby_api_key':fake_emby_key,'auto_sync':False},csrf)
            fake_mail_key = secrets.token_urlsafe(24)
            call(base,'/api/notifications',{'enabled':False,'smtp_host':'smtp.example.invalid','smtp_username':'sender@example.invalid',
                'smtp_password':fake_mail_key,'from_email':'sender@example.invalid','to_email':'owner@example.invalid'},csrf)
            created = call(base,'/api/items',{'title':'Disposable CI record','kind':'tv','season':1,'sources':[]},csrf)['item']['id']
            with sqlite3.connect('file:'+str(data/'media.sqlite3')+'?mode=ro',uri=True) as db:
                values='\n'.join(row[0] for row in db.execute('SELECT value FROM settings'))
                assert fake_emby_key not in values and fake_mail_key not in values
            db.close()  # Release the host reader before replacing the non-root container.
            print('Fresh installation has no personal connections; runtime credentials are saved encrypted.')
            docker('rm','--force',name)
            base = start()
            csrf = login(base)
            config = call(base,'/api/integrations')
            assert config['emby_url']=='http://emby.example.invalid:8096' and config['emby_configured']
            mail=call(base,'/api/notifications')
            assert mail['to_email']=='owner@example.invalid' and mail['has_password']
            assert 'smtp_password' not in mail and fake_mail_key not in json.dumps(mail)
            assert any(item['id']==created for item in call(base,'/api/items')['items'])
            print('Container replacement preserved the database, configured connections and encrypted mail credential.')
        except Exception:
            print(docker('logs','--tail','30',name,check=False))
            raise
        finally:
            docker('rm','--force',name,check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--image',required=True)
    main(parser.parse_args().image)
