from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from database import get_db
import datetime

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    db = get_db()
    users = db.execute(
        "SELECT * FROM users WHERE is_active=1 ORDER BY location, role DESC, name"
    ).fetchall()
    db.close()
    return render_template('main/select_user.html', users=users)


@main_bp.route('/select_user', methods=['POST'])
def select_user():
    user_id  = request.form.get('user_id')
    password = request.form.get('password', '').strip()

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)
    ).fetchone()
    db.close()

    if not user:
        flash('사용자를 선택해주세요.', 'error')
        return redirect(url_for('main.index'))

    # 비밀번호 사용 중인 경우 확인
    if user['use_password']:
        if not password:
            flash('비밀번호를 입력해주세요.', 'error')
            return redirect(url_for('main.index') + f'?pw_user={user_id}')
        if password != user['password']:
            flash('비밀번호가 틀렸습니다.', 'error')
            return redirect(url_for('main.index') + f'?pw_user={user_id}')

    session['user_id']   = user['id']
    session['user_name'] = user['name']
    session['role']      = user['role']
    session['location']  = user['location']
    return redirect(url_for('main.dashboard'))


@main_bp.route('/dashboard')
def dashboard():
    if not session.get('user_id'):
        return redirect(url_for('main.index'))

    db = get_db()
    today     = datetime.date.today().strftime('%Y-%m-%d')
    user_id   = session['user_id']
    location  = session['location']

    # ── 1. 오늘 IT 일정 (장소별, 최대 2개) ──────────────────
    it_schedules = db.execute('''
        SELECT s.*, p.name as project_name, b.name as board_name
        FROM it_schedule s
        LEFT JOIN projects p ON s.project_id = p.id
        LEFT JOIN boards b ON s.board_id = b.id
        WHERE s.scheduled_date = ?
        ORDER BY s.location, s.id
    ''', (today,)).fetchall()

    loc1_schedules = [s for s in it_schedules if s['location'] == 'DSR'][:2]
    loc2_schedules = [s for s in it_schedules if s['location'] == 'Tera'][:2]

    # ── 2. 프로젝트별 리뷰 진행률 ────────────────────────────
    projects = db.execute(
        "SELECT * FROM projects WHERE is_active=1 ORDER BY name"
    ).fetchall()

    project_stats = []
    for p in projects:
        total = db.execute(
            "SELECT COUNT(*) FROM tc_review WHERE project_id=?", (p['id'],)
        ).fetchone()[0]
        if total == 0:
            continue
        by_status = db.execute('''
            SELECT overall_status, COUNT(*) as cnt
            FROM tc_review WHERE project_id=?
            GROUP BY overall_status
        ''', (p['id'],)).fetchall()
        status_map = {r['overall_status']: r['cnt'] for r in by_status}
        reviewed = status_map.get('Reviewed', 0)
        in_review = status_map.get('In Review', 0)
        bl_in_review = status_map.get('BL in Review', 0)
        draft = status_map.get('Draft', 0)
        pct = round(reviewed / total * 100, 1) if total else 0
        project_stats.append({
            'project': p,
            'total': total,
            'reviewed': reviewed,
            'in_review': in_review,
            'bl_in_review': bl_in_review,
            'draft': draft,
            'pct': pct,
        })

    # ── 3. 나의 리뷰 현황 (내가 담당한 것) ──────────────────
    my_stats = db.execute('''
        SELECT
            SUM(CASE WHEN rs.status='Draft'        THEN 1 ELSE 0 END) as draft,
            SUM(CASE WHEN rs.status='In Review'    THEN 1 ELSE 0 END) as in_review,
            SUM(CASE WHEN rs.status='BL in Review' THEN 1 ELSE 0 END) as bl_in_review,
            SUM(CASE WHEN rs.status='Reviewed'     THEN 1 ELSE 0 END) as reviewed,
            COUNT(*) as total
        FROM review_status rs
        WHERE rs.reviewer_id = ?
    ''', (user_id,)).fetchone()

    # 내가 담당한 미완료 TC 목록 (최근 10개)
    my_pending = db.execute('''
        SELECT rs.*, t.id as tc_db_id, t.tc_id, t.title, t.tc_url,
               p.name as project_name, b.name as board_name
        FROM review_status rs
        JOIN tc_review t ON rs.tc_id = t.id
        JOIN projects p ON t.project_id = p.id
        JOIN boards b ON rs.board_id = b.id
        WHERE rs.reviewer_id = ? AND rs.status != 'Reviewed'
        ORDER BY rs.updated_at DESC
        LIMIT 10
    ''', (user_id,)).fetchall()

    # ── 4. 팀 전체 최근 활동 (최근 15건) ────────────────────
    recent_activities = db.execute('''
        SELECT rs.status, rs.updated_at, rs.comment,
               t.tc_id, t.title, t.tc_url,
               p.name as project_name,
               b.name as board_name,
               u.name as reviewer_name
        FROM review_status rs
        JOIN tc_review t  ON rs.tc_id    = t.id
        JOIN projects p   ON t.project_id = p.id
        JOIN boards b     ON rs.board_id  = b.id
        LEFT JOIN users u ON rs.reviewer_id = u.id
        WHERE rs.updated_at IS NOT NULL
        ORDER BY rs.updated_at DESC
        LIMIT 15
    ''').fetchall()

    # ── 5. 전체 요약 수치 ────────────────────────────────────
    total_tc = db.execute("SELECT COUNT(*) FROM tc_review").fetchone()[0]
    overall_summary = db.execute('''
        SELECT overall_status, COUNT(*) as cnt
        FROM tc_review GROUP BY overall_status
    ''').fetchall()
    summary_map = {r['overall_status']: r['cnt'] for r in overall_summary}

    db.close()
    return render_template('main/dashboard.html',
                           today=today,
                           loc1_schedules=loc1_schedules,
                           loc2_schedules=loc2_schedules,
                           project_stats=project_stats,
                           my_stats=my_stats,
                           my_pending=my_pending,
                           recent_activities=recent_activities,
                           total_tc=total_tc,
                           summary_map=summary_map)


@main_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main.index'))
