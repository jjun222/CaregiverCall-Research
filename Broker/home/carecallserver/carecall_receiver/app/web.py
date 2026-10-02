"""Unprivileged, AP-only connection-test page. No router credentials are accepted."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading

import common as c

PAGE = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CareCall 연결 시험</title><style>
body{margin:0;background:#f1f5f8;color:#18313d;font:17px/1.65 system-ui,sans-serif}
main{max-width:520px;margin:40px auto;padding:28px;background:white;border-radius:22px}
.brand{color:#087c74;font-weight:750}h1{font-size:27px;line-height:1.35}
.note{padding:16px;background:#edf8f5;border-radius:12px}button{width:100%;padding:16px;
border:0;border-radius:12px;background:#087c74;color:white;font:inherit;font-weight:700}
button:disabled{opacity:.5}#result{min-height:55px}small{color:#52666e}
@media(max-width:600px){main{margin:18px;padding:24px}}
</style><main><div class="brand">CareCall</div><h1>휴대폰과 장치가<br>연결되었습니다.</h1>
<p>현재 화면은 Wi-Fi 설정을 위한 첫 연결 시험입니다.</p>
<p class="note">아래 버튼을 누르면 장치가 기존 연구실 Wi-Fi로 돌아갑니다.
이 설정용 Wi-Fi는 사라지고 휴대폰 연결도 종료됩니다.</p>
<button id="done" disabled>시험 완료 · 기존 Wi-Fi로 복귀</button><p id="result">연결 상태를 확인하고 있습니다.</p>
<small>이번 시험에서는 공유기 이름이나 비밀번호를 입력하지 않습니다.
아무것도 누르지 않아도 시험 시작 약 3분 후 복귀를 시도합니다.</small></main>
<script>
let token=''; const result=document.getElementById('result'), done=document.getElementById('done');
fetch('/session',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()})
.then(s=>{token=s.token;done.disabled=false;result.textContent='화면이 정상적으로 열렸다면 시험을 완료해 주세요.'})
.catch(()=>{result.textContent='시험 시간이 끝났거나 연결이 종료되었습니다.'});
done.onclick=async()=>{done.disabled=true;try{const r=await fetch('/confirm',{method:'POST',
headers:{'Content-Type':'application/json','X-CareCall-Token':token},body:JSON.stringify({token})});
if(!r.ok)throw Error();result.textContent='확인했습니다. 장치가 기존 Wi-Fi로 돌아갑니다. 개발 PC에서 결과를 확인해 주세요.';
}catch(e){result.textContent='응답을 확인하지 못했습니다. 자동 복귀를 기다린 뒤 개발 PC에서 결과를 확인해 주세요.'}};
</script></html>'''


def valid_confirmation(host, origin, header, body, expected_token, expected_host):
    return (host == expected_host and origin == 'http://' + expected_host
            and isinstance(body, dict) and set(body) == {'token'}
            and isinstance(body['token'], str) and isinstance(header, str)
            and hmac.compare_digest(body['token'], expected_token)
            and hmac.compare_digest(header, expected_token))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, public):
        self.public = Path(public)
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)
        self.expected_host = f'{address[0]}:{self.server_address[1]}'

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'CareCall'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get('Host') != self.server.expected_host:
            return self.reply(403, '{}')
        if self.path == '/':
            return self.reply(200, PAGE, 'text/html; charset=utf-8')
        if self.path == '/session':
            try:
                data = json.loads((self.server.public / 'session.json').read_text())
                return self.reply(200, json.dumps({'token': data['token']}))
            except (OSError, ValueError, KeyError):
                return self.reply(503, '{}')
        self.reply(404, '{}')

    def do_POST(self):
        try:
            if self.path != '/confirm' or self.headers.get('Transfer-Encoding'):
                return self.reply(400, '{}')
            length = int(self.headers.get('Content-Length', '-1'))
            if not 1 <= length <= 256 or self.headers.get_content_type() != 'application/json':
                return self.reply(400, '{}')
            body = json.loads(self.rfile.read(length))
            session = json.loads((self.server.public / 'session.json').read_text())
            if not valid_confirmation(self.headers.get('Host'), self.headers.get('Origin'),
                    self.headers.get('X-CareCall-Token'), body, session['token'], self.server.expected_host):
                return self.reply(403, '{}')
            target = self.server.public / 'confirmed'
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write(session['token'])
                    f.flush()
                    os.fsync(f.fileno())
            except FileExistsError:
                pass
            self.reply(200, '{"accepted":true}')
        except (OSError, ValueError, KeyError):
            self.reply(400, '{}')


if __name__ == '__main__':
    Server((c.AP_IP, c.AP_PORT), c.RUN / 'public').serve_forever()
