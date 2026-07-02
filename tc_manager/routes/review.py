import os
import csv
import io
import tempfile
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, session)
from database import get_db
from functools import wraps

review_bp = Blueprint('review', __name__, url_prefix='/review')

ALLOWED_EXT = {'xlsx', 'xls', 'csv'}

# ──────────────────────────────────────────
# TC 상태 정의
# Approved 는 팀 내부 시스템에 없음
# → 우리 팀이 Reviewed 확인 후 Polarion ALM 에서 직접 변경
# → SWPM 이 ALM 에서 Approved 로 변경 → IT 대상
# ──────────────────────────────────────────
STATUS_LIST = ['Draft', 'In Review', 'BL in Review', 'Reviewed']
STATUS_STYLE = {
    'Draft':        ('badge-draft',      '⬜'),
    'In Review':    ('badge-inreview',   '🔵'),
    'BL in Review': ('badge-blinreview', '🟡'),
    'Reviewed':     ('badge-reviewed',   '🟢'),
}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('관리자만 접근할 수 있습니다.', 'error')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


# ──────────────────────────────────────────
#  엑셀 읽기 함수 (3단계 시도)
#
#  1순위: xlwings  → 실제 엑셀 앱을 통해 읽음 (DRM 우회 가능)
#  2순위: openpyxl → 파일 직접 읽기 (빠르지만 DRM에 막힐 수 있음)
#  3순위: CSV      → 별도 변환 필요, 파이썬 내장 모듈
# ──────────────────────────────────────────
def read_excel_file(file_storage):
    """
    file_storage: Flask request.files 객체
    반환: (rows, method, error_msg)
      rows      - list of dict (헤더:값, '__tc_url__' 키로 하이퍼링크 포함)
      method    - 'xlwings' | 'openpyxl' | 'csv' | None
      error_msg - 실패 시 오류 메시지
    """
    filename = file_storage.filename
    ext = filename.rsplit('.', 1)[1].lower()
    content = file_storage.read()

    # ── CSV ──────────────────────────────
    if ext == 'csv':
        try:
            text = content.decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(text))
            rows = [r for r in reader if any(v.strip() for v in r.values())]
            return rows, 'csv', None
        except Exception as e:
            return [], None, f'CSV 읽기 실패: {e}'

    # ── 엑셀 (xlsx / xls) ────────────────
    # 1순위: xlwings (실제 엑셀 앱 사용 → DRM 우회 가능)
    xw_error = None
    try:
        import xlwings as xw

        suffix = f'.{ext}'
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(tmp_fd, content)
            os.close(tmp_fd)
            tmp_fd = None

            app = xw.App(visible=False, add_book=False)
            try:
                wb = app.books.open(tmp_path)
                ws = wb.sheets[0]

                # 사용 범위의 마지막 행/열 확인
                last_row = ws.used_range.last_cell.row
                last_col = ws.used_range.last_cell.column

                # 헤더 (1행) — 고정 컬럼 수로 읽기 (expand 미사용)
                # expand('right')는 콤마 포함 셀에서 열이 밀리는 문제 있음
                header_range = ws.range(1, 1).resize(1, last_col).value
                if not isinstance(header_range, list):
                    header_range = [header_range]
                # 2D 리스트 처리
                if header_range and isinstance(header_range[0], list):
                    header_range = header_range[0]
                headers = [str(v).strip() if v is not None else ''
                           for v in header_range]

                # ID 컬럼 인덱스 찾기 (하이퍼링크 추출용)
                id_col_idx = None
                for idx, h in enumerate(headers):
                    if h.upper() == 'ID':
                        id_col_idx = idx + 1  # xlwings 1-based
                        break

                # 데이터 (2행~) — 고정 컬럼 수로 읽기
                rows = []
                for i in range(2, last_row + 1):
                    # resize로 고정 컬럼 수 읽기
                    row_vals = ws.range(i, 1).resize(1, last_col).value
                    if row_vals is None:
                        continue
                    # 2D 리스트 처리
                    if isinstance(row_vals, list) and row_vals and isinstance(row_vals[0], list):
                        row_vals = row_vals[0]
                    if not isinstance(row_vals, list):
                        row_vals = [row_vals]
                    # 컬럼 수 맞추기
                    row_vals = row_vals[:len(headers)]
                    row_vals += [None] * (len(headers) - len(row_vals))
                    if not any(v is not None for v in row_vals):
                        continue

                    row_dict = dict(zip(headers, row_vals))

                    # ID 셀 하이퍼링크 추출
                    tc_url = ''
                    if id_col_idx:
                        try:
                            cell = ws.range(i, id_col_idx)
                            hl   = cell.hyperlink
                            if hl and str(hl).startswith('http'):
                                tc_url = str(hl)
                        except Exception:
                            pass
                    row_dict['__tc_url__'] = tc_url
                    rows.append(row_dict)

                wb.close()
            finally:
                app.quit()

        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        return rows, 'xlwings', None

    except Exception as e_xw:
        xw_error = str(e_xw)

    # 2순위: openpyxl (파일 직접 읽기 + 하이퍼링크 추출)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        headers = [str(c.value).strip() if c.value is not None else ''
                   for c in ws[1]]

        # ID 컬럼 인덱스 찾기
        id_col_idx = None
        for idx, h in enumerate(headers):
            if h.upper() == 'ID':
                id_col_idx = idx  # 0-based
                break

        # 하이퍼링크 추출을 위해 data_only=False 로 한 번 더 열기
        wb_hl = openpyxl.load_workbook(io.BytesIO(content), data_only=False)
        ws_hl = wb_hl.active

        rows = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not any(v is not None for v in row):
                continue
            row_dict = dict(zip(headers, row))

            # ID 셀 하이퍼링크
            tc_url = ''
            if id_col_idx is not None:
                try:
                    cell_hl = ws_hl.cell(row=row_idx, column=id_col_idx + 1)
                    if cell_hl.hyperlink and str(cell_hl.hyperlink.target).startswith('http'):
                        tc_url = str(cell_hl.hyperlink.target)
                except Exception:
                    pass
            row_dict['__tc_url__'] = tc_url
            rows.append(row_dict)

        return rows, 'openpyxl', None

    except Exception as e_op:
        return [], None, (
            f'엑셀 읽기 실패.\n'
            f'• xlwings 오류: {xw_error}\n'
            f'• openpyxl 오류: {e_op}\n\n'
            f'💡 해결 방법: 엑셀에서 [다른 이름으로 저장] →\n'
            f'   파일 형식: CSV(쉼표로 분리)(*.csv) → 저장 후 업로드해주세요.'
        )


