from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from database import get_db

users_bp = Blueprint('users', __name__, url_prefix='/admin/users')

def admin_required(f):
    """관리자 권한 확인 데코레이터"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('관리자만 접근할 수 있습니다.', 'error')
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated


@users_bp.route('/')
@admin_required
def list_users():
    db = get_db()
    users = db.execute(
        "SELECT * FROM users ORDER BY role DESC, location, name"
    ).fetchall()
    db.close()
    return render_template('users/list.html', users=users)


@users_bp.route('/add', methods=['GET', 'POST'])
@admin_required
def add_user():
    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        role         = request.form.get('role', 'member')
        location     = request.form.get('location', 'DSR')
        use_password = 1 if request.form.get('use_password') else 0
        password     = request.form.get('password', '').strip()

        if not name:
            flash('이름을 입력해주세요.', 'error')
            return render_template('users/form.html', action='add', user=None)

        # 비밀번호 사용 시 입력 필수
        if use_password and not password:
            flash('비밀번호를 입력해주세요.', 'error')
            return render_template('users/form.html', action='add', user=None)

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (name, role, location, use_password, password) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, role, location, use_password,
                 password if use_password else None)
            )
            db.commit()
            flash(f'✅ {name} 님이 등록되었습니다.', 'success')
            return redirect(url_for('users.list_users'))
        except Exception:
            flash(f'이미 존재하는 이름입니다: {name}', 'error')
            return render_template('users/form.html', action='add', user=None)
        finally:
            db.close()

    return render_template('users/form.html', action='add', user=None)


@users_bp.route('/edit/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def edit_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    if not user:
        flash('사용자를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('users.list_users'))

    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        role         = request.form.get('role', 'member')
        location     = request.form.get('location', 'DSR')
        is_active    = 1 if request.form.get('is_active') else 0
        use_password = 1 if request.form.get('use_password') else 0
        password     = request.form.get('password', '').strip()

        if not name:
            flash('이름을 입력해주세요.', 'error')
            return render_template('users/form.html', action='edit', user=user)

        if use_password and not password:
            flash('비밀번호를 입력해주세요.', 'error')
            return render_template('users/form.html', action='edit', user=user)

        # 비밀번호 미입력 시 기존 비밀번호 유지
        if use_password and not password:
            password = user['password']

        try:
            db.execute(
                "UPDATE users SET name=?, role=?, location=?, is_active=?, "
                "use_password=?, password=? WHERE id=?",
                (name, role, location, is_active,
                 use_password, password if use_password else None,
                 user_id)
            )
            db.commit()
            flash(f'✅ {name} 님 정보가 수정되었습니다.', 'success')
            return redirect(url_for('users.list_users'))
        except Exception:
            flash(f'이미 존재하는 이름입니다: {name}', 'error')
            return render_template('users/form.html', action='edit', user=user)
        finally:
            db.close()

    db.close()
    return render_template('users/form.html', action='edit', user=user)


@users_bp.route('/delete/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    if not user:
        flash('사용자를 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('users.list_users'))

    if user['role'] == 'admin':
        # 관리자가 1명만 있으면 삭제 불가
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            flash('관리자는 최소 1명 이상 필요합니다.', 'error')
            db.close()
            return redirect(url_for('users.list_users'))

    name = user['name']
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    db.close()
    flash(f'🗑️ {name} 님이 삭제되었습니다.', 'success')
    return redirect(url_for('users.list_users'))


@users_bp.route('/toggle/<int:user_id>', methods=['POST'])
@admin_required
def toggle_user(user_id):
    """사용자 활성/비활성 전환"""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user:
        new_status = 0 if user['is_active'] else 1
        db.execute("UPDATE users SET is_active=? WHERE id=?", (new_status, user_id))
        db.commit()
        status_text = '활성화' if new_status else '비활성화'
        flash(f"✅ {user['name']} 님을 {status_text}했습니다.", 'success')
    db.close()
    return redirect(url_for('users.list_users'))
