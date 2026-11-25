import os
import re
import subprocess

from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

SERVICES = {
    'frontendService': 'frontend-dev',
    'backendService': 'backend-dev',
    'codingAgentService': 'coding-agent-ws',
}
REPO_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
SKIP_DIRS = {'node_modules', '.git', 'venv', '.venv', '__pycache__', '.aws-sam', 'dist'}
MAX_SEARCH_DEPTH = 4
BUCKET_NAME = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
UNIT_DIR = os.path.expanduser('~/.config/systemd/user')
# unit -> the status key whose path becomes its WorkingDirectory
UNIT_WORKDIR_KEY = {
    'coding-agent-ws': 'codingAgentPath',
    'frontend-dev': 'frontendPath',
    'backend-dev': 'samPath',
}
CLONE_TIMEOUT = 540  # run gunicorn with --timeout larger than this, or it kills the request


ENV_FILE = os.path.expanduser('~/.config/dev-box.env')


def get_prod_api_server():
    """PROD_API_SERVER from the environment, or else from ~/.config/dev-box.env. Never from a request."""
    value = os.environ.get('PROD_API_SERVER')
    if value:
        return value
    try:
        with open(ENV_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('PROD_API_SERVER='):
                    return line.split('=', 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ''


def check_token(token):
    """True only if the prod API confirms `token` belongs to an admin. Fails closed."""
    server = get_prod_api_server()
    if not token or not isinstance(token, str) or not server:
        return False
    try:
        resp = requests.post(
            server.rstrip('/') + '/Prod/api/user/verify-token',
            json={}, headers={'Authorization': token}, timeout=10)
        if resp.status_code != 200:
            return False
        return resp.json().get('isAdmin') is True
    except (requests.RequestException, ValueError):
        return False


def find_dir(root, name):
    """First directory called `name` under root (shallowest first, alphabetical), or None."""
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if dirpath.count(os.sep) - root_depth >= MAX_SEARCH_DEPTH:
            dirnames[:] = []
        if name in dirnames:
            return os.path.join(dirpath, name)
    return None


def service_status(unit):
    """ActiveState of a systemd user unit (active, inactive, failed, ...), or 'not-installed'."""
    env = dict(os.environ)
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    try:
        out = subprocess.run(
            ['systemctl', '--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState'],
            capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    props = dict(line.split('=', 1) for line in out.stdout.splitlines() if '=' in line)
    if out.returncode != 0 or 'ActiveState' not in props:
        return 'unknown'
    if props.get('LoadState') == 'not-found':
        return 'not-installed'
    return props['ActiveState']


def valid_repo(repo):
    # `repo` becomes a path under ~, so allow a plain folder name only (no '/', no '..').
    return isinstance(repo, str) and bool(REPO_NAME.match(repo)) and not repo.endswith('..')


def systemctl_env():
    env = dict(os.environ)
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return env


def run_cmd(argv, env=None, timeout=60):
    """Run a command without a shell; returns (exit_code, combined_output)."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, f'timed out after {timeout}s'
    except OSError as e:
        return -1, str(e)
    return out.returncode, (out.stdout + out.stderr).strip()


def build_status(repo):
    result = {
        'repoPath': None,
        'frontendPath': None,
        'samPath': None,
        'codingAgentPath': None,
        'frontendService': None,
        'backendService': None,
        'codingAgentService': None,
    }

    # ~/repo exists?
    repo_path = os.path.join(os.path.expanduser('~'), repo)
    if os.path.isdir(repo_path):
        result['repoPath'] = repo_path

        # frontendPath: first subfolder named "frontend"; samPath: its parent
        frontend_path = find_dir(repo_path, 'frontend')
        if frontend_path:
            result['frontendPath'] = frontend_path
            result['samPath'] = os.path.dirname(frontend_path)

            # codingAgentPath: the coding-agent folder next to frontend
            coding_agent_path = os.path.join(result['samPath'], 'coding-agent')
            if os.path.isdir(coding_agent_path):
                result['codingAgentPath'] = coding_agent_path

    # status of the three user services
    for key, unit in SERVICES.items():
        result[key] = service_status(unit)
    return result


def set_working_directory(unit, path):
    """Point the unit's WorkingDirectory at `path`. Returns True if the file changed."""
    if '\n' in path or '\r' in path:
        raise ValueError('unsupported characters in path')
    unit_file = os.path.join(UNIT_DIR, unit + '.service')
    with open(unit_file, encoding='utf-8') as f:
        text = f.read()
    line = 'WorkingDirectory=' + path.replace('%', '%%')  # '%' starts a systemd specifier
    if re.search(r'^WorkingDirectory=.*$', text, flags=re.M):
        new = re.sub(r'^WorkingDirectory=.*$', lambda m: line, text, count=1, flags=re.M)
    elif '[Service]' in text:
        new = text.replace('[Service]', '[Service]\n' + line, 1)
    else:
        raise ValueError(f'{unit_file} has no [Service] section')
    if new == text:
        return False
    tmp = unit_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
    os.replace(tmp, unit_file)
    return True


@app.route('/dev-box/status', methods=['POST'])
def get_dev_box_status():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    if not check_token(token):
        return jsonify({'error': 'Cannot verify admin'}), 401

    repo = data.get('repo')
    if not valid_repo(repo):
        return jsonify({'error': 'Invalid repo name'}), 400
    return jsonify(build_status(repo))


@app.route('/dev-box/setup', methods=['POST'])
def setup_dev_box():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    if not check_token(token):
        return jsonify({'error': 'Cannot verify admin'}), 401

    repo = data.get('repo')
    git_bucket = data.get('gitBucket')
    if not valid_repo(repo):
        return jsonify({'error': 'Invalid repo name'}), 400
    messages = []

    # if ~/repo does not exist, check it out from S3: git clone s3://{git_bucket}/{repo} ~/{repo}
    repo_path = os.path.join(os.path.expanduser('~'), repo)
    if not os.path.isdir(repo_path):
        if not isinstance(git_bucket, str) or not BUCKET_NAME.match(git_bucket):
            return jsonify({'error': 'Invalid gitBucket'}), 400
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        code, out = run_cmd(['git', 'clone', f's3://{git_bucket}/{repo}', repo_path],
                            env=env, timeout=CLONE_TIMEOUT)
        if code != 0:
            return jsonify({'error': 'git clone failed', 'details': out[-2000:]}), 500
        messages.append(f'Cloned s3://{git_bucket}/{repo} into {repo_path}')
    else:
        messages.append(f'{repo_path} already exists; skipped clone')

    # find codingAgentPath, frontendPath and samPath as before
    paths = build_status(repo)
    missing = [k for k in UNIT_WORKDIR_KEY.values() if not paths[k]]
    if missing:
        return jsonify({'error': 'Could not find ' + ', '.join(missing) + ' in the repo',
                        **paths, 'messages': messages}), 500

    # update WorkingDirectory in the three user service files
    changed = set()
    for unit, key in UNIT_WORKDIR_KEY.items():
        try:
            if set_working_directory(unit, paths[key]):
                changed.add(unit)
                messages.append(f'Updated {unit}.service: WorkingDirectory={paths[key]}')
        except (OSError, ValueError) as e:
            return jsonify({'error': f'Cannot update {unit}.service: {e}',
                            **paths, 'messages': messages}), 500
    env = systemctl_env()
    code, out = run_cmd(['systemctl', '--user', 'daemon-reload'], env=env)
    if code != 0:
        return jsonify({'error': 'daemon-reload failed', 'details': out, **paths, 'messages': messages}), 500

    # Start the services. A service already running with an old directory is restarted.
    # --no-block: backend-dev runs `sam build` first, which can outlast the HTTP request.
    for verb, units in (('restart', [u for u in UNIT_WORKDIR_KEY if u in changed]),
                        ('start', [u for u in UNIT_WORKDIR_KEY if u not in changed])):
        if not units:
            continue
        code, out = run_cmd(['systemctl', '--user', verb, '--no-block', *units], env=env)
        if code != 0:
            messages.append(f'systemctl --user {verb} {" ".join(units)} failed: {out}')
        else:
            messages.append(f'Requested {verb} of {", ".join(units)}')

    # finally return the result like get_dev_box_status
    result = build_status(repo)
    result['messages'] = messages
    return jsonify(result)
