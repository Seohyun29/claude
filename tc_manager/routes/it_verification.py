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
import io
import zipfile
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

# 결과값
RESULTS = ['PASS', 'FAIL', 'NA']

# workitems 엑셀 헤더 후보 (이름이 여러 개인 컬럼은 별칭을 모두 인식)
COL_ALIASES = {
    'sitl':   ['SITL ID', 'SITL ID (CI TC)', 'SITL'],
    'title':  ['Title'],
    'domain': ['Domain'],
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
        project     TEXT NOT NULL,        -- 프로젝트 (예: IDCEVO_SOP28V2)
        board       TEXT,                 -- 보드 (미사용, 예비)
        sitl_id     TEXT,                 -- SITL ID (엑셀 SITL ID 값)
        title       TEXT,                 -- Title (테스트 항목명)
        domain      TEXT,                 -- misc/sfi/linux/android/baremetal linux
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')
    # 기존 workitem 테이블에 없는 컬럼 마이그레이션
    wi_cols = [r[1] for r in c.execute("PRAGMA table_info(workitem)").fetchall()]
    for col in ('board', 'title'):
        if col not in wi_cols:
            c.execute(f"ALTER TABLE workitem ADD COLUMN {col} TEXT")

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
#  기존 리뷰 기능의 read_excel_file(xlwings→openpyxl→csv)을 재사용해
#  DRM 걸린 엑셀도 읽는다. 엑셀에서 SITL ID / Title / Domain 컬럼을 뽑음.
def _match_header(headers, aliases):
    """헤더 목록에서 별칭 중 하나와 일치하는 실제 헤더명을 반환 (없으면 None)"""
    norm = {h.strip(): h for h in headers if h}
    for alias in aliases:
        if alias in norm:
            return norm[alias]
    return None


def parse_workitems_excel(file_storage):
    """
    workitems 엑셀을 읽어 (project는 나중에 붙임) SITL 목록을 반환.
    반환: (items, method, error_msg)
      items = [{'sitl_id','title','domain'}, ...]
    """
    # 기존 리뷰 모듈의 검증된 엑셀 리더 재사용
    from routes.review import read_excel_file
    rows, method, error_msg = read_excel_file(file_storage)
    if error_msg:
        return [], method, error_msg
    if not rows:
        return [], method, '엑셀에서 데이터를 찾지 못했어요.'

    # 실제 헤더 이름 매칭 (첫 행 dict의 키가 헤더)
    headers = list(rows[0].keys())
    col_sitl   = _match_header(headers, COL_ALIASES['sitl'])
    col_title  = _match_header(headers, COL_ALIASES['title'])
    col_domain = _match_header(headers, COL_ALIASES['domain'])

    if not col_sitl or not col_domain:
        return [], method, "엑셀에 'SITL ID'와 'Domain' 컬럼이 필요해요."

    items = []
    for r in rows:
        sitl = _clean_sitl(r.get(col_sitl))
        if not sitl:
            continue
        title = ''
        if col_title:
            t = r.get(col_title)
            title = str(t).strip() if t is not None else ''

        # Domain 칸에 여러 도메인이 쉼표/슬래시로 들어있을 수 있음
        #   예) "Linux, Android"  또는  "Misc, Linux"  또는  "Misc."
        # → 각 도메인마다 SITL을 따로 등록 (그래야 도메인별 조회가 맞음)
        raw_domain = r.get(col_domain)
        raw_domain = str(raw_domain) if raw_domain is not None else ''
        for dom in re.split(r'[,/;]', raw_domain):
            dom = _clean_domain(dom)
            if not dom:
                continue
            items.append({
                'sitl_id': sitl,
                'title':   title,
                'domain':  dom,
            })
    return items, method, None


def _clean_domain(d):
    """도메인 문자열 정리: 소문자 + 앞뒤 점/공백 제거 ('Misc.' → 'misc')"""
    if d is None:
        return ''
    d = str(d).strip().lower()
    d = d.strip('. \t')          # 앞뒤 점·공백 제거
    d = re.sub(r'\s+', ' ', d)   # 중간 공백 정리 (baremetal linux 대비)
    return d.strip()


def _clean_sitl(v):
    """SITL 번호를 정수 문자열로 정리 (엑셀이 1024.0 처럼 실수로 주는 것 방지)"""
    if v is None:
        return ''
    # 숫자(실수 포함)면 정수로
    if isinstance(v, float):
        # 1024.0 → '1024', 소수부가 있으면(드묾) 반올림 아닌 버림
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    if not s:
        return ''
    # 문자열인데 숫자로 해석되면 정수화 ('1024', '1024.0', '1024.00' 모두 → '1024')
    try:
        f = float(s)
        return str(int(f))
    except ValueError:
        pass
    # 숫자가 아니면(예: 'SITL_1024') 그대로 둠
    return s


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

    # 드롭다운 목록: 관리자 '프로젝트 관리'(projects 테이블)의 프로젝트를 그대로 사용
    #   → 관리자에서 이름을 바꾸면 여기에도 자동 반영됨
    pdb = get_db()
    admin_projects = [r['name'] for r in pdb.execute(
        "SELECT name FROM projects ORDER BY name").fetchall()]
    pdb.close()

    # 관리자 목록 + (혹시 관리자에 없는데 이미 등록된) workitem 프로젝트도 합침
    registered = [r['project'] for r in rows]
    project_options = list(admin_projects)
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
        flash('프로젝트를 선택하거나 입력해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))
    if not file or not file.filename:
        flash('workitems 엑셀 파일을 선택해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))
    if not file.filename.lower().endswith(('.xlsx', '.xls', '.csv')):
        flash('엑셀(.xlsx/.xls) 또는 CSV 파일만 업로드할 수 있어요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    try:
        items, method, error_msg = parse_workitems_excel(file)
    except Exception as e:
        flash(f'엑셀을 읽는 중 오류가 났어요: {e}', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    if error_msg:
        flash(error_msg, 'error')
        return redirect(url_for('it_verification.workitems_view'))
    if not items:
        flash('SITL 데이터를 찾지 못했어요. 컬럼(SITL ID / Domain)을 확인해주세요.', 'error')
        return redirect(url_for('it_verification.workitems_view'))

    # 선택한 프로젝트로 저장 (해당 프로젝트 기존 것은 갱신)
    db = get_db()
    db.execute("DELETE FROM workitem WHERE project=?", (project,))
    for it in items:
        db.execute('''INSERT INTO workitem (project, board, sitl_id, title, domain)
                      VALUES (?,?,?,?,?)''',
                   (project, '', it['sitl_id'], it['title'], it['domain']))
    db.commit()
    db.close()

    tag = {'xlwings': '엑셀앱', 'openpyxl': '직접읽기', 'csv': 'CSV'}.get(method, method or '')
    flash(f'✅ {project} 등록 완료 · SITL {len(items)}건 ({tag})', 'success')
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
    # workitem에 등록된 프로젝트 목록
    projects = [r['project'] for r in db.execute(
        "SELECT DISTINCT project FROM workitem ORDER BY project").fetchall()]

    # 프로젝트별 보드 목록 (관리자 '프로젝트 관리'의 boards 재사용)
    #   프로젝트를 고르면 그 프로젝트의 보드가 보드 드롭다운에 채워지도록 매핑을 넘김
    #   {프로젝트명: [보드명, ...]}
    #   ※ workitems 프로젝트명과 관리자 프로젝트명의 대소문자가 달라도 매칭되도록
    #     COLLATE NOCASE 로 대소문자 무시 비교
    project_boards = {}
    for proj in projects:
        boards = db.execute('''SELECT b.name FROM boards b
                               JOIN projects p ON b.project_id = p.id
                               WHERE p.name = ? COLLATE NOCASE AND b.is_active = 1
                               ORDER BY b.name''', (proj,)).fetchall()
        project_boards[proj] = [b['name'] for b in boards]

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
                           projects=projects,
                           project_boards=project_boards,
                           domains=DOMAINS, logs=logs, dates=dates,
                           filter_date=filter_date,
                           sel_project=request.args.get('sel_project', ''),
                           sel_board=request.args.get('sel_board', ''),
                           sel_domain=request.args.get('sel_domain', ''),
                           today_str=datetime.date.today().strftime('%Y-%m-%d'))


@itverify_bp.route('/sitls')
@login_required
def get_sitls():
    """프로젝트+도메인으로 SITL 목록을 조회 (JSON) — 왼쪽 목록 채우기용.
       각 SITL이 오늘 저장됐는지(saved)와, 전체/저장 개수도 함께 반환."""
    project = request.args.get('project', '').strip()
    board   = request.args.get('board', '').strip()
    domain  = request.args.get('domain', '').strip().lower()
    if not project or not domain:
        return jsonify({'ok': False, 'sitls': [], 'total': 0, 'saved': 0})

    db = get_db()
    rows = db.execute('''SELECT sitl_id FROM workitem
                         WHERE project=? COLLATE NOCASE AND domain=?''',
                      (project, domain)).fetchall()

    # 오늘 이 조합으로 저장된 SITL 집합
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    saved_rows = db.execute('''SELECT DISTINCT sitl_id FROM verification_log
                               WHERE log_date=? AND project=? AND board=? AND domain=?''',
                            (date_str, project, board, domain)).fetchall()
    saved_set = {r['sitl_id'] for r in saved_rows}
    db.close()

    # SITL_2 < SITL_10 이 되도록 숫자 기준 정렬
    def sort_key(sid):
        m = re.search(r'(\d+)', sid)
        return (int(m.group(1)) if m else 0, sid)
    sids = sorted([r['sitl_id'] for r in rows], key=sort_key)

    sitls = [{'sitl_id': s, 'title': '', 'saved': (s in saved_set)} for s in sids]
    saved_count = sum(1 for s in sids if s in saved_set)
    return jsonify({'ok': True, 'sitls': sitls,
                    'total': len(sids), 'saved': saved_count})


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

    # 필수값 검증 (보드는 프로젝트에 보드가 등록돼 있을 때만 넘어옴)
    if not all([project, domain, sitl_id, result]):
        flash('프로젝트·도메인·SITL·결과를 모두 선택해주세요.', 'error')
        return redirect(url_for('it_verification.log_view'))
    if result not in RESULTS:
        flash('결과는 PASS/FAIL/NA 중 하나여야 해요.', 'error')
        return redirect(url_for('it_verification.log_view'))

    db = get_db()

    today = datetime.date.today()
    date_str     = today.strftime('%Y-%m-%d')   # 폴더용
    date_compact = today.strftime('%Y%m%d')      # 파일명용

    # 파일명/폴더에 쓸 조합 (보드가 있으면 프로젝트_보드_도메인, 없으면 프로젝트_도메인)
    if board:
        combo    = f"{_safe(project)}_{_safe(board)}_{_safe(domain)}"
        name_pre = f"{_safe(project)}_{_safe(board)}_{_safe(domain)}_{_safe(sitl_id)}"
    else:
        combo    = f"{_safe(project)}_{_safe(domain)}"
        name_pre = f"{_safe(project)}_{_safe(domain)}_{_safe(sitl_id)}"

    # 폴더:  logs/날짜/프로젝트_보드_도메인/
    folder = os.path.join(LOG_ROOT, date_str, combo)
    os.makedirs(folder, exist_ok=True)

    # 파일명: 프로젝트_보드_도메인_SITL_결과_날짜.txt
    filename = f"{name_pre}_{result}_{date_compact}.txt"
    full_path = os.path.join(folder, filename)

    # txt 내용 구성
    txt = (
        f"[프로젝트] {project}\n"
        f"[보드] {board}\n"
        f"[도메인] {domain}\n"
        f"[SITL] {sitl_id}\n"
        f"[결과] {result}\n"
        f"[작성일] {date_str}\n"
        f"{'-'*40}\n"
        f"{content}\n"
    )
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(txt)

    # DB 상대경로 기록 (logs/ 아래 경로)
    rel_path = os.path.relpath(full_path, LOG_ROOT)

    # 오늘 같은 조합(날짜+프로젝트+보드+도메인+SITL)의 로그가 있으면 → 덮어쓰기(수정)
    existing = db.execute('''SELECT id, file_path FROM verification_log
                             WHERE log_date=? AND project=? AND board=? AND domain=? AND sitl_id=?
                             ORDER BY id DESC LIMIT 1''',
                          (date_str, project, board, domain, sitl_id)).fetchone()
    if existing:
        # 결과가 바뀌면 파일명도 바뀜 → 옛 파일 삭제 (경로가 다를 때만)
        old_full = os.path.join(LOG_ROOT, existing['file_path'] or '')
        if existing['file_path'] and existing['file_path'] != rel_path and os.path.exists(old_full):
            try:
                os.remove(old_full)
            except OSError:
                pass
        db.execute('''UPDATE verification_log
                      SET result=?, content=?, file_path=?, created_by=?
                      WHERE id=?''',
                   (result, content, rel_path, session.get('user_id'), existing['id']))
        db.commit()
        db.close()
        flash(f'✏️ 수정 완료: {filename}', 'success')
    else:
        db.execute('''INSERT INTO verification_log
            (log_date, project, board, domain, sitl_id, title, result, content, file_path, created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (date_str, project, board, domain, sitl_id, '', result, content,
             rel_path, session.get('user_id')))
        db.commit()
        db.close()
        flash(f'💾 저장 완료: {filename}', 'success')

    # 저장 후에도 프로젝트·보드·도메인 선택이 유지되도록 쿼리로 넘김
    return redirect(url_for('it_verification.log_view', date=date_str,
                            sel_project=project, sel_board=board, sel_domain=domain))


@itverify_bp.route('/log/get')
@login_required
def log_get():
    """
    오늘 날짜 + 프로젝트·보드·도메인·SITL 조합으로 저장된 로그가 있으면
    내용과 결과를 돌려줌 (SITL 다시 클릭 시 수정 모드용)
    """
    project = request.args.get('project', '').strip()
    board   = request.args.get('board', '').strip()
    domain  = request.args.get('domain', '').strip().lower()
    sitl_id = request.args.get('sitl_id', '').strip()
    date_str = datetime.date.today().strftime('%Y-%m-%d')

    if not all([project, domain, sitl_id]):
        return jsonify({'found': False})

    db = get_db()
    log = db.execute('''SELECT id, result, content FROM verification_log
                        WHERE log_date=? AND project=? AND board=? AND domain=? AND sitl_id=?
                        ORDER BY id DESC LIMIT 1''',
                     (date_str, project, board, domain, sitl_id)).fetchone()
    db.close()
    if not log:
        return jsonify({'found': False})
    return jsonify({'found': True, 'result': log['result'], 'content': log['content'] or ''})


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


def _domain_label(domain):
    """폴더명용 도메인 표기: SFI만 전체 대문자, 나머지는 첫 글자만 대문자
       예) sfi→SFI, linux→Linux, baremetal linux→Baremetal linux"""
    d = (domain or '').strip()
    if not d:
        return ''
    if d.lower() == 'sfi':
        return 'SFI'
    return d[:1].upper() + d[1:].lower()


def _board_digits(board):
    """보드에서 숫자만 추출: V720 → 720, 720 → 720"""
    if not board:
        return ''
    m = re.findall(r'\d+', board)
    return ''.join(m)


@itverify_bp.route('/log/download_all')
@login_required
def log_download_all():
    """오늘 저장된 로그를 ZIP으로 묶어 다운로드.
       구조: IRYYYYMMDD/ 프로젝트(대문자)_보드(숫자)_도메인표기 / 로그.txt"""
    today = datetime.date.today()
    date_str = today.strftime('%Y-%m-%d')
    ir_name = 'IR' + today.strftime('%Y%m%d')     # 예: IR20260707

    db = get_db()
    logs = db.execute('''SELECT * FROM verification_log
                         WHERE log_date=? ORDER BY project, board, domain, sitl_id''',
                      (date_str,)).fetchall()
    db.close()

    if not logs:
        flash('오늘 저장된 로그가 없어요.', 'error')
        return redirect(url_for('it_verification.log_view'))

    # 메모리에 ZIP 생성
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for log in logs:
            # 폴더명: 프로젝트(대문자)_보드(숫자만)_도메인표기
            proj = (log['project'] or '').upper()
            board = _board_digits(log['board'])
            dom = _domain_label(log['domain'])
            parts = [p for p in [proj, board, dom] if p]
            folder = '_'.join(parts)

            # 실제 저장된 txt를 읽어 ZIP에 넣음 (없으면 DB 내용으로 생성)
            full = os.path.join(LOG_ROOT, log['file_path'] or '')
            fname = os.path.basename(log['file_path']) if log['file_path'] else \
                    f"{proj}_{board}_{dom}_{log['sitl_id']}_{log['result']}.txt"
            arcname = f"{ir_name}/{folder}/{fname}"

            if log['file_path'] and os.path.exists(full):
                zf.write(full, arcname)
            else:
                # 파일이 없으면 DB 내용으로 즉석 생성
                txt = (f"[프로젝트] {log['project']}\n[보드] {log['board']}\n"
                       f"[도메인] {log['domain']}\n[SITL] {log['sitl_id']}\n"
                       f"[결과] {log['result']}\n[작성일] {log['log_date']}\n"
                       f"{'-'*40}\n{log['content'] or ''}\n")
                zf.writestr(arcname, txt)

    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=f'{ir_name}.zip')


@itverify_bp.route('/log/delete_multi', methods=['POST'])
@login_required
def log_delete_multi():
    """체크박스로 고른 여러 로그를 한 번에 삭제 (서버 txt도 함께)"""
    ids = request.form.getlist('log_ids')
    if not ids:
        flash('삭제할 로그를 선택해주세요.', 'error')
        return redirect(url_for('it_verification.log_view'))

    db = get_db()
    deleted = 0
    for lid in ids:
        log = db.execute("SELECT file_path FROM verification_log WHERE id=?", (lid,)).fetchone()
        if not log:
            continue
        full = os.path.join(LOG_ROOT, log['file_path'] or '')
        try:
            if log['file_path'] and os.path.exists(full):
                os.remove(full)
        except OSError:
            pass
        db.execute("DELETE FROM verification_log WHERE id=?", (lid,))
        deleted += 1
    db.commit()
    db.close()
    flash(f'🗑️ {deleted}개 로그를 삭제했어요.', 'success')
    return redirect(url_for('it_verification.log_view'))


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
