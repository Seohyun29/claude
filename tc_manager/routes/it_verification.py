"""
================================================================================
  IT 검증 모듈 (it_verification.py)
================================================================================

[이 모듈이 하는 일]
  IT 검증 테스트의 로그를 웹에서 저장/관리한다. (기존 로컬 exe를 대체)

  두 가지 기능:
    1) workitems 등록 : 프로젝트별 workitems 엑셀을 올려두면 SITL 목록을 파싱해 DB에 저장
    2) 로그 저장      : 프로젝트→보드→도메인 선택 → 해당 SITL 목록에서 하나 골라
                        로그 작성 + 결과(Pass/Fail/N/A) → 서버에 txt로 저장 + DB 기록

[SITL 목록의 출처]
  workitems 엑셀을 프로젝트 단위로 미리 등록해둔다. (workitems가 프로젝트 단위로 나오므로)
  엑셀 컬럼(이름이 여러 개인 것은 모두 인식):
    · SITL ID  (또는 'SITL ID (CI TC)')
    · ID / Sub System / IP / Title
    · Automated  (또는 'CI Test Automated')
    · Domain     ← misc / sfi / linux / android / baremetal linux

[저장 방식]
  · DB(verification_log)에 로그 1건씩 기록
  · 동시에 서버에 txt 파일로도 저장
      파일명 : 프로젝트_보드_도메인_SITL_결과_날짜.txt
               예) IDCEVO_EVB-01_linux_SITL_1024_PASS_20260702.txt
      폴더   : logs/날짜/프로젝트_보드_도메인/  (검증이 매일 이뤄지므로 날짜별로 모음)
               예) logs/2026-07-02/IDCEVO_EVB-01_linux/

[기존 앱(tc_manager)에 붙이는 방법]  ※ app.py 안 create_app() 에 3줄 추가
    from routes.it_verification import itverify_bp, init_itverify_db
    init_itverify_db()
    app.register_blueprint(itverify_bp)
  base.html 사이드바에 링크:
    <a href="{{ url_for('it_verification.log_view') }}">🧪 IT 검증</a>

[접속 주소]  http://[서버IP]:5000/itverify/
================================================================================
"""

import os
import re
import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, send_file)
from functools import wraps
from database import get_db

itverify_bp = Blueprint('it_verification', __name__, url_prefix='/itverify')

# 로그 txt 파일이 저장될 최상위 폴더 (이 파일 기준 ../logs)
LOG_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')

# 검증 도메인 5종 (workitems의 Domain 값과 매칭. 소문자로 비교)
DOMAINS = ['misc', 'sfi', 'linux', 'android', 'baremetal linux']

# 미리 정해둔 프로젝트 목록 (여기에 새 프로젝트를 추가하면 드롭다운에 나옴)
PRESET_PROJECTS = [
    'IDCevo_sop26v2',
    'IDCevo_sop27v2',
    'IDCevo_sop28v2',
    'IDCevo_sop28v3',
]

# 결과값
RESULTS = ['PASS', 'FAIL', 'NA']

# workitems 엑셀 헤더 후보 (이름이 여러 개인 컬럼은 별칭을 모두 인식)
COL_ALIASES = {
    'sitl':      ['SITL ID', 'SITL ID (CI TC)', 'SITL'],
    'wid':       ['ID'],
    'subsystem': ['Sub System', 'SubSystem'],
    'ip':        ['IP'],
    'title':     ['Title'],
    'automated': ['Automated', 'Automated*', 'CI Test Automated'],
    'domain':    ['Domain'],
}


# ──────────────────────────────────────────
#  공통: 로그인 데코레이터
# ──────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if 'user_id' not in session:
            return redirect(url_for('main.index'))
        return f(*a, **k)
    return wrap


