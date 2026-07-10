import requests
import json
import datetime
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, session, jsonify)
from database import get_db
from functools import wraps

# SSL 경고 숨기기 (사내 인증서 문제 대비)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

integration_bp = Blueprint('integration', __name__, url_prefix='/admin/integration')

# 장소당 하루 최대 IT 일정 수 — config.py 의 MAX_IT_PER_LOCATION 한 곳에서 관리
from config import MAX_IT_PER_LOCATION


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('관리자만 접근할 수 있습니다.', 'error')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
#  설정 읽기 헬퍼
# ─────────────────────────────────────────────
def get_jira_config():
    db = get_db()
    row = db.execute("SELECT * FROM jira_config ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return dict(row) if row else {}

def get_jenkins_config():
    db = get_db()
    row = db.execute("SELECT * FROM jenkins_config ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return dict(row) if row else {}


# ─────────────────────────────────────────────
#  Jenkins API 헬퍼
# ─────────────────────────────────────────────
def jenkins_request(path, cfg, tree=None):
    """Jenkins REST API 호출"""
    url = cfg['server_url'].rstrip('/') + path
    if tree:
        url += ('&' if '?' in url else '?') + f'tree={tree}'
    auth = (cfg['username'], cfg['token'])
    try:
        resp = requests.get(url, auth=auth, timeout=15, verify=False)
        if resp.status_code == 200:
            try:
                return True, resp.json()
            except Exception:
                return True, {}
        return False, f'HTTP {resp.status_code}: {resp.text[:300]}'
    except requests.exceptions.ConnectionError:
        return False, '서버에 연결할 수 없습니다. URL을 확인해주세요.'
    except requests.exceptions.Timeout:
        return False, '연결 시간 초과 (15초).'
    except Exception as e:
        return False, str(e)


def jenkins_get_folder_jobs(path, cfg):
    """
    폴더 구조 Jenkins에서 재귀적으로 Job 목록 수집
    path: '' (루트) 또는 '/job/FolderName'
    반환: [{'name', 'full_path', 'type', 'last_build', 'color'}, ...]
    """
    api_path = path + '/api/json'
    ok, data = jenkins_request(api_path, cfg,
        tree='name,url,color,jobs[name,url,color,jobs[name,url,color],'
             'lastBuild[number,timestamp,result,displayName]],'
             'lastBuild[number,timestamp,result,displayName]')
    if not ok:
        return []

    result = []
    jobs = data.get('jobs', [])
    for job in jobs:
        job_type = job.get('_class', '')
        is_folder = 'Folder' in job_type or 'WorkflowMultiBranchProject' in job_type
        sub_jobs  = job.get('jobs')

        if is_folder or sub_jobs is not None:
            # 폴더 → 하위 Job 포함
            sub_path = path + '/job/' + job['name']
            sub_result = jenkins_get_folder_jobs(sub_path, cfg)
            if sub_result:
                result.append({
                    'type':       'folder',
                    'name':       job['name'],
                    'full_path':  sub_path,
                    'children':   sub_result,
                    'color':      job.get('color', ''),
                })
            # 하위가 없어도 폴더 자체 표시
            else:
                result.append({
                    'type':      'folder',
                    'name':      job['name'],
                    'full_path': sub_path,
                    'children':  [],
                    'color':     job.get('color', ''),
                })
        else:
            # 일반 Job
            lb = job.get('lastBuild') or {}
            ts = lb.get('timestamp', 0)
            build_date = ''
            if ts:
                build_date = datetime.datetime.fromtimestamp(
                    ts / 1000
                ).strftime('%Y-%m-%d %H:%M')
            result.append({
                'type':        'job',
                'name':        job['name'],
                'full_path':   path + '/job/' + job['name'],
                'color':       job.get('color', ''),
                'last_build':  lb.get('number'),
                'last_result': lb.get('result', ''),
                'build_date':  build_date,
                'display':     lb.get('displayName', ''),
            })
    return result


def get_job_last_build(job_path, cfg):
    """특정 Job의 최신 빌드 정보 조회"""
    ok, data = jenkins_request(
        job_path + '/lastBuild/api/json', cfg,
        tree='number,timestamp,result,displayName,duration,building,url'
    )
    if not ok:
        return None
    ts = data.get('timestamp', 0)
    build_date = ''
    if ts:
        build_date = datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M')
    dur = data.get('duration', 0)
    duration_str = f'{dur // 60000}분 {(dur % 60000) // 1000}초' if dur else ''
    return {
        'number':    data.get('number'),
        'result':    data.get('result', 'UNKNOWN'),
        'building':  data.get('building', False),
        'date':      build_date,
        'duration':  duration_str,
        'display':   data.get('displayName', ''),
        'url':       data.get('url', ''),
    }


# ─────────────────────────────────────────────
#  Jira API 헬퍼
# ─────────────────────────────────────────────
def jira_request(method, path, cfg, **kwargs):
    url = cfg['server_url'].rstrip('/') + '/rest/api/2' + path
    auth = (cfg['email'], cfg['token'])
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Atlassian-Token': 'no-check',
    }
    try:
        resp = requests.request(method, url, auth=auth, headers=headers,
                                timeout=10, verify=False, **kwargs)
        if resp.status_code in (200, 201, 204):
            return True, resp.json() if resp.text else {}
        return False, f'HTTP {resp.status_code}: {resp.text[:200]}'
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
#  메인 설정 화면
# ─────────────────────────────────────────────
@integration_bp.route('/')
@admin_required
def index():
    jira_cfg    = get_jira_config()
    jenkins_cfg = get_jenkins_config()
    return render_template('integration/index.html',
                           jira_cfg=jira_cfg,
                           jenkins_cfg=jenkins_cfg)


# ─────────────────────────────────────────────
#  Jira 설정 저장 / 테스트
# ─────────────────────────────────────────────
@integration_bp.route('/jira/save', methods=['POST'])
@admin_required
def save_jira():
    server_url  = request.form.get('server_url', '').strip().rstrip('/')
    email       = request.form.get('email', '').strip()
    token       = request.form.get('token', '').strip()
    project_key = request.form.get('project_key', '').strip()
    is_enabled  = 1 if request.form.get('is_enabled') else 0

    db = get_db()
    existing = db.execute("SELECT id FROM jira_config LIMIT 1").fetchone()
    if existing:
        db.execute('''UPDATE jira_config SET
            server_url=?, email=?, token=?, project_key=?, is_enabled=?,
            updated_at=datetime('now','localtime')
        ''', (server_url, email, token, project_key, is_enabled))
    else:
        db.execute('''INSERT INTO jira_config
            (server_url, email, token, project_key, is_enabled)
            VALUES (?, ?, ?, ?, ?)
        ''', (server_url, email, token, project_key, is_enabled))
    db.commit(); db.close()
    flash('✅ Jira 설정이 저장되었습니다.', 'success')
    return redirect(url_for('integration.index'))


@integration_bp.route('/jira/test', methods=['POST'])
@admin_required
def test_jira():
    cfg = get_jira_config()
    if not cfg.get('server_url') or not cfg.get('token'):
        return jsonify({'ok': False, 'msg': '먼저 설정을 저장해주세요.'})
    ok, data = jira_request('GET', '/myself', cfg)
    if ok:
        name = data.get('displayName', data.get('emailAddress', '?'))
        return jsonify({'ok': True, 'msg': f'✅ 연결 성공! 로그인 계정: {name}'})
    return jsonify({'ok': False, 'msg': f'❌ 연결 실패: {data}'})


# ─────────────────────────────────────────────
#  Jenkins 설정 저장 / 테스트
# ─────────────────────────────────────────────
@integration_bp.route('/jenkins/save', methods=['POST'])
@admin_required
def save_jenkins():
    server_url  = request.form.get('server_url', '').strip().rstrip('/')
    username    = request.form.get('username', '').strip()
    token       = request.form.get('token', '').strip()
    is_enabled  = 1 if request.form.get('is_enabled') else 0
    # Job 필터: 줄바꿈으로 구분, 각 줄 앞뒤 공백 제거, 빈 줄 제거
    raw_filters = request.form.get('job_filters', '')
    job_filters = '\n'.join(
        line.strip() for line in raw_filters.splitlines() if line.strip()
    )

    db = get_db()
    existing = db.execute("SELECT id FROM jenkins_config LIMIT 1").fetchone()
    if existing:
        db.execute('''UPDATE jenkins_config SET
            server_url=?, username=?, token=?, job_filters=?, is_enabled=?,
            updated_at=datetime('now','localtime')
        ''', (server_url, username, token, job_filters, is_enabled))
    else:
        db.execute('''INSERT INTO jenkins_config
            (server_url, username, token, job_filters, is_enabled)
            VALUES (?, ?, ?, ?, ?)
        ''', (server_url, username, token, job_filters, is_enabled))
    db.commit(); db.close()
    flash('✅ Jenkins 설정이 저장되었습니다.', 'success')
    return redirect(url_for('integration.index'))


@integration_bp.route('/jenkins/test', methods=['POST'])
@admin_required
def test_jenkins():
    cfg = get_jenkins_config()
    if not cfg.get('server_url') or not cfg.get('token'):
        return jsonify({'ok': False, 'msg': '먼저 설정을 저장해주세요.'})
    ok, data = jenkins_request('/api/json', cfg, tree='nodeName,version')
    if ok:
        version = data.get('version', data.get('nodeName', 'OK'))
        return jsonify({'ok': True, 'msg': f'✅ 연결 성공! Jenkins 버전: {version}'})
    return jsonify({'ok': False, 'msg': f'❌ 연결 실패: {data}'})


# ─────────────────────────────────────────────
#  Jenkins: Job 브라우저 (폴더 구조 탐색)
# ─────────────────────────────────────────────
@integration_bp.route('/jenkins/jobs')
@admin_required
def jenkins_jobs():
    cfg = get_jenkins_config()
    if not cfg.get('is_enabled') or not cfg.get('server_url'):
        flash('Jenkins 연동이 비활성화되어 있습니다.', 'error')
        return redirect(url_for('integration.index'))

    folder_path  = request.args.get('path', '')
    show_all     = request.args.get('all', '0') == '1'

    # 등록된 Job 필터 목록
    job_filters = [
        line.strip()
        for line in (cfg.get('job_filters') or '').splitlines()
        if line.strip()
    ]
    use_filter = bool(job_filters) and not show_all and not folder_path

    if use_filter:
        # ── 필터 모드: 등록된 Job 경로만 직접 조회 ──
        jobs = []
        for job_path in job_filters:
            # 마지막 /job/NAME 에서 이름 추출
            job_name = job_path.rstrip('/').split('/job/')[-1] if '/job/' in job_path else job_path
            build = get_job_last_build(job_path, cfg)
            if build:
                jobs.append({
                    'name':        job_name,
                    'full_path':   job_path,
                    'is_folder':   False,
                    'color':       '',
                    'last_build':  build['number'],
                    'last_result': build['result'],
                    'build_date':  build['date'],
                })
            else:
                # 빌드 정보 없어도 Job은 표시
                jobs.append({
                    'name':        job_name,
                    'full_path':   job_path,
                    'is_folder':   False,
                    'color':       '',
                    'last_build':  None,
                    'last_result': '',
                    'build_date':  '',
                })
        breadcrumb = []

    else:
        # ── 전체 탐색 모드: 폴더 구조 탐색 ──
        ok, data = jenkins_request(
            (folder_path or '') + '/api/json', cfg,
            tree='name,url,color,jobs[name,url,color,'
                 'lastBuild[number,timestamp,result,displayName]]'
        )
        if not ok:
            flash(f'Jenkins 조회 실패: {data}', 'error')
            return redirect(url_for('integration.index'))

        jobs = []
        for job in data.get('jobs', []):
            job_type  = job.get('_class', '')
            is_folder = ('Folder' in job_type or
                         'WorkflowMultiBranch' in job_type or
                         job.get('jobs') is not None)
            lb = job.get('lastBuild') or {}
            ts = lb.get('timestamp', 0)
            build_date = ''
            if ts:
                build_date = datetime.datetime.fromtimestamp(
                    ts / 1000).strftime('%Y-%m-%d %H:%M')

            jobs.append({
                'name':        job['name'],
                'full_path':   (folder_path or '') + '/job/' + job['name'],
                'is_folder':   is_folder,
                'color':       job.get('color', ''),
                'last_build':  lb.get('number'),
                'last_result': lb.get('result', ''),
                'build_date':  build_date,
            })

        # 브레드크럼
        breadcrumb = []
        if folder_path:
            parts = folder_path.strip('/').split('/job/')
            acc = ''
            for p in parts:
                if not p: continue
                acc = acc + '/job/' + p if acc else '/job/' + p
                breadcrumb.append({'name': p, 'path': acc})

    db = get_db()
    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()
    boards_all = db.execute(
        "SELECT b.*, p.name as project_name FROM boards b "
        "JOIN projects p ON b.project_id=p.id WHERE b.is_active=1 ORDER BY p.name, b.name"
    ).fetchall()
    db.close()

    return render_template('integration/jenkins_jobs.html',
                           jobs=jobs,
                           folder_path=folder_path,
                           breadcrumb=breadcrumb,
                           use_filter=use_filter,
                           job_filters=job_filters,
                           show_all=show_all,
                           projects=projects,
                           boards_all=boards_all,
                           cfg=cfg)


# ─────────────────────────────────────────────
#  Jenkins: 특정 Job 빌드 상태 조회 (AJAX)
# ─────────────────────────────────────────────
@integration_bp.route('/jenkins/job_status')
@admin_required
def jenkins_job_status():
    cfg      = get_jenkins_config()
    job_path = request.args.get('path', '')
    if not job_path:
        return jsonify({'ok': False, 'msg': 'Job 경로 없음'})

    build = get_job_last_build(job_path, cfg)
    if not build:
        return jsonify({'ok': False, 'msg': '빌드 정보를 가져올 수 없습니다.'})
    return jsonify({'ok': True, 'build': build})


# ─────────────────────────────────────────────
#  IT 일정 관리 화면
# ─────────────────────────────────────────────
@integration_bp.route('/it_schedule')
@admin_required
def it_schedule():
    db = get_db()
    today = datetime.date.today().strftime('%Y-%m-%d')

    schedules = db.execute('''
        SELECT s.*, p.name as project_name, b.name as board_name
        FROM it_schedule s
        LEFT JOIN projects p ON s.project_id = p.id
        LEFT JOIN boards b ON s.board_id = b.id
        ORDER BY s.scheduled_date DESC, s.location
    ''').fetchall()

    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()
    boards_all = db.execute(
        "SELECT b.*, p.name as project_name FROM boards b "
        "JOIN projects p ON b.project_id=p.id WHERE b.is_active=1 ORDER BY p.name, b.name"
    ).fetchall()
    db.close()

    return render_template('integration/it_schedule.html',
                           schedules=schedules,
                           projects=projects,
                           boards_all=boards_all,
                           max_per_day=MAX_IT_PER_LOCATION,
                           today=today)


# ─────────────────────────────────────────────
#  IT 일정 등록
# ─────────────────────────────────────────────
@integration_bp.route('/it_schedule/add', methods=['POST'])
@admin_required
def add_it_schedule():
    project_id     = request.form.get('project_id', type=int)
    board_id       = request.form.get('board_id', type=int)
    location       = request.form.get('location', '')
    scheduled_date = request.form.get('scheduled_date', '')
    jenkins_job    = request.form.get('jenkins_job', '').strip()
    notes          = request.form.get('notes', '').strip()

    if not project_id or not location or not scheduled_date:
        flash('프로젝트, 장소, 날짜는 필수입니다.', 'error')
        return redirect(url_for('integration.it_schedule'))

    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM it_schedule WHERE location=? AND scheduled_date=?",
        (location, scheduled_date)
    ).fetchone()[0]
    if count >= MAX_IT_PER_LOCATION:
        flash(f'⚠️ {location}는 하루 최대 {MAX_IT_PER_LOCATION}개까지 등록 가능합니다.', 'error')
        db.close()
        return redirect(url_for('integration.it_schedule'))

    db.execute('''
        INSERT INTO it_schedule
            (location, project_id, board_id, scheduled_date, jenkins_job, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (location, project_id, board_id or None,
          scheduled_date, jenkins_job, notes))
    db.commit(); db.close()
    flash('✅ IT 일정이 등록되었습니다.', 'success')
    return redirect(url_for('integration.it_schedule'))


# ─────────────────────────────────────────────
#  IT 일정 삭제
# ─────────────────────────────────────────────
@integration_bp.route('/it_schedule/delete/<int:schedule_id>', methods=['POST'])
@admin_required
def delete_it_schedule(schedule_id):
    db = get_db()
    db.execute("DELETE FROM it_schedule WHERE id=?", (schedule_id,))
    db.commit(); db.close()
    flash('🗑️ IT 일정이 삭제되었습니다.', 'success')
    return redirect(url_for('integration.it_schedule'))


# ─────────────────────────────────────────────
#  Jenkins: Job을 IT 일정에 등록 (Job 브라우저에서)
# ─────────────────────────────────────────────
@integration_bp.route('/jenkins/schedule', methods=['POST'])
@admin_required
def jenkins_schedule():
    project_id     = request.form.get('project_id', type=int)
    board_id       = request.form.get('board_id', type=int)
    location       = request.form.get('location', '')
    scheduled_date = request.form.get('scheduled_date', '')
    jenkins_job    = request.form.get('jenkins_job', '').strip()
    notes          = request.form.get('notes', '').strip()

    if not project_id or not location or not scheduled_date:
        flash('프로젝트, 장소, 날짜를 모두 입력해주세요.', 'error')
        return redirect(url_for('integration.jenkins_jobs'))

    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM it_schedule WHERE location=? AND scheduled_date=?",
        (location, scheduled_date)
    ).fetchone()[0]
    if count >= MAX_IT_PER_LOCATION:
        flash(f'⚠️ {location}는 하루 최대 {MAX_IT_PER_LOCATION}개까지 등록 가능합니다.', 'error')
        db.close()
        return redirect(url_for('integration.jenkins_jobs'))

    db.execute('''
        INSERT INTO it_schedule
            (location, project_id, board_id, scheduled_date, jenkins_job, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (location, project_id, board_id or None,
          scheduled_date, jenkins_job, notes))
    db.commit(); db.close()
    flash('✅ IT 일정에 등록되었습니다.', 'success')
    return redirect(url_for('integration.it_schedule'))
