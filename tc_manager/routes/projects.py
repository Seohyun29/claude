from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from database import get_db
from functools import wraps

projects_bp = Blueprint('projects', __name__, url_prefix='/admin/projects')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('관리자만 접근할 수 있습니다.', 'error')
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────
#  프로젝트 목록
# ──────────────────────────────────────────
@projects_bp.route('/')
@admin_required
def list_projects():
    db = get_db()
    # 프로젝트별 보드 수도 함께 조회
    projects = db.execute('''
        SELECT p.*,
               COUNT(b.id) as board_count,
               SUM(CASE WHEN b.is_active=1 THEN 1 ELSE 0 END) as active_board_count
        FROM projects p
        LEFT JOIN boards b ON b.project_id = p.id
        GROUP BY p.id
        ORDER BY p.is_active DESC, p.name
    ''').fetchall()
    db.close()
    return render_template('projects/list.html', projects=projects)


# ──────────────────────────────────────────
#  프로젝트 추가
# ──────────────────────────────────────────
@projects_bp.route('/add', methods=['GET', 'POST'])
@admin_required
def add_project():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            flash('프로젝트 이름을 입력해주세요.', 'error')
            return render_template('projects/form.html', action='add', project=None)

        db = get_db()
        try:
            db.execute(
                "INSERT INTO projects (name, description) VALUES (?, ?)",
                (name, description)
            )
            db.commit()
            flash(f'✅ 프로젝트 [{name}] 가 등록되었습니다.', 'success')
            return redirect(url_for('projects.list_projects'))
        except Exception:
            flash(f'이미 존재하는 프로젝트 이름입니다: {name}', 'error')
            return render_template('projects/form.html', action='add', project=None)
        finally:
            db.close()

    return render_template('projects/form.html', action='add', project=None)


# ──────────────────────────────────────────
#  프로젝트 수정
# ──────────────────────────────────────────
@projects_bp.route('/edit/<int:project_id>', methods=['GET', 'POST'])
@admin_required
def edit_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    if not project:
        flash('프로젝트를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        is_active = 1 if request.form.get('is_active') else 0

        if not name:
            flash('프로젝트 이름을 입력해주세요.', 'error')
            return render_template('projects/form.html', action='edit', project=project)

        try:
            db.execute(
                "UPDATE projects SET name=?, description=?, is_active=? WHERE id=?",
                (name, description, is_active, project_id)
            )
            db.commit()
            flash(f'✅ 프로젝트 [{name}] 가 수정되었습니다.', 'success')
            return redirect(url_for('projects.list_projects'))
        except Exception:
            flash(f'이미 존재하는 프로젝트 이름입니다: {name}', 'error')
            return render_template('projects/form.html', action='edit', project=project)
        finally:
            db.close()

    db.close()
    return render_template('projects/form.html', action='edit', project=project)


# ──────────────────────────────────────────
#  프로젝트 삭제
# ──────────────────────────────────────────
@projects_bp.route('/delete/<int:project_id>', methods=['POST'])
@admin_required
def delete_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    if not project:
        flash('프로젝트를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    # 연결된 보드 확인
    board_count = db.execute(
        "SELECT COUNT(*) FROM boards WHERE project_id=?", (project_id,)
    ).fetchone()[0]

    if board_count > 0:
        flash(f'먼저 이 프로젝트의 장비 {board_count}개를 삭제해주세요.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    name = project['name']
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.commit()
    db.close()
    flash(f'🗑️ 프로젝트 [{name}] 가 삭제되었습니다.', 'success')
    return redirect(url_for('projects.list_projects'))


# ──────────────────────────────────────────
#  장비(보드) 목록 - 특정 프로젝트
# ──────────────────────────────────────────
@projects_bp.route('/<int:project_id>/boards')
@admin_required
def list_boards(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    if not project:
        flash('프로젝트를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    boards = db.execute(
        "SELECT * FROM boards WHERE project_id=? ORDER BY is_active DESC, name",
        (project_id,)
    ).fetchall()
    db.close()
    return render_template('projects/boards.html', project=project, boards=boards)


# ──────────────────────────────────────────
#  장비(보드) 추가
# ──────────────────────────────────────────
@projects_bp.route('/<int:project_id>/boards/add', methods=['GET', 'POST'])
@admin_required
def add_board(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    if not project:
        flash('프로젝트를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        version = request.form.get('version', '').strip()

        if not name:
            flash('장비 이름을 입력해주세요.', 'error')
            return render_template('projects/board_form.html',
                                   action='add', project=project, board=None)
        try:
            db.execute(
                "INSERT INTO boards (project_id, name, version) VALUES (?, ?, ?)",
                (project_id, name, version)
            )
            db.commit()
            flash(f'✅ 장비 [{name}] 가 등록되었습니다.', 'success')
            return redirect(url_for('projects.list_boards', project_id=project_id))
        except Exception:
            flash(f'이미 존재하는 장비 이름입니다: {name}', 'error')
            return render_template('projects/board_form.html',
                                   action='add', project=project, board=None)
        finally:
            db.close()

    db.close()
    return render_template('projects/board_form.html',
                           action='add', project=project, board=None)


# ──────────────────────────────────────────
#  장비(보드) 수정
# ──────────────────────────────────────────
@projects_bp.route('/<int:project_id>/boards/edit/<int:board_id>', methods=['GET', 'POST'])
@admin_required
def edit_board(project_id, board_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    board = db.execute(
        "SELECT * FROM boards WHERE id=? AND project_id=?", (board_id, project_id)
    ).fetchone()

    if not project or not board:
        flash('정보를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_projects'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        version = request.form.get('version', '').strip()
        is_active = 1 if request.form.get('is_active') else 0

        if not name:
            flash('장비 이름을 입력해주세요.', 'error')
            return render_template('projects/board_form.html',
                                   action='edit', project=project, board=board)
        try:
            db.execute(
                "UPDATE boards SET name=?, version=?, is_active=? WHERE id=?",
                (name, version, is_active, board_id)
            )
            db.commit()
            flash(f'✅ 장비 [{name}] 가 수정되었습니다.', 'success')
            return redirect(url_for('projects.list_boards', project_id=project_id))
        except Exception:
            flash(f'이미 존재하는 장비 이름입니다: {name}', 'error')
            return render_template('projects/board_form.html',
                                   action='edit', project=project, board=board)
        finally:
            db.close()

    db.close()
    return render_template('projects/board_form.html',
                           action='edit', project=project, board=board)


# ──────────────────────────────────────────
#  장비(보드) 삭제
# ──────────────────────────────────────────
@projects_bp.route('/<int:project_id>/boards/delete/<int:board_id>', methods=['POST'])
@admin_required
def delete_board(project_id, board_id):
    db = get_db()
    board = db.execute(
        "SELECT * FROM boards WHERE id=? AND project_id=?", (board_id, project_id)
    ).fetchone()

    if not board:
        flash('장비를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('projects.list_boards', project_id=project_id))

    name = board['name']
    db.execute("DELETE FROM boards WHERE id=?", (board_id,))
    db.commit()
    db.close()
    flash(f'🗑️ 장비 [{name}] 가 삭제되었습니다.', 'success')
    return redirect(url_for('projects.list_boards', project_id=project_id))