# ──────────────────────────────────────────
#  DB 초기화 (app.py의 init_db 근처에서 호출)
# ──────────────────────────────────────────
def init_itverify_db():
    """
    이 모듈이 쓰는 테이블 2개를 준비한다. (기존 데이터 보존)
      · workitem       : 등록된 workitems의 각 행(SITL) 저장
      · verification_log : 저장된 검증 로그
    """
    db = get_db()
    c = db.cursor()

    # workitems 파싱 결과 (프로젝트별 SITL 목록)
    c.execute('''CREATE TABLE IF NOT EXISTS workitem (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project     TEXT NOT NULL,        -- 프로젝트명 (예: IDCEVO)
        sitl_id     TEXT,                 -- SITL ID
        wid         TEXT,                 -- ID
        subsystem   TEXT,                 -- Sub System
        ip          TEXT,                 -- IP
        title       TEXT,                 -- Title (테스트 항목명)
        automated   TEXT,                 -- Automated 여부
        domain      TEXT,                 -- misc/sfi/linux/android/baremetal linux
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # 검증 로그
    c.execute('''CREATE TABLE IF NOT EXISTS verification_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date    TEXT NOT NULL,        -- 저장 날짜 (YYYY-MM-DD)
        project     TEXT NOT NULL,
        board       TEXT NOT NULL,
        domain      TEXT NOT NULL,
        sitl_id     TEXT NOT NULL,
        title       TEXT,                 -- 해당 SITL의 제목(참고용)
        result      TEXT NOT NULL,        -- PASS / FAIL / NA
        content     TEXT,                 -- 로그 본문
        file_path   TEXT,                 -- 저장된 txt 상대경로
        created_by  INTEGER,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    db.commit()
    db.close()
    os.makedirs(LOG_ROOT, exist_ok=True)


# ──────────────────────────────────────────
#  workitems 엑셀 파싱 헬퍼
# ──────────────────────────────────────────
def _find_col(header_row, aliases):
    """헤더 행에서 별칭 중 하나와 일치하는 컬럼 index를 찾음 (없으면 None)"""
    norm = {str(v).strip(): i for i, v in enumerate(header_row) if v is not None}
    for alias in aliases:
        if alias in norm:
            return norm[alias]
    return None


def parse_workitems(file_stream):
    """
    workitems 엑셀(첫 시트)에서 SITL 행들을 파싱해 리스트로 반환.
    반환: [{'sitl_id','wid','subsystem','ip','title','automated','domain'}, ...]
    """
    import openpyxl
    wb = openpyxl.load_workbook(file_stream, read_only=True, data_only=True)
    ws = wb.active

    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []

    # 각 논리 컬럼이 엑셀 몇 번째 열인지 매핑
    idx = {key: _find_col(header, aliases) for key, aliases in COL_ALIASES.items()}

    items = []
    for row in rows:
        def cell(key):
            i = idx.get(key)
            if i is None or i >= len(row):
                return ''
            v = row[i]
            return '' if v is None else str(v).strip()

        sitl = cell('sitl')
        if not sitl:          # SITL ID 없는 행은 건너뜀
            continue
        items.append({
            'sitl_id':   sitl,
            'wid':       cell('wid'),
            'subsystem': cell('subsystem'),
            'ip':        cell('ip'),
            'title':     cell('title'),
            'automated': cell('automated'),
            'domain':    cell('domain').lower(),   # 비교 편의를 위해 소문자
        })
    wb.close()
    return items


# ══════════════════════════════════════════
#  화면 1) workitems 등록
# ══════════════════════════════════════════
@itverify_bp.route('/workitems')
@login_required
def workitems_view():
    db = get_db()
    # 등록된 프로젝트별 SITL 개수 요약
    rows = db.execute('''
        SELECT project,
               COUNT(*)          AS sitl_count,
               MAX(created_at)   AS updated_at
        FROM workitem
        GROUP BY project
        ORDER BY project
    ''').fetchall()
    db.close()

    # 드롭다운 목록: 미리 정한 프로젝트 + 이미 등록된 프로젝트 (중복 제거, 순서 유지)
    registered = [r['project'] for r in rows]
    project_options = list(PRESET_PROJECTS)
    for p in registered:
        if p not in project_options:
            project_options.append(p)

    return render_template('it_verification/workitems.html',
                           projects=rows, project_options=project_options)


@itverify_bp.route('/workitems/upload', methods=['POST'])
@login_required
def workitems_upload():
    project = request.form.get('project', '').strip()
    file = request.files.get('workitems_file')

    if not project:
        flash('프로젝트명을 입력해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))
    if not file or not file.filename:
        flash('workitems 엑셀 파일을 선택해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        flash('엑셀 파일(.xlsx)만 업로드할 수 있어요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    try:
        items = parse_workitems(file.stream)
    except Exception as e:
        flash(f'엑셀을 읽는 중 오류가 났어요: {e}', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    if not items:
        flash('SITL 데이터를 찾지 못했어요. 컬럼(SITL ID 등)을 확인해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    db = get_db()
    # 같은 프로젝트명은 갱신 개념 → 기존 것 지우고 새로 넣음
    db.execute("DELETE FROM workitem WHERE project=?", (project,))
    for it in items:
        db.execute('''INSERT INTO workitem
            (project, sitl_id, wid, subsystem, ip, title, automated, domain)
            VALUES (?,?,?,?,?,?,?,?)''',
            (project, it['sitl_id'], it['wid'], it['subsystem'],
             it['ip'], it['title'], it['automated'], it['domain']))
    db.commit()
    db.close()

    flash(f'✅ {project} workitems 등록 완료 (SITL {len(items)}건)', 'success')
    return redirect(url_for('it_verification.workitems_view'))


@itverify_bp.route('/workitems/delete/<project>', methods=['POST'])
@login_required
def workitems_delete(project):
    db = get_db()
    db.execute("DELETE FROM workitem WHERE project=?", (project,))
    db.commit()
    db.close()
    flash(f'🗑️ {project} workitems 삭제됨', 'success')
    return redirect(url_for('it_verification.workitems_view'))


# ══════════════════════════════════════════
#  화면 2) 로그 저장
# ══════════════════════════════════════════
@itverify_bp.route('/')
@login_required
def log_view():
    db = get_db()
    # 등록된 프로젝트 목록 (workitem 기준)
    projects = [r['project'] for r in db.execute(
        "SELECT DISTINCT project FROM workitem ORDER BY project").fetchall()]
    # 보드 목록 (기존 boards 테이블 재사용, 프로젝트 무관 전체)
    boards = db.execute(
        "SELECT DISTINCT name FROM boards WHERE is_active=1 ORDER BY name").fetchall()
    board_names = [b['name'] for b in boards]

    # 최근 로그 (하단 목록용) - 날짜 필터 있으면 적용
    filter_date = request.args.get('date', '').strip()
    if filter_date:
        logs = db.execute('''SELECT * FROM verification_log
                             WHERE log_date=? ORDER BY id DESC''',
                          (filter_date,)).fetchall()
    else:
        logs = db.execute('''SELECT * FROM verification_log
                             ORDER BY id DESC LIMIT 50''').fetchall()

    # 날짜 목록 (필터 드롭다운용)
    dates = [r['log_date'] for r in db.execute(
        "SELECT DISTINCT log_date FROM verification_log ORDER BY log_date DESC").fetchall()]
    db.close()

    return render_template('it_verification/log.html',
                           projects=projects, boards=board_names,
                           domains=DOMAINS, logs=logs, dates=dates,
                           filter_date=filter_date,
                           today_str=datetime.date.today().strftime('%Y-%m-%d'))


@itverify_bp.route('/sitls')
@login_required
def get_sitls():
    """프로젝트+도메인으로 SITL 목록을 조회 (JSON) — 왼쪽 목록 채우기용"""
    project = request.args.get('project', '').strip()
    domain  = request.args.get('domain', '').strip().lower()
    if not project or not domain:
        return jsonify({'ok': False, 'sitls': []})

    db = get_db()
    rows = db.execute('''SELECT sitl_id, title FROM workitem
                         WHERE project=? AND domain=?
                         ORDER BY sitl_id''', (project, domain)).fetchall()
    db.close()
    sitls = [{'sitl_id': r['sitl_id'], 'title': r['title'] or ''} for r in rows]
    return jsonify({'ok': True, 'sitls': sitls})


def _safe(s):
    """파일명/폴더명에 안전한 문자열로 (공백→_, 특수문자 제거)"""
    s = (s or '').strip().replace(' ', '-')
    return re.sub(r'[^0-9A-Za-z가-힣_\-]', '', s)


@itverify_bp.route('/log/save', methods=['POST'])
@login_required
def log_save():
    project = request.form.get('project', '').strip()
    board   = request.form.get('board', '').strip()
    domain  = request.form.get('domain', '').strip().lower()
    sitl_id = request.form.get('sitl_id', '').strip()
    result  = request.form.get('result', '').strip().upper()
    content = request.form.get('content', '').strip()

    # 필수값 검증
    if not all([project, board, domain, sitl_id, result]):
        flash('프로젝트·보드·도메인·SITL·결과를 모두 선택/입력해주세요.', 'error')
        return redirect(url_for('it_verification.log_view'))
    if result not in RESULTS:
        flash('결과는 PASS/FAIL/NA 중 하나여야 해요.', 'error')
        return redirect(url_for('it_verification.log_view'))

    # 해당 SITL의 제목(참고용) 가져오기
    db = get_db()
    wi = db.execute('''SELECT title FROM workitem
                       WHERE project=? AND domain=? AND sitl_id=? LIMIT 1''',
                    (project, domain, sitl_id)).fetchone()
    title = wi['title'] if wi else ''

    today = datetime.date.today()
    date_str    = today.strftime('%Y-%m-%d')   # 폴더용
    date_compact = today.strftime('%Y%m%d')    # 파일명용

    # 폴더:  logs/날짜/프로젝트_보드_도메인/
    combo = f"{_safe(project)}_{_safe(board)}_{_safe(domain)}"
    folder = os.path.join(LOG_ROOT, date_str, combo)
    os.makedirs(folder, exist_ok=True)

    # 파일명: 프로젝트_보드_도메인_SITL_결과_날짜.txt
    filename = f"{_safe(project)}_{_safe(board)}_{_safe(domain)}_{_safe(sitl_id)}_{result}_{date_compact}.txt"
    full_path = os.path.join(folder, filename)

    # txt 내용 구성
    txt = (
        f"[프로젝트] {project}\n"
        f"[보드] {board}\n"
        f"[도메인] {domain}\n"
        f"[SITL] {sitl_id}\n"
        f"[제목] {title}\n"
        f"[결과] {result}\n"
        f"[작성일] {date_str}\n"
        f"{'-'*40}\n"
        f"{content}\n"
    )
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(txt)

    # DB 상대경로 기록 (logs/ 아래 경로)
    rel_path = os.path.relpath(full_path, LOG_ROOT)

    db.execute('''INSERT INTO verification_log
        (log_date, project, board, domain, sitl_id, title, result, content, file_path, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (date_str, project, board, domain, sitl_id, title, result, content,
         rel_path, session.get('user_id')))
    db.commit()
    db.close()

    flash(f'💾 저장 완료: {filename}', 'success')
    return redirect(url_for('it_verification.log_view', date=date_str))


@itverify_bp.route('/log/download/<int:log_id>')
@login_required
def log_download(log_id):
    """저장된 txt 파일 다운로드"""
    db = get_db()
    log = db.execute("SELECT * FROM verification_log WHERE id=?", (log_id,)).fetchone()
    db.close()
    if not log:
        flash('로그를 찾을 수 없어요.', 'error')
        return redirect(url_for('it_verification.log_view'))
    full = os.path.join(LOG_ROOT, log['file_path'])
    if not os.path.exists(full):
        flash('파일이 서버에 없어요.', 'error')
        return redirect(url_for('it_verification.log_view'))
    return send_file(full, as_attachment=True,
                     download_name=os.path.basename(full))


@itverify_bp.route('/log/delete/<int:log_id>', methods=['POST'])
@login_required
def log_delete(log_id):
    db = get_db()
    log = db.execute("SELECT * FROM verification_log WHERE id=?", (log_id,)).fetchone()
    if log:
        # 서버 txt도 삭제
        full = os.path.join(LOG_ROOT, log['file_path'] or '')
        try:
            if log['file_path'] and os.path.exists(full):
                os.remove(full)
        except OSError:
            pass
        db.execute("DELETE FROM verification_log WHERE id=?", (log_id,))
        db.commit()
    db.close()
    flash('🗑️ 로그가 삭제되었습니다.', 'success')
    return redirect(url_for('it_verification.log_view'))
