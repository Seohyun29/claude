"""
================================================================================
  일정 캘린더 모듈 (schedule.py)
================================================================================

[이 모듈이 하는 일]
  IT 팀의 월간 일정을 한 화면에서 관리하는 Flask 블루프린트입니다.
  세 가지 종류의 일정을 캘린더(막대 형태) + 리스트로 보여주고,
  등록 / 수정 / 삭제 / 일괄삭제를 지원합니다.

    1) IT 일정  : 프로젝트·장비(보드) 테스트 일정. 매주/격주 반복 가능. (파란색)
    2) 휴가     : 연차(여러 날 가능) / 반차 / 반반차. 반차류는 시간 입력. (노란색)
    3) 기타 일정: 회의·세미나 등. 여러 날·하루종일·시간 지정 가능.      (초록색)

[전체 구조 - 회사 동료가 빠르게 파악하려면 이 순서로 읽으세요]
  · init_schedule_db()  : 앱 시작 시 테이블 생성/마이그레이션 (기존 DB 보존)
  · calendar_view()     : 메인 화면. 일정을 모아 '막대(bar)'로 배치해 화면에 전달
  · add_* / edit_* / delete_* / bulk_delete : 등록·수정·삭제 처리
  · get_item()          : 수정 폼을 채우기 위한 단건 조회 (JSON 반환)

[DB 테이블]
  · it_schedule : IT 일정  (기존 테이블에 담당자/반복 컬럼을 자동 추가)
  · vacation    : 휴가      (종류 leave_type, 시간 start_time/end_time)
  · etc_event   : 기타 일정 (start~end 기간, all_day, 시간)

[기존 앱(tc_manager)에 붙이는 방법]  ※ app.py 안 create_app() 에 3줄 추가
    from routes.schedule import schedule_bp, init_schedule_db
    init_schedule_db()                  # init_db() 호출 바로 아래
    app.register_blueprint(schedule_bp) # 다른 블루프린트 등록 옆에
  그리고 base.html 사이드바에 링크 한 줄:
    <a href="{{ url_for('schedule.calendar_view') }}">📅 일정 캘린더</a>

[접속 주소]  http://[서버IP]:5000/schedule/
================================================================================
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from database import get_db          # tc_manager의 공용 DB 연결 함수 (SQLite)
from functools import wraps
import datetime
import re

# 이 모듈의 모든 URL은 '/schedule' 로 시작합니다. (예: /schedule/it/add)
schedule_bp = Blueprint('schedule', __name__, url_prefix='/schedule')

# 시간 형식 검증용 (HH:MM, 00:00~23:59)
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def valid_time(t):
    """'HH:MM' 형식이고 00:00~23:59 범위면 True"""
    return bool(t and TIME_RE.match(t.strip()))


# 휴가 종류 라벨 (코드 → 표시명 / 약칭)
LEAVE_TYPES = [
    ('full',    '연차(종일)', '연차', False),  # (코드, 풀네임, 약칭, 시간입력여부)
    ('half',    '반차',       '반차', True),
    ('quarter', '반반차',     '반반', True),
]
LEAVE_FULL  = {code: full  for code, full, short, has_time in LEAVE_TYPES}   # 코드→풀네임
LEAVE_SHORT = {code: short for code, full, short, has_time in LEAVE_TYPES}   # 코드→약칭
LEAVE_TIME  = {code: has_time for code, full, short, has_time in LEAVE_TYPES}  # 코드→시간입력여부


def fmt_time_short(t):
    """'09:00' → '09', '09:30' → '09:30' (정시는 분 생략)"""
    if not t:
        return ''
    t = t.strip()
    if t.endswith(':00'):
        return t[:2]
    return t


def vacation_chip_label(leave_type, short, start_time, end_time):
    """캘린더 칩에 표시할 라벨: 반차류는 '09-13', 연차는 '연차'"""
    if leave_type != 'full' and start_time and end_time:
        return f"{fmt_time_short(start_time)}-{fmt_time_short(end_time)}"
    return short


# ──────────────────────────────────────────
#  공통 데코레이터
# ──────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────
#  DB 초기화 (app.py의 init_db에서 호출)
# ──────────────────────────────────────────
def init_schedule_db():
    """
    [앱 시작 시 1회 실행] 이 모듈이 쓰는 테이블을 준비한다.

    중요: 기존 데이터를 절대 지우지 않는다. (안전한 마이그레이션)
      - 이미 있는 it_schedule 테이블에는 '없는 컬럼만' 추가
      - vacation / etc_event 테이블은 없을 때만 새로 생성 (CREATE TABLE IF NOT EXISTS)
    덕분에 이미 운영 중인 DB에 이 모듈을 붙여도 기존 일정이 보존된다.
    """
    db = get_db()
    c = db.cursor()

    # [1] it_schedule(기존 테이블)에 이 모듈이 추가로 쓰는 컬럼을 보강
    #     PRAGMA table_info → 현재 컬럼 목록을 읽어와, 없는 것만 ALTER로 추가
    it_cols = [r[1] for r in c.execute("PRAGMA table_info(it_schedule)").fetchall()]
    for col, col_type in [
        ('assignee_id',    'INTEGER'),              # 담당자 (users.id)
        ('repeat_type',    "TEXT DEFAULT 'none'"),  # 반복: none / weekly(매주) / biweekly(격주)
        ('repeat_end',     'TEXT'),                 # 반복 종료일
        ('parent_id',      'INTEGER'),              # (예비) 반복 원본 id
    ]:
        if col not in it_cols:
            c.execute(f"ALTER TABLE it_schedule ADD COLUMN {col} {col_type}")

    # [2] vacation(휴가) 테이블 - 없으면 새로 생성
    c.execute('''CREATE TABLE IF NOT EXISTS vacation (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        start_date  TEXT NOT NULL,
        end_date    TEXT NOT NULL,
        leave_type  TEXT DEFAULT 'full',  -- 종류: full(연차) / half(반차) / quarter(반반차)
        start_time  TEXT,                 -- 반차/반반차 시작 시간 (HH:MM)
        end_time    TEXT,                 -- 반차/반반차 종료 시간 (HH:MM)
        reason      TEXT,                 -- (현재 미사용)
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # [2-1] 예전 버전 vacation 테이블에는 없을 수 있는 컬럼 보강
    vac_cols = [r[1] for r in c.execute("PRAGMA table_info(vacation)").fetchall()]
    for col, col_type in [
        ('leave_type', "TEXT DEFAULT 'full'"),
        ('start_time', 'TEXT'),
        ('end_time',   'TEXT'),
    ]:
        if col not in vac_cols:
            c.execute(f"ALTER TABLE vacation ADD COLUMN {col} {col_type}")

    # [3] etc_event(기타 일정) 테이블 - 회의/세미나 등. 없으면 새로 생성
    c.execute('''CREATE TABLE IF NOT EXISTS etc_event (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        event_date  TEXT NOT NULL,
        end_date    TEXT,
        all_day     INTEGER DEFAULT 0,
        start_time  TEXT,
        end_time    TEXT,
        location    TEXT,
        created_by  INTEGER,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    # etc_event 마이그레이션
    etc_cols = [r[1] for r in c.execute("PRAGMA table_info(etc_event)").fetchall()]
    for col, col_type in [
        ('end_date', 'TEXT'),
        ('all_day',  'INTEGER DEFAULT 0'),
    ]:
        if col not in etc_cols:
            c.execute(f"ALTER TABLE etc_event ADD COLUMN {col} {col_type}")

    db.commit()
    db.close()


# ──────────────────────────────────────────
#  헬퍼: 날짜 범위 생성
# ──────────────────────────────────────────
def date_range(start_str, end_str):
    """start~end 사이의 모든 날짜 문자열 리스트 반환"""
    start = datetime.date.fromisoformat(start_str)
    end   = datetime.date.fromisoformat(end_str)
    days  = []
    cur   = start
    while cur <= end:
        days.append(cur.strftime('%Y-%m-%d'))
        cur += datetime.timedelta(days=1)
    return days


def expand_repeats(schedule):
    """
    단일 it_schedule 레코드(sqlite3.Row)를 받아
    repeat_type에 따라 날짜 목록을 확장하여 반환.
    반환값: [{'id':..,'date':..,'project_name':..,...}, ...]
    """
    s = dict(schedule)
    base_date  = datetime.date.fromisoformat(s['scheduled_date'])
    repeat     = s.get('repeat_type') or 'none'
    repeat_end = s.get('repeat_end')

    if repeat == 'none' or not repeat_end:
        return [s]

    end_date = datetime.date.fromisoformat(repeat_end)
    delta = datetime.timedelta(weeks=1 if repeat == 'weekly' else 2)

    items = []
    cur = base_date
    while cur <= end_date:
        item = dict(s)
        item['scheduled_date'] = cur.strftime('%Y-%m-%d')
        items.append(item)
        cur += delta
    return items


# ──────────────────────────────────────────
#  메인 캘린더 뷰
# ──────────────────────────────────────────
@schedule_bp.route('/')
@login_required
def calendar_view():
    today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')  # input[type=date]용 문자열

    # 쿼리 파라미터로 연·월 이동
    year  = request.args.get('year',  today.year,  type=int)
    month = request.args.get('month', today.month, type=int)

    # 월 경계 처리
    if month < 1:
        month = 12; year -= 1
    if month > 12:
        month = 1;  year += 1

    # 해당 월의 첫날·마지막날
    first_day = datetime.date(year, month, 1)
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)

    first_str = first_day.strftime('%Y-%m-%d')
    last_str  = last_day.strftime('%Y-%m-%d')

    db = get_db()

    # IT 일정 조회 (해당 월 포함 가능성 있는 것 전부)
    raw_it = db.execute('''
        SELECT s.*, p.name as project_name, b.name as board_name,
               u.name as assignee_name
        FROM it_schedule s
        LEFT JOIN projects p   ON s.project_id  = p.id
        LEFT JOIN boards   b   ON s.board_id     = b.id
        LEFT JOIN users    u   ON s.assignee_id  = u.id
        WHERE s.scheduled_date <= ? AND (s.repeat_end >= ? OR s.repeat_end IS NULL)
          AND s.scheduled_date >= date(?, '-3 months')
    ''', (last_str, first_str, first_str)).fetchall()

    # 반복 일정 전개
    it_events = []
    for row in raw_it:
        for item in expand_repeats(row):
            if first_str <= item['scheduled_date'] <= last_str:
                it_events.append(item)

    # 휴가 조회
    vacations_raw = db.execute('''
        SELECT v.*, u.name as user_name
        FROM vacation v
        JOIN users u ON v.user_id = u.id
        WHERE v.start_date <= ? AND v.end_date >= ?
        ORDER BY v.start_date
    ''', (last_str, first_str)).fetchall()

    # 기타 일정 조회 (회의/세미나 등) - 종료일까지 고려해 겹치는 것 전부
    etc_raw = db.execute('''
        SELECT e.*, u.name as creator_name
        FROM etc_event e
        LEFT JOIN users u ON e.created_by = u.id
        WHERE e.event_date <= ?
          AND COALESCE(e.end_date, e.event_date) >= ?
        ORDER BY e.event_date, e.start_time
    ''', (last_str, first_str)).fetchall()

    # ── 세 종류 일정을 하나의 '막대 인스턴스' 목록으로 통합 ──
    #  IT/휴가/기타를 같은 형태({kind, start, end, label, title})로 합쳐서
    #  아래 막대 배치 로직이 종류를 구분하지 않고 한 번에 처리할 수 있게 함.
    #    · kind  : 색을 결정 (it=파랑, vac=노랑, etc=초록)
    #    · label : 막대 안에 보이는 짧은 글자
    #    · title : 마우스를 올렸을 때 보이는 전체 설명(툴팁)
    instances = []

    # [IT 일정] 각 일정은 하루짜리. 라벨은 "프로젝트 / 장비" 형태.
    for ev in it_events:
        d = datetime.date.fromisoformat(ev['scheduled_date'])
        label = ev.get('project_name') or '미지정'
        if ev.get('board_name'):
            label += ' / ' + ev['board_name']
        title = f"[{ev.get('location') or ''}] {label}"
        if ev.get('assignee_name'):
            title += f" - {ev['assignee_name']}"
        instances.append({
            'id': ev['id'], 'kind': 'it', 'start': d, 'end': d,
            'label': '🔵 ' + label, 'title': title,
        })

    # [휴가] 연차는 여러 날 가능(start~end). 반차/반반차는 하루 + 시간 표시.
    for v in vacations_raw:
        s = datetime.date.fromisoformat(v['start_date'])
        e = datetime.date.fromisoformat(v['end_date'])
        lt = v['leave_type'] or 'full'
        if lt == 'full':                                   # 연차 → "홍길동 연차"
            label = f"🟡 {v['user_name']} 연차"
            title = f"{v['user_name']} · 연차"
        elif v['start_time'] and v['end_time']:            # 반차류 → "홍길동 09-13"
            tlabel = f"{fmt_time_short(v['start_time'])}-{fmt_time_short(v['end_time'])}"
            label = f"🟡 {v['user_name']} {tlabel}"
            title = f"{v['user_name']} · {LEAVE_SHORT.get(lt,'반차')} {v['start_time']}~{v['end_time']}"
        else:
            label = f"🟡 {v['user_name']} {LEAVE_SHORT.get(lt,'반차')}"
            title = f"{v['user_name']} · {LEAVE_SHORT.get(lt,'반차')}"
        instances.append({
            'id': v['id'], 'kind': 'vac', 'start': s, 'end': e,
            'label': label, 'title': title,
        })

    # [기타 일정] start~end 기간. 하루종일이면 시간 생략, 아니면 시간 표시.
    for e in etc_raw:
        s = datetime.date.fromisoformat(e['event_date'])
        end = datetime.date.fromisoformat(e['end_date']) if e['end_date'] else s
        if e['all_day']:                                   # 하루종일 → "회의 하루종일"
            label = f"🟢 {e['title']} 하루종일"
            title = f"{e['title']} · 하루종일"
        elif e['start_time'] and e['end_time']:            # 시간 지정 → "회의 14-15"
            tlabel = f"{fmt_time_short(e['start_time'])}-{fmt_time_short(e['end_time'])}"
            label = f"🟢 {e['title']} {tlabel}"
            title = f"{e['title']} {e['start_time']}~{e['end_time']}"
        else:
            label = f"🟢 {e['title']}"
            title = e['title']
        if e['location']:
            title += f" @ {e['location']}"
        instances.append({
            'id': e['id'], 'kind': 'etc', 'start': s, 'end': end,
            'label': label, 'title': title,
        })

    # 프로젝트·사용자 목록 (등록 폼용)
    projects  = db.execute("SELECT * FROM projects WHERE is_active=1 ORDER BY name").fetchall()
    boards_all = db.execute(
        "SELECT b.*, p.name as project_name FROM boards b "
        "JOIN projects p ON b.project_id=p.id WHERE b.is_active=1 ORDER BY p.name, b.name"
    ).fetchall()
    users = db.execute("SELECT * FROM users WHERE is_active=1 ORDER BY name").fetchall()

    # 이번 달 IT 일정 리스트 (날짜순)
    it_list = sorted(it_events, key=lambda x: x['scheduled_date'])

    # 이번 달 휴가 리스트
    vacation_rows = db.execute('''
        SELECT v.*, u.name as user_name
        FROM vacation v
        JOIN users u ON v.user_id = u.id
        WHERE NOT (v.end_date < ? OR v.start_date > ?)
        ORDER BY v.start_date
    ''', (first_str, last_str)).fetchall()
    vacation_list = []
    for v in vacation_rows:
        vd = dict(v)
        lt = vd.get('leave_type') or 'full'
        vd['type_short'] = LEAVE_SHORT.get(lt, '연차')
        vd['type_full']  = LEAVE_FULL.get(lt, '연차(종일)')
        # 반차류는 시간 범위 문자열 (예: 09:00~13:00), 연차는 빈 문자열
        if lt != 'full' and vd.get('start_time') and vd.get('end_time'):
            vd['time_range'] = f"{vd['start_time']}~{vd['end_time']}"
        else:
            vd['time_range'] = ''
        vacation_list.append(vd)

    # 이번 달 기타 일정 리스트
    etc_list = [dict(e) for e in etc_raw]

    db.close()

    # ════════════════════════════════════════════════════════════════════
    #  캘린더를 '주(week)' 단위로 만들고, 각 일정을 가로 막대(bar)로 배치
    # ════════════════════════════════════════════════════════════════════
    #  [핵심 아이디어]
    #   구글 캘린더처럼 여러 날 일정이 칸을 가로질러 이어지는 막대로 보이게 함.
    #   - 한 주 = 7칸(일~토). 각 일정은 그 주 안에서 col_start~col_end 칸을 차지.
    #   - 일정이 여러 주에 걸치면 주마다 잘라서(클리핑) 각각 막대로 그림.
    #   - 같은 칸에서 막대가 겹치지 않도록 '레인(lane=세로 줄)'을 자동 배정.
    #   - 한 주에 막대가 너무 많으면 MAX_LANES까지만 보이고 나머지는 '+N'으로 표시.
    MAX_LANES = 4   # 한 주에 보여줄 최대 막대 줄 수 (초과분은 +N 표시)
    cal_weeks = []
    # 달력 첫 칸을 '일요일'로 맞춤 (헤더가 일~토 순서이므로)
    # weekday(): 월=0..일=6  →  (weekday()+1)%7 만큼 빼면 그 주 일요일로 이동
    cur = first_day - datetime.timedelta(days=(first_day.weekday() + 1) % 7)

    while cur <= last_day:
        week_start = cur                                  # 그 주 일요일
        week_end   = cur + datetime.timedelta(days=6)     # 그 주 토요일

        # (1) 화면에 그릴 날짜 셀 7개 만들기
        days = []
        for i in range(7):
            d = week_start + datetime.timedelta(days=i)
            days.append({
                'date':       d,
                'is_current': d.month == month,   # 이번 달이 아니면 흐리게 표시
                'is_today':   d == today,          # 오늘이면 강조
            })

        # (2) 이 주에 걸치는 일정만 골라, 주 경계에 맞춰 칸(col)을 자름
        #     예) 6/27(토)~6/30(화) 일정 → 이번 주에서는 토요일 1칸,
        #         다음 주에서는 일~화 3칸으로 각각 잘려서 막대 2개가 됨
        week_evs = []
        for inst in instances:
            if inst['end'] >= week_start and inst['start'] <= week_end:
                cs = max(0, (inst['start'] - week_start).days)  # 시작 칸(0~6)
                ce = min(6, (inst['end']   - week_start).days)  # 끝 칸(0~6)
                week_evs.append({
                    'id':       inst['id'],
                    'kind':     inst['kind'],     # it / vac / etc (색 구분)
                    'label':    inst['label'],
                    'title':    inst['title'],    # 마우스 올리면 보이는 전체 설명
                    'col_start': cs,
                    'col_end':   ce,
                    'is_start': inst['start'] >= week_start,  # 진짜 시작이 이 주 안 → 왼쪽 모서리 둥글게
                    'is_end':   inst['end']   <= week_end,    # 진짜 끝이 이 주 안   → 오른쪽 모서리 둥글게
                })

        # (3) 막대 정렬: 시작 칸이 빠른 순 → 같으면 긴 막대 먼저
        week_evs.sort(key=lambda x: (x['col_start'], -(x['col_end'] - x['col_start'])))

        # (4) 레인(세로 줄) 배정 - '그리디' 방식
        #     각 막대를, 칸이 겹치지 않는 가장 위쪽 줄에 차례로 끼워 넣음.
        #     겹치는 막대는 자동으로 아래 줄로 내려가 서로 안 겹치게 됨.
        lanes = []  # lanes[줄번호] = 그 줄이 이미 차지한 (시작칸, 끝칸) 목록
        for ev in week_evs:
            placed = False
            for li, lane in enumerate(lanes):
                # 이 줄(lane)의 기존 막대들과 하나도 안 겹치면 여기에 배치
                if all(ev['col_end'] < o[0] or ev['col_start'] > o[1] for o in lane):
                    lane.append((ev['col_start'], ev['col_end']))
                    ev['lane'] = li
                    placed = True
                    break
            if not placed:                 # 들어갈 줄이 없으면 새 줄을 만듦
                lanes.append([(ev['col_start'], ev['col_end'])])
                ev['lane'] = len(lanes) - 1

        # (5) MAX_LANES까지만 화면에 그리고, 초과 막대는 '+N'으로 셈
        bars     = [e for e in week_evs if e['lane'] < MAX_LANES]   # 실제로 그릴 막대
        overflow = [e for e in week_evs if e['lane'] >= MAX_LANES]  # 넘쳐서 +N 처리할 막대

        # 날짜(칸)별로 넘친 막대가 몇 개인지 세기 → 해당 칸에 '+N' 표시
        overflow_count = [0] * 7
        for e in overflow:
            for c in range(e['col_start'], e['col_end'] + 1):
                overflow_count[c] += 1

        cal_weeks.append({
            'days':           days,
            'bars':           bars,
            'overflow_count': overflow_count,
            'n_lanes':        min(len(lanes), MAX_LANES),  # 이 주의 막대 줄 수(높이 계산용)
        })
        cur += datetime.timedelta(weeks=1)

    return render_template('schedule/calendar.html',
                           year=year, month=month,
                           today=today,
                           today_str=today_str,
                           cal_weeks=cal_weeks,
                           it_list=it_list,
                           vacation_list=vacation_list,
                           etc_list=etc_list,
                           projects=projects,
                           boards_all=boards_all,
                           users=users,
                           LEAVE_TYPES=LEAVE_TYPES,
                           LOCATIONS=['DSR', 'Tera'])


# ──────────────────────────────────────────
#  IT 일정 등록
# ──────────────────────────────────────────
@schedule_bp.route('/it/add', methods=['POST'])
@login_required
def add_it():
    project_id     = request.form.get('project_id',  type=int)
    board_id       = request.form.get('board_id',    type=int)
    location       = request.form.get('location', '').strip()
    scheduled_date = request.form.get('scheduled_date', '').strip()
    assignee_id    = request.form.get('assignee_id', type=int)
    repeat_type    = request.form.get('repeat_type', 'none')
    repeat_end     = request.form.get('repeat_end', '').strip() or None
    notes          = request.form.get('notes', '').strip()
    jenkins_job    = request.form.get('jenkins_job', '').strip()

    if not scheduled_date:
        flash('날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    if not project_id or not board_id:
        flash('프로젝트와 장비를 선택해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view', tab='it'))

    # repeat 설정 검증
    if repeat_type != 'none' and not repeat_end:
        flash('반복 종료일을 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    db = get_db()
    db.execute('''
        INSERT INTO it_schedule
        (project_id, board_id, location, scheduled_date,
         assignee_id, repeat_type, repeat_end, notes, jenkins_job, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
    ''', (project_id, board_id, location, scheduled_date,
          assignee_id, repeat_type, repeat_end, notes, jenkins_job))
    db.commit()
    db.close()

    flash('✅ IT 일정이 등록되었습니다.', 'success')
    y, m = scheduled_date[:4], int(scheduled_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m))


# ──────────────────────────────────────────
#  IT 일정 삭제
# ──────────────────────────────────────────
@schedule_bp.route('/it/delete/<int:schedule_id>', methods=['POST'])
@login_required
def delete_it(schedule_id):
    year  = request.form.get('year',  datetime.date.today().year,  type=int)
    month = request.form.get('month', datetime.date.today().month, type=int)

    db = get_db()
    db.execute("DELETE FROM it_schedule WHERE id=?", (schedule_id,))
    db.commit()
    db.close()

    flash('🗑️ IT 일정이 삭제되었습니다.', 'success')
    return redirect(url_for('schedule.calendar_view', year=year, month=month, tab='it'))


# ──────────────────────────────────────────
#  IT 일정 상태 변경 (AJAX)
# ──────────────────────────────────────────
@schedule_bp.route('/it/status/<int:schedule_id>', methods=['POST'])
@login_required
def update_it_status(schedule_id):
    status = request.json.get('status', 'pending')
    db = get_db()
    db.execute("UPDATE it_schedule SET status=? WHERE id=?", (status, schedule_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# ──────────────────────────────────────────
#  휴가 등록
# ──────────────────────────────────────────
@schedule_bp.route('/vacation/add', methods=['POST'])
@login_required
def add_vacation():
    user_id    = request.form.get('vacation_user_id', type=int)
    start_date = request.form.get('start_date', '').strip()
    end_date   = request.form.get('end_date', '').strip()
    leave_type = request.form.get('leave_type', 'full')
    is_multi   = request.form.get('vac_multi') == '1'
    start_time = request.form.get('start_time', '').strip()
    end_time   = request.form.get('end_time', '').strip()

    if not user_id or not start_date:
        flash('이름과 날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    # 반차/반반차는 하루짜리 → 종료일을 시작일과 동일하게 강제
    if leave_type != 'full':
        end_date = start_date
        # 시간 검증
        if not start_time or not end_time:
            flash('반차/반반차는 시작·종료 시간을 입력해주세요.', 'error')
            return redirect(url_for('schedule.calendar_view', tab='vac'))
        if not valid_time(start_time) or not valid_time(end_time):
            flash('시간 형식이 올바르지 않아요. (예: 09:30)', 'error')
            return redirect(url_for('schedule.calendar_view', tab='vac'))
        if start_time >= end_time:
            flash('종료 시간이 시작 시간보다 빨라요.', 'error')
            return redirect(url_for('schedule.calendar_view', tab='vac'))
    else:
        # 연차는 시간 의미 없음
        start_time = None
        end_time   = None
        # '이틀 이상' 체크 안 했으면 단일 연차 → 종료일 = 시작일 (이전 값 잔재 무시)
        if not is_multi:
            end_date = start_date
        if not end_date:
            end_date = start_date

    if start_date > end_date:
        flash('종료일이 시작일보다 빠릅니다.', 'error')
        return redirect(url_for('schedule.calendar_view', tab='vac'))

    db = get_db()
    db.execute(
        "INSERT INTO vacation "
        "(user_id, start_date, end_date, leave_type, start_time, end_time) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, start_date, end_date, leave_type, start_time, end_time)
    )
    db.commit()
    db.close()

    flash('✅ 휴가가 등록되었습니다.', 'success')
    y, m = start_date[:4], int(start_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m, tab='vac'))


# ──────────────────────────────────────────
#  휴가 삭제
# ──────────────────────────────────────────
@schedule_bp.route('/vacation/delete/<int:vacation_id>', methods=['POST'])
@login_required
def delete_vacation(vacation_id):
    year  = request.form.get('year',  datetime.date.today().year,  type=int)
    month = request.form.get('month', datetime.date.today().month, type=int)

    db = get_db()
    # 본인 또는 관리자만 삭제 가능
    vac = db.execute("SELECT * FROM vacation WHERE id=?", (vacation_id,)).fetchone()
    if vac and (vac['user_id'] == session.get('user_id') or session.get('role') == 'admin'):
        db.execute("DELETE FROM vacation WHERE id=?", (vacation_id,))
        db.commit()
        flash('🗑️ 휴가 일정이 삭제되었습니다.', 'success')
    else:
        flash('삭제 권한이 없습니다.', 'error')
    db.close()

    return redirect(url_for('schedule.calendar_view', year=year, month=month, tab='vac'))


# ──────────────────────────────────────────
#  기타 일정 등록 (회의/세미나 등) - 누구나 가능
# ──────────────────────────────────────────
@schedule_bp.route('/etc/add', methods=['POST'])
@login_required
def add_etc():
    title      = request.form.get('title', '').strip()
    event_date = request.form.get('event_date', '').strip()
    is_multi   = request.form.get('etc_multi') == '1'
    end_date   = request.form.get('etc_end_date', '').strip() or None
    all_day    = 1 if request.form.get('all_day') else 0
    start_time = request.form.get('etc_start_time', '').strip() or None
    end_time   = request.form.get('etc_end_time', '').strip() or None
    location   = request.form.get('etc_location', '').strip()

    if not title or not event_date:
        flash('제목과 날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view', tab='etc'))

    # '이틀 이상' 체크 안 했으면 단일 일정 → 종료일 = 시작일 (이전 값 잔재 무시)
    if not is_multi:
        end_date = event_date
    # 종료일 미입력 시 시작일과 동일
    if not end_date:
        end_date = event_date
    if event_date > end_date:
        flash('종료일이 시작일보다 빠릅니다.', 'error')
        return redirect(url_for('schedule.calendar_view', tab='etc'))

    # 하루종일이면 시간 무시
    if all_day:
        start_time = None
        end_time   = None
    else:
        # 시간 입력했으면 형식 검증
        if start_time and not valid_time(start_time):
            flash('시작 시간 형식이 올바르지 않아요. (예: 14:30)', 'error')
            return redirect(url_for('schedule.calendar_view', tab='etc'))
        if end_time and not valid_time(end_time):
            flash('종료 시간 형식이 올바르지 않아요. (예: 15:30)', 'error')
            return redirect(url_for('schedule.calendar_view', tab='etc'))
        if start_time and end_time and start_time >= end_time:
            flash('종료 시간이 시작 시간보다 빨라요.', 'error')
            return redirect(url_for('schedule.calendar_view', tab='etc'))

    db = get_db()
    db.execute(
        "INSERT INTO etc_event "
        "(title, event_date, end_date, all_day, start_time, end_time, location, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, event_date, end_date, all_day, start_time, end_time, location,
         session.get('user_id'))
    )
    db.commit()
    db.close()

    flash('✅ 기타 일정이 등록되었습니다.', 'success')
    y, m = event_date[:4], int(event_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m, tab='etc'))


# ──────────────────────────────────────────
#  기타 일정 삭제 - 누구나 가능
# ──────────────────────────────────────────
@schedule_bp.route('/etc/delete/<int:event_id>', methods=['POST'])
@login_required
def delete_etc(event_id):
    year  = request.form.get('year',  datetime.date.today().year,  type=int)
    month = request.form.get('month', datetime.date.today().month, type=int)

    db = get_db()
    db.execute("DELETE FROM etc_event WHERE id=?", (event_id,))
    db.commit()
    db.close()

    flash('🗑️ 기타 일정이 삭제되었습니다.', 'success')
    return redirect(url_for('schedule.calendar_view', year=year, month=month, tab='etc'))


# ── 일괄 삭제 (체크박스 선택) ──
@schedule_bp.route('/bulk_delete/<kind>', methods=['POST'])
@login_required
def bulk_delete(kind):
    year  = request.form.get('year',  datetime.date.today().year,  type=int)
    month = request.form.get('month', datetime.date.today().month, type=int)
    ids   = request.form.getlist('ids')   # 체크된 id 목록

    # 정수만 추려서 안전하게
    id_list = [int(i) for i in ids if i.isdigit()]
    if not id_list:
        flash('선택된 항목이 없습니다.', 'error')
        return redirect(url_for('schedule.calendar_view', year=year, month=month, tab=kind))

    placeholders = ','.join('?' * len(id_list))
    db = get_db()

    if kind == 'it':
        db.execute(f"DELETE FROM it_schedule WHERE id IN ({placeholders})", id_list)
        label = 'IT 일정'
    elif kind == 'vac':
        # 휴가는 본인 또는 관리자만 (관리자가 아니면 본인 것만 삭제)
        if session.get('role') == 'admin':
            db.execute(f"DELETE FROM vacation WHERE id IN ({placeholders})", id_list)
        else:
            params = id_list + [session.get('user_id')]
            db.execute(f"DELETE FROM vacation WHERE id IN ({placeholders}) AND user_id=?", params)
        label = '휴가'
    elif kind == 'etc':
        db.execute(f"DELETE FROM etc_event WHERE id IN ({placeholders})", id_list)
        label = '기타 일정'
    else:
        db.close()
        flash('잘못된 요청입니다.', 'error')
        return redirect(url_for('schedule.calendar_view', year=year, month=month))

    db.commit()
    db.close()

    flash(f'🗑️ {label} {len(id_list)}건이 삭제되었습니다.', 'success')
    return redirect(url_for('schedule.calendar_view', year=year, month=month, tab=kind))


# ══════════════════════════════════════════
#  일정 수정 (IT / 휴가 / 기타)
# ══════════════════════════════════════════

# ── 단건 조회 (수정 폼 채우기용 JSON) ──
@schedule_bp.route('/item/<kind>/<int:item_id>')
@login_required
def get_item(kind, item_id):
    db = get_db()
    if kind == 'it':
        row = db.execute("SELECT * FROM it_schedule WHERE id=?", (item_id,)).fetchone()
    elif kind == 'vac':
        row = db.execute("SELECT * FROM vacation WHERE id=?", (item_id,)).fetchone()
    elif kind == 'etc':
        row = db.execute("SELECT * FROM etc_event WHERE id=?", (item_id,)).fetchone()
    else:
        row = None
    db.close()
    if not row:
        return jsonify({'ok': False}), 404
    return jsonify({'ok': True, 'item': dict(row)})


# ── IT 일정 수정 ──
@schedule_bp.route('/it/edit/<int:schedule_id>', methods=['POST'])
@login_required
def edit_it(schedule_id):
    project_id     = request.form.get('project_id',  type=int)
    board_id       = request.form.get('board_id',    type=int)
    location       = request.form.get('location', '').strip()
    scheduled_date = request.form.get('scheduled_date', '').strip()
    assignee_id    = request.form.get('assignee_id', type=int)
    repeat_type    = request.form.get('repeat_type', 'none')
    repeat_end     = request.form.get('repeat_end', '').strip() or None
    notes          = request.form.get('notes', '').strip()
    jenkins_job    = request.form.get('jenkins_job', '').strip()

    if not scheduled_date:
        flash('날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))
    if not project_id or not board_id:
        flash('프로젝트와 장비를 선택해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view', tab='it'))
    if repeat_type != 'none' and not repeat_end:
        flash('반복 종료일을 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    db = get_db()
    db.execute('''UPDATE it_schedule SET
        project_id=?, board_id=?, location=?, scheduled_date=?,
        assignee_id=?, repeat_type=?, repeat_end=?, notes=?, jenkins_job=?
        WHERE id=?''',
        (project_id, board_id, location, scheduled_date,
         assignee_id, repeat_type, repeat_end, notes, jenkins_job, schedule_id))
    db.commit()
    db.close()

    flash('✏️ IT 일정이 수정되었습니다.', 'success')
    y, m = scheduled_date[:4], int(scheduled_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m, tab='it'))


# ── 휴가 수정 ──
@schedule_bp.route('/vacation/edit/<int:vacation_id>', methods=['POST'])
@login_required
def edit_vacation(vacation_id):
    db = get_db()
    vac = db.execute("SELECT * FROM vacation WHERE id=?", (vacation_id,)).fetchone()
    if not vac or (vac['user_id'] != session.get('user_id') and session.get('role') != 'admin'):
        db.close()
        flash('수정 권한이 없습니다.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    user_id    = request.form.get('vacation_user_id', type=int)
    start_date = request.form.get('start_date', '').strip()
    end_date   = request.form.get('end_date', '').strip()
    leave_type = request.form.get('leave_type', 'full')
    start_time = request.form.get('start_time', '').strip()
    end_time   = request.form.get('end_time', '').strip()

    if not user_id or not start_date:
        db.close()
        flash('이름과 날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    if leave_type != 'full':
        end_date = start_date
        if not start_time or not end_time:
            db.close()
            flash('반차/반반차는 시간을 입력해주세요.', 'error')
            return redirect(url_for('schedule.calendar_view'))
    else:
        start_time = None
        end_time   = None
        if not end_date:
            end_date = start_date

    if start_date > end_date:
        db.close()
        flash('종료일이 시작일보다 빠릅니다.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    db.execute('''UPDATE vacation SET
        user_id=?, start_date=?, end_date=?, leave_type=?, start_time=?, end_time=?
        WHERE id=?''',
        (user_id, start_date, end_date, leave_type, start_time, end_time, vacation_id))
    db.commit()
    db.close()

    flash('✏️ 휴가가 수정되었습니다.', 'success')
    y, m = start_date[:4], int(start_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m, tab='vac'))


# ── 기타 일정 수정 ──
@schedule_bp.route('/etc/edit/<int:event_id>', methods=['POST'])
@login_required
def edit_etc(event_id):
    title      = request.form.get('title', '').strip()
    event_date = request.form.get('event_date', '').strip()
    end_date   = request.form.get('etc_end_date', '').strip() or None
    all_day    = 1 if request.form.get('all_day') else 0
    start_time = request.form.get('etc_start_time', '').strip() or None
    end_time   = request.form.get('etc_end_time', '').strip() or None
    location   = request.form.get('etc_location', '').strip()

    if not title or not event_date:
        flash('제목과 날짜를 입력해주세요.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    if not end_date:
        end_date = event_date
    if event_date > end_date:
        flash('종료일이 시작일보다 빠릅니다.', 'error')
        return redirect(url_for('schedule.calendar_view'))

    if all_day:
        start_time = None
        end_time   = None

    db = get_db()
    db.execute('''UPDATE etc_event SET
        title=?, event_date=?, end_date=?, all_day=?, start_time=?, end_time=?, location=?
        WHERE id=?''',
        (title, event_date, end_date, all_day, start_time, end_time, location, event_id))
    db.commit()
    db.close()

    flash('✏️ 기타 일정이 수정되었습니다.', 'success')
    y, m = event_date[:4], int(event_date[5:7])
    return redirect(url_for('schedule.calendar_view', year=y, month=m, tab='etc'))
