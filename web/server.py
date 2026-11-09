"""Local P07 task service. Run: python -m web.server --port 8765."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

from repair.fixtures import load_cases, PROJECT_ROOT

STATIC = Path(__file__).parent / 'static'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported task database schema')
            db.execute('PRAGMA user_version=1')
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, case_id TEXT, mode TEXT, status TEXT, cancel INTEGER, created REAL, updated REAL, result TEXT)')
            db.execute("UPDATE tasks SET status='interrupted',updated=? WHERE status IN ('queued','running')", (time.time(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, case_id, mode):
        task_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                       (task_id, case_id, mode, 'queued', 0, now, now, '{}'))
        return self.get(task_id)

    def get(self, task_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        data = dict(row)
        data['result'] = json.loads(data['result'])
        return data

    def list(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM tasks ORDER BY created DESC LIMIT 100')]
        return [self.get(i) for i in ids]

    def cancel(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            db.execute("UPDATE tasks SET cancel=1, status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated=? WHERE id=? AND status NOT IN ('succeeded','failed','cancelled','interrupted')", (time.time(), task_id))
        return self.get(task_id)

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE tasks SET status='running',updated=? WHERE id=?", (time.time(), row[0]))
        return self.get(row[0]) if row else None

    def finish(self, task_id, result, *, interrupted=False):
        with self.connect() as db:
            row = db.execute('SELECT cancel FROM tasks WHERE id=?', (task_id,)).fetchone()
            status = ('cancelled' if row[0] else 'succeeded' if result.get('status') == 'verified'
                      else 'interrupted' if interrupted else 'failed')
            db.execute('UPDATE tasks SET status=?,result=?,updated=? WHERE id=?',
                       (status, json.dumps(result, ensure_ascii=False), time.time(), task_id))


def demo_runner(task, cancelled):
    for _ in range(8):
        if cancelled():
            return {'status': 'cancelled', 'evidence': 'simulated'}
        time.sleep(.1)
    return {'status': 'verified', 'evidence': 'simulated',
            'logs': ['演示：解析完成', '演示：测试生成', '演示：执行与修复完成'],
            'tests': 'def test_boundary():\n    assert is_adult(18) is True\n',
            'diagnosis': '演示数据：边界比较条件差一。未调用模型或容器。',
            'diff': '--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-return age > 18\n+return age >= 18\n',
            'coverage': None}


def real_runner(image_id, artifact_root, *, model=None, feedback=False):
    from models.adapter import Config, Budget, ModelClient
    from workflow.orchestrator import run_case, feedback_case
    cases, validation, _ = load_cases()
    by_id = {c['id']: c for c in cases}

    def run(task, cancelled):
        if cancelled():
            return {'status': 'cancelled', 'evidence': 'real', 'coverage': {}}
        config = Config.from_env(PROJECT_ROOT / '.env')
        if model:
            config = replace(config, model=model)
        client = ModelClient(config,
                             Budget(PROJECT_ROOT / 'artifacts/model-budget.json', max_attempts=None),
                             thinking_mode='disabled')
        if client.config.model not in client.list_models():
            raise ValueError('model not authorized')
        case = dict(by_id[task['case_id']], require_coverage=feedback)
        result = run_case(case, validation[task['case_id']], client=client,
                          image_id=image_id, artifact_root=artifact_root, cancelled=cancelled)
        child = None
        if feedback and result['status'] == 'verified' and not cancelled():
            child = run_case(feedback_case(case, result), validation[task['case_id']], client=client,
                             image_id=image_id, artifact_root=artifact_root, cancelled=cancelled)
        logs = []
        base = Path(artifact_root).resolve()
        def collect(value):
            if isinstance(value, dict):
                location = value.get('artifact_directory')
                if isinstance(location, str) and len(logs) < 30:
                    directory = Path(location).resolve()
                    if directory.is_relative_to(base) and directory != base:
                        from execution.proposals import checked_path
                        try:
                            file = checked_path(base, str(directory.relative_to(base) / 'log.txt').replace('\\', '/'))
                            with file.open('rb') as stream:
                                text = stream.read(65536).decode('utf-8', errors='replace')
                            logs.append(str(directory.relative_to(base)) + '\n' + text)
                        except (OSError, ValueError):
                            logs.append('执行日志不可读取')
                for child in value.values():
                    collect(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    collect(child)
        collect(result)
        if child:
            collect(child)
        output = {'evidence': 'real', 'workflow': result, 'status': child['status'] if child else result['status'],
                  'coverage': result.get('coverage', {}), 'feedback_child': child, 'logs': logs}
        client.ensure_safe(output)
        return output
    return run


class Application:
    def __init__(self, store, *, mode='demo', runner=None):
        self.store = store
        self.mode = mode
        self.runner = runner or demo_runner
        self.token = secrets.token_urlsafe(32)
        self.cases = [c['id'] for c in load_cases()[0]]
        self.stop = threading.Event()
        self.thread = None
        self.submit_lock = threading.Lock()

    def submit(self, body):
        if self.mode == 'evidence':
            raise ValueError('历史证据模式只读，请用演示或真实模式创建任务')
        if not isinstance(body, dict) or set(body) != {'case_id'} or body['case_id'] not in self.cases:
            raise ValueError('请选择已审核的任务，不接受路径、命令或密钥')
        with self.submit_lock:
            if sum(t['status'] in {'queued', 'running'} for t in self.store.list()) >= 10:
                raise ValueError('任务队列已满')
            return self.store.create(body['case_id'], self.mode)

    def start(self):
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def work(self):
        while not self.stop.is_set():
            task = self.store.claim()
            if task is None:
                self.stop.wait(.15)
                continue
            try:
                result = self.runner(task, lambda: self.stop.is_set() or bool(self.store.get(task['id'])['cancel']))
                # Output bound applies before database persistence and download.
                if len(json.dumps(result).encode()) > 4_000_000:
                    raise ValueError('result too large')
            except Exception:
                result = {'status': 'failed', 'evidence': self.mode,
                          'error': '任务执行失败，请检查本机模型配置、镜像和执行产物。异常原文未公开。'}
            self.store.finish(task['id'], result, interrupted=self.stop.is_set())

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, kind='application/json; charset=utf-8', download=False):
            payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="task-result.json"')
            self.end_headers()
            self.wfile.write(payload)

        def boundary(self, write=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected:
                self.send(403, {'error': '仅允许本机地址'})
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin != 'http://' + expected:
                self.send(403, {'error': '拒绝跨站请求'})
                return False
            if write and not secrets.compare_digest(self.headers.get('X-Task-Token', ''), app.token):
                self.send(403, {'error': '无效的任务令牌'})
                return False
            return True

        def do_GET(self):
            if not self.boundary():
                return
            path = urlsplit(self.path).path
            if path in {'/', '/app.js', '/style.css'}:
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
                return self.send(200, (STATIC / name).read_bytes(), mime)
            if path == '/api/info':
                return self.send(200, {'schema': 1, 'mode': app.mode, 'cases': app.cases, 'token': app.token, 'can_submit': app.mode != 'evidence'})
            if path == '/api/tasks':
                return self.send(200, {'tasks': app.store.list()})
            match = re.fullmatch(r'/api/tasks/([a-f0-9]{32})(/download)?', path)
            if match:
                try:
                    return self.send(200, app.store.get(match[1]), download=bool(match[2]))
                except KeyError:
                    return self.send(404, {'error': '任务不存在'})
            self.send(404, {'error': '地址不存在'})

        def do_POST(self):
            if not self.boundary(True):
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                from models.adapter import strict_json
                body = strict_json(self.rfile.read(size).decode('utf-8'))
                path = urlsplit(self.path).path
                if path == '/api/tasks':
                    return self.send(202, app.submit(body))
                match = re.fullmatch(r'/api/tasks/([a-f0-9]{32})/cancel', path)
                if match and body == {}:
                    return self.send(200, app.store.cancel(match[1]))
                self.send(404, {'error': '地址不存在'})
            except KeyError:
                self.send(404, {'error': '任务不存在'})
            except (ValueError, TypeError, UnicodeError):
                self.send(400, {'error': '请求格式无效，请选择已审核任务'})
    return Handler



def import_saved_evidence(store):
    """Import only the reviewed, frozen P06 bundle; never fetch browser paths."""
    import hashlib
    data = (PROJECT_ROOT / 'evidence/p06-real-workflow.json').read_bytes()
    saved = json.loads(data)
    if saved.get('schema') != 1 or saved['fixture_manifest'] != load_cases()[2]:
        raise ValueError('saved evidence schema or fixture manifest mismatch')
    digest = hashlib.sha256(data).hexdigest()
    rows = saved['cases'] + ([saved['feedback_child']] if saved.get('feedback_child') else [])
    for row in rows:
        if row['status'] != 'verified' or not re.fullmatch(r'[a-f0-9]{32}', row['task_id']):
            raise ValueError('invalid reviewed evidence identity')
        output = {'status': row['status'], 'evidence': 'saved-real', 'workflow': row,
                  'coverage': row.get('coverage', {}), 'bundle_sha256': digest,
                  'notice': 'P06历史真实运行证据，只读展示，本机未重新执行。',
                  'logs': ['已载入P06历史真实证据。原执行日志位于原开发机，不将其宿主路径作为可下载文件。']}
        now = time.time()
        with store.connect() as db:
            existing = db.execute('SELECT mode FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            if existing and existing[0] != 'evidence':
                raise ValueError('saved evidence ID conflicts with an existing task')
            db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                       (row['task_id'], row['case_id'], 'evidence', 'succeeded', 0, now, now, json.dumps(output)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--data', type=Path, default=PROJECT_ROOT / 'artifacts/web')
    p.add_argument('--enable-real', action='store_true')
    p.add_argument('--image-id')
    p.add_argument('--model', help='explicit authorized model override; does not change .env')
    p.add_argument('--feedback', action='store_true', help='run a coverage child after a verified parent')
    p.add_argument('--evidence', action='store_true', help='view fixed saved real P06 evidence without executing')
    args = p.parse_args()
    if args.enable_real and args.evidence:
        p.error('--enable-real and --evidence are mutually exclusive')
    if args.feedback and not args.enable_real:
        p.error('--feedback requires --enable-real')
    if args.enable_real and not re.fullmatch(r'sha256:[0-9a-f]{64}', args.image_id or ''):
        p.error('真实模式需要 --image-id sha256:<64位镜像摘要>')
    # Prevent two processes from executing paid tasks from the same database.
    args.data.mkdir(parents=True, exist_ok=True)
    import os
    lock = args.data / 'server.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        p.error('数据目录已有服务或遗留锁，请确认旧服务停止后再处理 server.lock')
    app = None
    try:
        os.write(fd, str(os.getpid()).encode())
        app = Application(Store(args.data / 'tasks.sqlite3'), mode='evidence' if args.evidence else ('real' if args.enable_real else 'demo'),
                          runner=real_runner(args.image_id, args.data / 'runs', model=args.model, feedback=args.feedback) if args.enable_real else None)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(app))
        if args.evidence:
            import_saved_evidence(app.store)
        else:
            app.start()
        print(f'http://127.0.0.1:{server.server_port}  mode={app.mode}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if app:
            app.close()
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