# ──────────────────────────────────────────
#  TC 목록
# ──────────────────────────────────────────
@review_bp.route('/')
@login_required
def list_tc():
    db = get_db()
    project_id      = request.args.get('project_id', type=int)
    status_filter   = request.args.get('status', '')
    domain_filter   = request.args.get('domain', '')
    alm_status_filter = request.args.get('alm_status', '')
    keyword         = request.args.get('q', '').strip()
    sort_col        = request.args.get('sort', 'tc_id')
    sort_dir        = request.args.get('dir', 'asc')

    ALLOWED_SORT = {
        'tc_id':          't.tc_id',
        'sub_system':     't.sub_system',
        'ip':             't.ip',
        'sitl_id':        't.sitl_id',
        'title':          't.title',
        'domain':         't.domain',
        'alm_status':     't.alm_status',
        'overall_status': 't.overall_status',
        'project_name':   'p.name',
    }
    sort_dir  = 'asc' if sort_dir != 'desc' else 'desc'
    order_by  = ALLOWED_SORT.get(sort_col, 't.tc_id')
    order_sql = f"{order_by} {sort_dir.upper()}"

    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()

    domain_list = db.execute(
        "SELECT DISTINCT domain FROM tc_review WHERE domain != '' ORDER BY domain"
    ).fetchall()
    domain_list = [r['domain'] for r in domain_list]

    # ALM TC 상태 목록 (실제 데이터에서 추출)
    alm_status_list = db.execute(
        "SELECT DISTINCT alm_status FROM tc_review "
        "WHERE alm_status IS NOT NULL AND alm_status != '' ORDER BY alm_status"
    ).fetchall()
    alm_status_list = [r['alm_status'] for r in alm_status_list]

    where  = ["1=1"]
    params = []
    if project_id:
        where.append("t.project_id=?"); params.append(project_id)
    if status_filter:
        where.append("t.overall_status=?"); params.append(status_filter)
    if domain_filter:
        where.append("t.domain=?"); params.append(domain_filter)
    if alm_status_filter:
        where.append("t.alm_status=?"); params.append(alm_status_filter)
    if keyword:
        where.append("""(
            t.tc_id      LIKE ? OR
            t.title      LIKE ? OR
            t.sub_system LIKE ? OR
            t.ip         LIKE ? OR
            t.sitl_id    LIKE ?
        )""")
        kw = f'%{keyword}%'
        params += [kw, kw, kw, kw, kw]

    tc_list = db.execute(f'''
        SELECT t.*, p.name as project_name
        FROM tc_review t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE {" AND ".join(where)}
        ORDER BY {order_sql}
    ''', params).fetchall()

    # TC별 보드 상태 목록
    tc_ids = [tc['id'] for tc in tc_list]
    board_status_map = {}
    if tc_ids:
        placeholders = ','.join('?' * len(tc_ids))
        board_rows = db.execute(f'''
            SELECT rs.tc_id, b.name as board_name, rs.status
            FROM review_status rs
            JOIN boards b ON rs.board_id = b.id
            WHERE rs.tc_id IN ({placeholders})
            ORDER BY b.name
        ''', tc_ids).fetchall()
        for row in board_rows:
            if row['tc_id'] not in board_status_map:
                board_status_map[row['tc_id']] = []
            board_status_map[row['tc_id']].append({
                'board': row['board_name'],
                'status': row['status']
            })

    db.close()
    return render_template('review/list.html',
                           tc_list=tc_list,
                           projects=projects,
                           domain_list=domain_list,
                           alm_status_list=alm_status_list,
                           board_status_map=board_status_map,
                           selected_project=project_id,
                           status_filter=status_filter,
                           domain_filter=domain_filter,
                           alm_status_filter=alm_status_filter,
                           keyword=keyword,
                           sort_col=sort_col,
                           sort_dir=sort_dir,
                           STATUS_LIST=STATUS_LIST,
                           STATUS_STYLE=STATUS_STYLE)


# ──────────────────────────────────────────
#  TC 상세 (보드별 리뷰 상태)
# ──────────────────────────────────────────
@review_bp.route('/tc/<int:tc_id>')
@login_required
def tc_detail(tc_id):
    db = get_db()
    tc = db.execute('''
        SELECT t.*, p.name as project_name
        FROM tc_review t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.id=?
    ''', (tc_id,)).fetchone()

    if not tc:
        flash('TC를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('review.list_tc'))

    boards = db.execute(
        "SELECT * FROM boards WHERE project_id=? AND is_active=1 ORDER BY name",
        (tc['project_id'],)
    ).fetchall()

    review_statuses = db.execute('''
        SELECT rs.*, b.name as board_name, u.name as reviewer_name
        FROM review_status rs
        JOIN boards b ON rs.board_id = b.id
        LEFT JOIN users u ON rs.reviewer_id = u.id
        WHERE rs.tc_id=?
        ORDER BY b.name
    ''', (tc_id,)).fetchall()

    status_map = {rs['board_id']: rs for rs in review_statuses}

    members = db.execute(
        "SELECT * FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()

    db.close()
    return render_template('review/detail.html',
                           tc=tc,
                           boards=boards,
                           status_map=status_map,
                           members=members,
                           STATUS_LIST=STATUS_LIST,
                           STATUS_STYLE=STATUS_STYLE)


# ──────────────────────────────────────────
#  보드별 상태 변경
# ──────────────────────────────────────────
@review_bp.route('/tc/<int:tc_id>/update_status', methods=['POST'])
@login_required
def update_board_status(tc_id):
    board_id    = request.form.get('board_id', type=int)
    new_status  = request.form.get('status', '').strip()
    comment     = request.form.get('comment', '').strip()
    reviewer_id = request.form.get('reviewer_id', type=int) or session['user_id']

    if not board_id or new_status not in STATUS_LIST:
        flash('잘못된 요청입니다.', 'error')
        return redirect(url_for('review.tc_detail', tc_id=tc_id))

    db = get_db()

    existing = db.execute(
        "SELECT id FROM review_status WHERE tc_id=? AND board_id=?",
        (tc_id, board_id)
    ).fetchone()

    if existing:
        db.execute('''
            UPDATE review_status
            SET status=?, comment=?, reviewer_id=?,
                updated_at=datetime('now','localtime')
            WHERE tc_id=? AND board_id=?
        ''', (new_status, comment, reviewer_id, tc_id, board_id))
    else:
        db.execute('''
            INSERT INTO review_status (tc_id, board_id, reviewer_id, status, comment)
            VALUES (?, ?, ?, ?, ?)
        ''', (tc_id, board_id, reviewer_id, new_status, comment))

    # ── 전체 상태 자동 계산 ──
    # TC에 V920 BL 보드가 있는지 확인 (Domain에 baremetal linux 포함 여부)
    tc_row = db.execute(
        "SELECT project_id, domain FROM tc_review WHERE id=?", (tc_id,)
    ).fetchone()

    has_bl = 'baremetal linux' in (tc_row['domain'] or '').lower()

    # 이 TC에 실제 등록된 review_status 보드 목록
    all_board_statuses = db.execute('''
        SELECT rs.status, b.name as board_name
        FROM review_status rs
        JOIN boards b ON rs.board_id = b.id
        WHERE rs.tc_id=?
    ''', (tc_id,)).fetchall()

    vals       = [r['status']     for r in all_board_statuses]
    board_names = [r['board_name'] for r in all_board_statuses]

    # 필수 보드 구성 정의
    # V920 BL 있는 TC: V720, V820, V920, V920 BL 4개 모두 Reviewed
    # V920 BL 없는 TC: V720, V820, V920 3개 모두 Reviewed
    REQUIRED_BOARDS    = ['V720', 'V820', 'V920']
    REQUIRED_BOARDS_BL = ['V720', 'V820', 'V920', 'V920 BL']
    required = REQUIRED_BOARDS_BL if has_bl else REQUIRED_BOARDS

    # 필수 보드가 모두 Reviewed인지 확인
    status_by_board = {r['board_name']: r['status'] for r in all_board_statuses}
    all_required_reviewed = all(
        status_by_board.get(b) == 'Reviewed' for b in required
    )

    if all_required_reviewed and len(vals) > 0:
        overall = 'Reviewed'
    elif 'In Review' in vals:
        overall = 'In Review'
    elif 'BL in Review' in vals:
        overall = 'BL in Review'
    else:
        overall = 'Draft'

    db.execute("UPDATE tc_review SET overall_status=? WHERE id=?", (overall, tc_id))
    db.commit()
    db.close()

    flash(f'✅ [{new_status}] 로 변경되었습니다.', 'success')
    return redirect(url_for('review.tc_detail', tc_id=tc_id))


# ──────────────────────────────────────────
#  TC 업로드 (관리자)
# ──────────────────────────────────────────
@review_bp.route('/upload', methods=['GET', 'POST'])
@login_required
@admin_required
def upload_tc():
    db = get_db()
    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()

    if request.method == 'POST':
        project_id   = request.form.get('project_id', type=int)
        upload_mode  = request.form.get('upload_mode', 'replace')
        file         = request.files.get('file')

        if not project_id:
            flash('프로젝트를 선택해주세요.', 'error')
            db.close()
            return render_template('review/upload.html', projects=projects)

        # 선택한 프로젝트 이름 가져오기 (target_project 저장용)
        selected_project = db.execute(
            "SELECT name FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        selected_project_name = selected_project['name'] if selected_project else ''

        if not file or file.filename == '':
            flash('파일을 선택해주세요.', 'error')
            db.close()
            return render_template('review/upload.html', projects=projects)

        if not allowed_file(file.filename):
            flash('xlsx, xls, csv 파일만 업로드 가능합니다.', 'error')
            db.close()
            return render_template('review/upload.html', projects=projects)

        rows, method, error_msg = read_excel_file(file)

        if error_msg:
            flash(f'❌ {error_msg}', 'error')
            db.close()
            return render_template('review/upload.html', projects=projects)

        if not rows:
            flash('파일에 데이터가 없습니다.', 'error')
            db.close()
            return render_template('review/upload.html', projects=projects)

        # 컬럼 매핑 — 헤더명이 달라도 자동 인식 (대소문자 무관, 순서 무관)
        COL_MAP = {
            'target_project':      ['Target Project', 'target_project', 'TargetProject'],
            'id':                  ['ID', 'id'],
            'sitl_id':             ['SITL-ID', 'SITL ID', 'SITL_ID',
                                    'SITl ID (CI TC)',   # 실제 엑셀 헤더 (소문자 l)
                                    'SITL ID (CI TC)',
                                    'STIL ID (CI TC)',
                                    'sitlid', 'sitl_id', 'sitl-id'],
            'sub_system':          ['Sub System', 'SubSystem', 'sub_system', 'sub system'],
            'ip':                  ['IP', 'ip'],
            'domain':              ['Domain', 'domain'],
            'title':               ['Title', 'title'],
            'alm_status':          ['Status', 'status', 'ALM Status'],
            'ci_script_file_name': ['CI Test Script File Name',
                                    'CI_Test_Script_File_Name',
                                    'CI Script File Name',
                                    'CITestScriptFileName'],
            'ci_test_automated':   ['CI Test Automated', 'CI_Test_Automated'],
        }

        def find_col(row_dict, candidates):
            # 1순위: 대소문자 무관 정확 일치
            for k in row_dict:
                if k and k.strip():
                    for c in candidates:
                        if k.strip().lower() == c.lower():
                            return row_dict[k]
            # 2순위: 영문/숫자만 추출 후 비교 (공백·특수문자 제거)
            # 예) 'SITl ID (CI TC)' → 'sitlidcitc'
            for k in row_dict:
                if k and k.strip():
                    k_clean = ''.join(ch for ch in k if ch.isalnum()).lower()
                    for c in candidates:
                        c_clean = ''.join(ch for ch in c if ch.isalnum()).lower()
                        if k_clean and c_clean and k_clean == c_clean:
                            return row_dict[k]
            return None

        if upload_mode == 'replace':
            db.execute(
                "DELETE FROM review_status WHERE tc_id IN "
                "(SELECT id FROM tc_review WHERE project_id=?)", (project_id,)
            )
            db.execute("DELETE FROM tc_review WHERE project_id=?", (project_id,))

        def s(v):
            """None → 빈 문자열, 16.0 같은 실수 → '16' 정수형 문자열로 변환"""
            if v is None:
                return ''
            if isinstance(v, float) and v == int(v):
                return str(int(v))
            return str(v).strip()

        def to_int(v):
            """sitl_id 전용: 정수 변환. 숫자가 아니면 None 반환"""
            if v is None:
                return None
            try:
                if isinstance(v, float):
                    return int(v)
                return int(str(v).strip())
            except (ValueError, TypeError):
                return None

        # ── Domain 자동 보드 매핑 규칙 ──────────────────────────
        DOMAIN_BOARD_RULES = [
            ('baremetal linux', 'V920 BL'),
        ]

        def get_or_create_board(proj_id, board_name):
            """보드가 없으면 자동 생성 후 ID 반환. 실패 시 None 반환"""
            # 먼저 기존 보드 확인
            row = db.execute(
                "SELECT id FROM boards WHERE project_id=? AND name=?",
                (proj_id, board_name)
            ).fetchone()
            if row:
                return row['id']
            # project_id 유효성 확인
            proj = db.execute(
                "SELECT id FROM projects WHERE id=?", (proj_id,)
            ).fetchone()
            if not proj:
                return None  # 프로젝트가 없으면 보드 생성 불가
            try:
                db.execute(
                    "INSERT INTO boards (project_id, name, is_active) VALUES (?, ?, 1)",
                    (proj_id, board_name)
                )
                result = db.execute(
                    "SELECT id FROM boards WHERE project_id=? AND name=?",
                    (proj_id, board_name)
                ).fetchone()
                return result['id'] if result else None
            except Exception:
                return None

        def apply_domain_board_rules(tc_db_id, proj_id, domain_str):
            """Domain 규칙에 따라 review_status 자동 생성, 생성 수 반환"""
            count = 0
            for keyword, board_name in DOMAIN_BOARD_RULES:
                if keyword in domain_str.lower():
                    board_id = get_or_create_board(proj_id, board_name)
                    if not board_id:
                        continue  # 보드 생성 실패 시 건너뜀
                    exists = db.execute(
                        "SELECT id FROM review_status WHERE tc_id=? AND board_id=?",
                        (tc_db_id, board_id)
                    ).fetchone()
                    if not exists:
                        try:
                            db.execute(
                                "INSERT INTO review_status (tc_id, board_id, status) VALUES (?, ?, 'Draft')",
                                (tc_db_id, board_id)
                            )
                            count += 1
                        except Exception:
                            pass
            return count
        # ─────────────────────────────────────────────────────────

        inserted   = 0   # 새로 추가된 TC
        updated    = 0   # 필드 업데이트된 TC
        bl_auto_count = 0

        for r in rows:
            tc_id_val      = find_col(r, COL_MAP['id'])
            title          = find_col(r, COL_MAP['title'])
            target_project = find_col(r, COL_MAP['target_project'])
            sub_system     = find_col(r, COL_MAP['sub_system'])
            ip             = find_col(r, COL_MAP['ip'])
            domain         = find_col(r, COL_MAP['domain'])
            sitl_id        = find_col(r, COL_MAP['sitl_id'])
            alm_status     = find_col(r, COL_MAP['alm_status'])
            ci_script      = find_col(r, COL_MAP['ci_script_file_name'])
            ci_automated   = find_col(r, COL_MAP['ci_test_automated'])
            tc_url         = r.get('__tc_url__', '')

            if not tc_id_val and not title:
                continue

            domain_str   = s(domain)
            tc_id_str    = s(tc_id_val)

            if upload_mode == 'append':
                # ── 추가 모드: TC ID 기준으로 기존 여부 확인 ──
                existing = db.execute(
                    "SELECT id FROM tc_review WHERE project_id=? AND tc_id=?",
                    (project_id, tc_id_str)
                ).fetchone()

                if existing:
                    # 기존 TC → 메타 필드만 업데이트 (진행상태/보드별 상태는 유지)
                    # target_project: 엑셀 원본값 그대로 저장 (공유 프로젝트 정보용)
                    db.execute('''
                        UPDATE tc_review SET
                            target_project      = ?,
                            sub_system          = ?,
                            ip                  = ?,
                            sitl_id             = ?,
                            title               = ?,
                            domain              = ?,
                            alm_status          = ?,
                            ci_script_file_name = ?,
                            ci_test_automated   = ?,
                            tc_url              = ?
                        WHERE project_id=? AND tc_id=?
                    ''', (s(target_project), s(sub_system), s(ip),
                          to_int(sitl_id), s(title), domain_str, s(alm_status),
                          s(ci_script), s(ci_automated), s(tc_url),
                          project_id, tc_id_str))
                    tc_db_id = existing['id']
                    updated += 1
                    bl_auto_count += apply_domain_board_rules(tc_db_id, project_id, domain_str)

                else:
                    # 새 TC → 신규 추가
                    # project_id: 선택한 프로젝트 (통계/필터 기준)
                    # target_project: 엑셀 원본값 (공유 프로젝트 정보용)
                    db.execute('''
                        INSERT INTO tc_review
                            (project_id, target_project, tc_id, sub_system, ip,
                             title, domain, sitl_id, alm_status,
                             ci_script_file_name, ci_test_automated, tc_url, overall_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft')
                    ''', (project_id, s(target_project), tc_id_str, s(sub_system),
                          s(ip), s(title), domain_str, to_int(sitl_id), s(alm_status),
                          s(ci_script), s(ci_automated), s(tc_url)))
                    tc_db_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    inserted += 1
                    bl_auto_count += apply_domain_board_rules(tc_db_id, project_id, domain_str)

            else:
                # ── 전체교체 모드: 무조건 INSERT ──
                # project_id: 선택한 프로젝트 (통계/필터 기준)
                # target_project: 엑셀 원본값 (공유 프로젝트 정보용)
                db.execute('''
                    INSERT INTO tc_review
                        (project_id, target_project, tc_id, sub_system, ip,
                         title, domain, sitl_id, alm_status,
                         ci_script_file_name, ci_test_automated, tc_url, overall_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft')
                ''', (project_id, s(target_project), tc_id_str, s(sub_system),
                      s(ip), s(title), domain_str, to_int(sitl_id), s(alm_status),
                      s(ci_script), s(ci_automated), s(tc_url)))
                tc_db_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                inserted += 1
                bl_auto_count += apply_domain_board_rules(tc_db_id, project_id, domain_str)

        db.commit()
        db.close()

        method_label = {'xlwings': '엑셀 앱(xlwings)', 'openpyxl': '직접읽기(openpyxl)',
                        'csv': 'CSV'}.get(method, method)
        if upload_mode == 'append':
            msg = f'✅ 추가: {inserted}개 / 업데이트: {updated}개 (읽기 방식: {method_label})'
        else:
            msg = f'✅ {inserted}개 TC 업로드 완료 (읽기 방식: {method_label})'
        if bl_auto_count:
            msg += f' / V920 BL 자동 생성: {bl_auto_count}개'
        flash(msg, 'success')
        return redirect(url_for('review.list_tc', project_id=project_id))

    db.close()
    return render_template('review/upload.html', projects=projects)


# ──────────────────────────────────────────
#  통합 현황
# ──────────────────────────────────────────
@review_bp.route('/summary')
@login_required
def summary():
    db = get_db()
    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()

    summary_data = []
    for p in projects:
        boards  = db.execute(
            "SELECT * FROM boards WHERE project_id=? AND is_active=1 ORDER BY name",
            (p['id'],)
        ).fetchall()
        members = db.execute(
            "SELECT * FROM users WHERE is_active=1 ORDER BY name"
        ).fetchall()

        member_stats = []
        for m in members:
            board_stats = []
            for b in boards:
                stats = db.execute('''
                    SELECT
                        SUM(CASE WHEN rs.status='In Review'    THEN 1 ELSE 0 END) as in_review,
                        SUM(CASE WHEN rs.status='BL in Review' THEN 1 ELSE 0 END) as bl_in_review,
                        SUM(CASE WHEN rs.status='Reviewed'     THEN 1 ELSE 0 END) as reviewed,
                        COUNT(rs.id) as total
                    FROM tc_review t
                    LEFT JOIN review_status rs ON rs.tc_id=t.id
                        AND rs.board_id=? AND rs.reviewer_id=?
                    WHERE t.project_id=?
                ''', (b['id'], m['id'], p['id'])).fetchone()
                board_stats.append({'board': b, 'stats': stats})
            member_stats.append({'member': m, 'board_stats': board_stats})

        total_tc    = db.execute(
            "SELECT COUNT(*) FROM tc_review WHERE project_id=?", (p['id'],)
        ).fetchone()[0]
        reviewed_tc = db.execute(
            "SELECT COUNT(*) FROM tc_review WHERE project_id=? AND overall_status='Reviewed'",
            (p['id'],)
        ).fetchone()[0]

        summary_data.append({
            'project': p, 'boards': boards,
            'member_stats': member_stats,
            'total_tc': total_tc, 'reviewed_tc': reviewed_tc,
        })

    db.close()
    return render_template('review/summary.html',
                           summary_data=summary_data,
                           STATUS_STYLE=STATUS_STYLE)


# ──────────────────────────────────────────
#  리뷰 상태 엑셀 업로드 (기존 엑셀 → DB 적용)
# ──────────────────────────────────────────

# 엑셀 컬럼명 → 보드명 매핑
BOARD_COL_MAP = {
    'V720':    ['V720리뷰어',   'V720리뷰상태',   'V720 리뷰어',   'V720 리뷰상태'],
    'V820':    ['V820리뷰어',   'V820리뷰상태',   'V820 리뷰어',   'V820 리뷰상태'],
    'V920':    ['V920리뷰어',   'V920리뷰상태',   'V920 리뷰어',   'V920 리뷰상태'],
    'V920 BL': ['V920BL리뷰어', 'V920BL리뷰상태', 'V920BL 리뷰어', 'V920BL 리뷰상태',
                'V920 BL리뷰어','V920 BL리뷰상태','V920 BL 리뷰어','V920 BL 리뷰상태'],
}

# 상태값 정규화 (엑셀에 다양하게 표기될 수 있음)
STATUS_NORMALIZE = {
    'draft':        'Draft',
    '':             'Draft',
    'in review':    'In Review',
    'inreview':     'In Review',
    'in_review':    'In Review',
    'bl in review': 'BL in Review',
    'blinreview':   'BL in Review',
    'bl_in_review': 'BL in Review',
    'reviewed':     'Reviewed',
}

def normalize_status(val):
    if val is None:
        return 'Draft'
    return STATUS_NORMALIZE.get(str(val).strip().lower(), str(val).strip())


def find_col_val(row_dict, candidates):
    """대소문자 무관 컬럼 찾기"""
    for k in row_dict:
        if k is None:
            continue
        for c in candidates:
            if k.strip().lower() == c.lower():
                return row_dict[k]
    return None


@review_bp.route('/upload_review_status', methods=['GET', 'POST'])
@login_required
@admin_required
def upload_review_status():
    db = get_db()
    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()

    if request.method == 'POST':
        project_id = request.form.get('project_id', type=int)
        file       = request.files.get('file')

        if not project_id:
            flash('프로젝트를 선택해주세요.', 'error')
            db.close()
            return render_template('review/upload_review_status.html', projects=projects)

        if not file or file.filename == '':
            flash('파일을 선택해주세요.', 'error')
            db.close()
            return render_template('review/upload_review_status.html', projects=projects)

        ext = file.filename.rsplit('.', 1)[-1].lower()
        if ext not in ('xlsx', 'xls', 'csv'):
            flash('xlsx, xls, csv 파일만 가능합니다.', 'error')
            db.close()
            return render_template('review/upload_review_status.html', projects=projects)

        # 파일 읽기 (review.py 안의 read_excel_file 재사용)
        rows, method, error_msg = read_excel_file(file)
        if error_msg:
            flash(f'❌ {error_msg}', 'error')
            db.close()
            return render_template('review/upload_review_status.html', projects=projects)

        if not rows:
            flash('파일에 데이터가 없습니다.', 'error')
            db.close()
            return render_template('review/upload_review_status.html', projects=projects)

        # 해당 프로젝트의 보드 목록
        boards = db.execute(
            "SELECT * FROM boards WHERE project_id=? AND is_active=1",
            (project_id,)
        ).fetchall()
        board_by_name = {b['name'].upper(): b for b in boards}

        # 팀원 목록 (이름으로 매핑)
        members = db.execute("SELECT * FROM users WHERE is_active=1").fetchall()
        member_by_name = {m['name'].strip(): m for m in members}

        updated = 0   # 업데이트된 review_status 수
        skipped = 0   # TC ID 없어서 건너뜀
        no_board = 0  # 보드 없어서 건너뜀

        def s(v):
            if v is None: return ''
            if isinstance(v, float) and v == int(v): return str(int(v))
            return str(v).strip()

        for r in rows:
            # TC ID 찾기
            tc_id_val = find_col_val(r, ['ID', 'TC ID', 'TCID', 'tc_id'])
            if not tc_id_val:
                skipped += 1
                continue
            tc_id_str = s(tc_id_val)

            # DB에서 TC 찾기 (해당 프로젝트 기준)
            tc_row = db.execute(
                "SELECT id, domain FROM tc_review WHERE project_id=? AND tc_id=?",
                (project_id, tc_id_str)
            ).fetchone()

            if not tc_row:
                skipped += 1
                continue

            tc_db_id  = tc_row['id']
            domain_str = (tc_row['domain'] or '').lower()
            has_bl     = 'baremetal linux' in domain_str

            # 보드별 리뷰어/상태 적용
            for board_name, col_candidates in BOARD_COL_MAP.items():
                # V920 BL은 BL TC에만 적용
                if board_name == 'V920 BL' and not has_bl:
                    continue

                # 리뷰어 컬럼 / 상태 컬럼 후보
                reviewer_cols = [c for c in col_candidates if '리뷰어' in c]
                status_cols   = [c for c in col_candidates if '상태' in c]

                reviewer_val = find_col_val(r, reviewer_cols)
                status_val   = find_col_val(r, status_cols)

                if not reviewer_val and not status_val:
                    continue

                # 상태 정규화
                new_status = normalize_status(status_val)
                if new_status not in STATUS_LIST:
                    new_status = 'Draft'

                # 보드 찾기 (대소문자 무관)
                board = board_by_name.get(board_name.upper())
                if not board:
                    # 자동 생성 — project_id 유효성 먼저 확인
                    proj_ok = db.execute(
                        "SELECT id FROM projects WHERE id=?", (project_id,)
                    ).fetchone()
                    if not proj_ok:
                        no_board += 1
                        continue
                    try:
                        db.execute(
                            "INSERT OR IGNORE INTO boards (project_id, name, is_active) VALUES (?,?,1)",
                            (project_id, board_name)
                        )
                    except Exception:
                        no_board += 1
                        continue
                    board = db.execute(
                        "SELECT * FROM boards WHERE project_id=? AND name=?",
                        (project_id, board_name)
                    ).fetchone()
                    if board:
                        board_by_name[board_name.upper()] = board
                    else:
                        no_board += 1
                        continue

                # 리뷰어 찾기
                reviewer_id = None
                if reviewer_val:
                    reviewer_name = s(reviewer_val)
                    member = member_by_name.get(reviewer_name)
                    if member:
                        reviewer_id = member['id']

                # review_status upsert
                existing = db.execute(
                    "SELECT id, reviewer_id FROM review_status WHERE tc_id=? AND board_id=?",
                    (tc_db_id, board['id'])
                ).fetchone()

                if existing:
                    keep_reviewer = reviewer_id if reviewer_id else existing['reviewer_id']
                    db.execute('''
                        UPDATE review_status
                        SET status=?, reviewer_id=?,
                            updated_at=datetime('now','localtime')
                        WHERE tc_id=? AND board_id=?
                    ''', (new_status, keep_reviewer, tc_db_id, board['id']))
                else:
                    try:
                        db.execute('''
                            INSERT INTO review_status (tc_id, board_id, reviewer_id, status)
                            VALUES (?, ?, ?, ?)
                        ''', (tc_db_id, board['id'], reviewer_id, new_status))
                    except Exception:
                        # FK 오류 등 → 건너뜀
                        continue

                updated += 1

            # overall_status 재계산
            all_statuses = db.execute(
                "SELECT rs.status, b.name as board_name "
                "FROM review_status rs JOIN boards b ON rs.board_id=b.id "
                "WHERE rs.tc_id=?", (tc_db_id,)
            ).fetchall()

            vals = [r2['status'] for r2 in all_statuses]
            status_by_board = {r2['board_name']: r2['status'] for r2 in all_statuses}

            REQUIRED_BOARDS    = ['V720', 'V820', 'V920']
            REQUIRED_BOARDS_BL = ['V720', 'V820', 'V920', 'V920 BL']
            required = REQUIRED_BOARDS_BL if has_bl else REQUIRED_BOARDS

            all_required_reviewed = all(
                status_by_board.get(b) == 'Reviewed' for b in required
            )

            if all_required_reviewed and vals:
                overall = 'Reviewed'
            elif 'In Review' in vals:
                overall = 'In Review'
            elif 'BL in Review' in vals:
                overall = 'BL in Review'
            else:
                overall = 'Draft'

            db.execute(
                "UPDATE tc_review SET overall_status=? WHERE id=?",
                (overall, tc_db_id)
            )

        db.commit()
        db.close()

        msg = f'✅ 리뷰 상태 적용 완료 — 업데이트: {updated}건'
        if skipped:  msg += f' / TC ID 없음: {skipped}건 (건너뜀)'
        if no_board: msg += f' / 보드 없음: {no_board}건'
        flash(msg, 'success')
        return redirect(url_for('review.list_tc', project_id=project_id))

    db.close()
    return render_template('review/upload_review_status.html', projects=projects)


# ──────────────────────────────────────────
#  엑셀 디버그 — SITL-ID 문제 추적용
# ──────────────────────────────────────────
@review_bp.route('/debug_excel', methods=['GET', 'POST'])
@login_required
@admin_required
def debug_excel():
    result = None
    if request.method == 'POST':
        file      = request.files.get('file')
        row_start = request.form.get('row_start', 1, type=int)
        row_end   = request.form.get('row_end', 30, type=int)

        if not file or file.filename == '':
            flash('파일을 선택해주세요.', 'error')
            return render_template('review/debug_excel.html', result=None)

        rows, method, error_msg = read_excel_file(file)
        if error_msg:
            flash(f'❌ {error_msg}', 'error')
            return render_template('review/debug_excel.html', result=None)
        if not rows:
            flash('데이터가 없습니다.', 'error')
            return render_template('review/debug_excel.html', result=None)

        headers = list(rows[0].keys())
        headers_repr = [repr(h) for h in headers]

        # SITL 관련 헤더 탐색
        sitl_headers = [h for h in headers
                        if h and ('sitl' in h.lower() or 'stil' in h.lower())]

        # 매칭 테스트
        sitl_candidates = ['SITL-ID', 'SITL ID', 'SITL_ID',
                           'SITl ID (CI TC)', 'SITL ID (CI TC)',
                           'STIL ID (CI TC)', 'sitlid', 'sitl_id']
        match_results = []
        for h in headers:
            if h is None: continue
            # 1순위 정확 매칭
            for c in sitl_candidates:
                if h.strip().lower() == c.lower():
                    match_results.append({'header': h, 'matched': c, 'method': '정확'})
                    break
            else:
                # 2순위 퍼지 매칭
                h_clean = ''.join(ch for ch in h if ch.isalnum()).lower()
                for c in sitl_candidates:
                    c_clean = ''.join(ch for ch in c if ch.isalnum()).lower()
                    if h_clean and c_clean and h_clean == c_clean:
                        match_results.append({'header': h, 'matched': c,
                                              'method': f'퍼지({h_clean})'})
                        break

        # 지정 범위 행 분석
        start_idx = max(0, row_start - 2)
        end_idx   = min(len(rows), row_end - 1)

        sample_rows = []
        for i, r in enumerate(rows[start_idx:end_idx]):
            # TC ID
            tc_id = None
            for k in ['ID', 'id']:
                if k in r and r[k] is not None:
                    tc_id = r[k]; break

            # Target Project
            target_proj = None
            for k in r:
                if k and 'target' in k.lower() and 'project' in k.lower():
                    target_proj = r[k]; break

            # SITL-ID — 실제 find_col 로직과 동일
            found_val = None
            matched_key = None
            matched_method = None
            for k in r:
                if k and k.strip():
                    for c in sitl_candidates:
                        if k.strip().lower() == c.lower():
                            found_val = r[k]; matched_key = k; matched_method = '정확'; break
                if matched_key: break
            if not matched_key:
                for k in r:
                    if k and k.strip():
                        k_clean = ''.join(ch for ch in k if ch.isalnum()).lower()
                        for c in sitl_candidates:
                            c_clean = ''.join(ch for ch in c if ch.isalnum()).lower()
                            if k_clean and c_clean and k_clean == c_clean:
                                found_val = r[k]; matched_key = k
                                matched_method = f'퍼지'; break
                        if matched_key: break

            # 원본 SITL 컬럼 값들
            sitl_vals = {h: r.get(h) for h in sitl_headers}
            col_count = len([v for v in r if not str(v).startswith('__')])

            sample_rows.append({
                'row':           start_idx + i + 2,
                'tc_id':         tc_id,
                'target_proj':   target_proj,
                'sitl_vals':     sitl_vals,
                'found_val':     found_val,
                'matched_key':   matched_key,
                'matched_method':matched_method,
                'col_count':     col_count,
                'header_count':  len(headers),
            })

        result = {
            'method':        method,
            'total_rows':    len(rows),
            'headers':       headers,
            'headers_repr':  headers_repr,
            'sitl_headers':  sitl_headers,
            'match_results': match_results,
            'sample_rows':   sample_rows,
            'row_start':     row_start,
            'row_end':       row_end,
        }

    return render_template('review/debug_excel.html', result=result)
