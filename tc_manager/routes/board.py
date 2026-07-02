import os
import uuid
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, session, send_from_directory, abort)
from database import get_db
from functools import wraps

board_bp = Blueprint('board', __name__, url_prefix='/board')

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', 'board')
ALLOWED_EXT = {'pdf', 'xlsx', 'xls', 'docx', 'doc', 'pptx', 'ppt',
               'txt', 'png', 'jpg', 'jpeg', 'gif', 'zip', 'csv'}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def file_size_str(size):
    if size < 1024:
        return f'{size}B'
    elif size < 1024 * 1024:
        return f'{size//1024}KB'
    else:
        return f'{size//1024//1024:.1f}MB'


# ── 게시판 목록 ───────────────────────────────────────
@board_bp.route('/')
@login_required
def list_posts():
    db = get_db()
    category = request.args.get('category', '')  # '' = 전체
    keyword  = request.args.get('q', '').strip()
    page     = request.args.get('page', 1, type=int)
    per_page = 15

    where  = ["1=1"]
    params = []
    if category:
        where.append("p.category=?"); params.append(category)
    if keyword:
        where.append("(p.title LIKE ? OR p.content LIKE ?)")
        kw = f'%{keyword}%'
        params += [kw, kw]

    total = db.execute(
        f"SELECT COUNT(*) FROM board_posts p WHERE {' AND '.join(where)}", params
    ).fetchone()[0]

    # 공지는 항상 상단 고정
    notices = db.execute('''
        SELECT p.*, u.name as author_name,
               (SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.id) as comment_count,
               (SELECT COUNT(*) FROM board_attachments a WHERE a.post_id=p.id) as attach_count
        FROM board_posts p
        JOIN users u ON p.author_id = u.id
        WHERE p.category='notice'
        ORDER BY p.created_at DESC
    ''').fetchall()

    posts = db.execute(f'''
        SELECT p.*, u.name as author_name,
               (SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.id) as comment_count,
               (SELECT COUNT(*) FROM board_attachments a WHERE a.post_id=p.id) as attach_count
        FROM board_posts p
        JOIN users u ON p.author_id = u.id
        WHERE {' AND '.join(where)}
          AND p.category != 'notice'
        ORDER BY p.created_at DESC
        LIMIT ? OFFSET ?
    ''', params + [per_page, (page - 1) * per_page]).fetchall()

    total_pages = (total + per_page - 1) // per_page

    db.close()
    return render_template('board/list.html',
                           notices=notices,
                           posts=posts,
                           category=category,
                           keyword=keyword,
                           page=page,
                           total=total,
                           total_pages=total_pages)


# ── 게시글 상세 ───────────────────────────────────────
@board_bp.route('/post/<int:post_id>')
@login_required
def view_post(post_id):
    db = get_db()

    # 조회수 증가
    db.execute("UPDATE board_posts SET view_count=view_count+1 WHERE id=?", (post_id,))
    db.commit()

    post = db.execute('''
        SELECT p.*, u.name as author_name
        FROM board_posts p
        JOIN users u ON p.author_id = u.id
        WHERE p.id=?
    ''', (post_id,)).fetchone()

    if not post:
        flash('게시글을 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('board.list_posts'))

    comments = db.execute('''
        SELECT c.*, u.name as author_name
        FROM board_comments c
        JOIN users u ON c.author_id = u.id
        WHERE c.post_id=?
        ORDER BY c.created_at ASC
    ''', (post_id,)).fetchall()

    attachments = db.execute(
        "SELECT * FROM board_attachments WHERE post_id=? ORDER BY id",
        (post_id,)
    ).fetchall()

    # 이전/다음 글
    prev_post = db.execute(
        "SELECT id, title FROM board_posts WHERE id < ? ORDER BY id DESC LIMIT 1",
        (post_id,)
    ).fetchone()
    next_post = db.execute(
        "SELECT id, title FROM board_posts WHERE id > ? ORDER BY id ASC LIMIT 1",
        (post_id,)
    ).fetchone()

    db.close()
    return render_template('board/detail.html',
                           post=post,
                           comments=comments,
                           attachments=attachments,
                           prev_post=prev_post,
                           next_post=next_post,
                           file_size_str=file_size_str)


# ── 게시글 작성 ───────────────────────────────────────
@board_bp.route('/write', methods=['GET', 'POST'])
@login_required
def write_post():
    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        content  = request.form.get('content', '').strip()
        category = request.form.get('category', 'general')

        # 공지는 관리자만
        if category == 'notice' and session.get('role') != 'admin':
            category = 'general'

        if not title:
            flash('제목을 입력해주세요.', 'error')
            return render_template('board/form.html', action='write', post=None)
        if not content:
            flash('내용을 입력해주세요.', 'error')
            return render_template('board/form.html', action='write', post=None)

        db = get_db()
        db.execute('''
            INSERT INTO board_posts (category, title, content, author_id)
            VALUES (?, ?, ?, ?)
        ''', (category, title, content, session['user_id']))
        post_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 파일 첨부 처리
        files = request.files.getlist('files')
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                content_bytes = file.read()
                if len(content_bytes) > MAX_FILE_SIZE:
                    flash(f'파일 크기 초과 (최대 20MB): {file.filename}', 'error')
                    continue
                ext       = file.filename.rsplit('.', 1)[1].lower()
                saved     = f"{uuid.uuid4().hex}.{ext}"
                save_path = os.path.join(UPLOAD_DIR, saved)
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                with open(save_path, 'wb') as f:
                    f.write(content_bytes)
                db.execute('''
                    INSERT INTO board_attachments (post_id, filename, saved_name, file_size)
                    VALUES (?, ?, ?, ?)
                ''', (post_id, file.filename, saved, len(content_bytes)))

        db.commit()
        db.close()
        flash('✅ 게시글이 등록되었습니다.', 'success')
        return redirect(url_for('board.view_post', post_id=post_id))

    return render_template('board/form.html', action='write', post=None)


# ── 게시글 수정 ───────────────────────────────────────
@board_bp.route('/post/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    db = get_db()
    post = db.execute("SELECT * FROM board_posts WHERE id=?", (post_id,)).fetchone()

    if not post:
        flash('게시글을 찾을 수 없습니다.', 'error')
        db.close()
        return redirect(url_for('board.list_posts'))

    # 작성자 또는 관리자만 수정 가능
    if post['author_id'] != session['user_id'] and session.get('role') != 'admin':
        flash('수정 권한이 없습니다.', 'error')
        db.close()
        return redirect(url_for('board.view_post', post_id=post_id))

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        content  = request.form.get('content', '').strip()
        category = request.form.get('category', 'general')

        if category == 'notice' and session.get('role') != 'admin':
            category = 'general'

        if not title or not content:
            flash('제목과 내용을 입력해주세요.', 'error')
            return render_template('board/form.html', action='edit', post=post)

        db.execute('''
            UPDATE board_posts
            SET title=?, content=?, category=?,
                updated_at=datetime('now','localtime')
            WHERE id=?
        ''', (title, content, category, post_id))

        # 기존 첨부파일 삭제 요청 처리
        delete_ids = request.form.getlist('delete_attach')
        for aid in delete_ids:
            att = db.execute(
                "SELECT saved_name FROM board_attachments WHERE id=?", (aid,)
            ).fetchone()
            if att:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, att['saved_name']))
                except Exception:
                    pass
                db.execute("DELETE FROM board_attachments WHERE id=?", (aid,))

        # 새 파일 추가
        files = request.files.getlist('files')
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                content_bytes = file.read()
                if len(content_bytes) > MAX_FILE_SIZE:
                    continue
                ext       = file.filename.rsplit('.', 1)[1].lower()
                saved     = f"{uuid.uuid4().hex}.{ext}"
                save_path = os.path.join(UPLOAD_DIR, saved)
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                with open(save_path, 'wb') as f:
                    f.write(content_bytes)
                db.execute('''
                    INSERT INTO board_attachments (post_id, filename, saved_name, file_size)
                    VALUES (?, ?, ?, ?)
                ''', (post_id, file.filename, saved, len(content_bytes)))

        db.commit()
        db.close()
        flash('✅ 게시글이 수정되었습니다.', 'success')
        return redirect(url_for('board.view_post', post_id=post_id))

    attachments = db.execute(
        "SELECT * FROM board_attachments WHERE post_id=?", (post_id,)
    ).fetchall()
    db.close()
    return render_template('board/form.html', action='edit',
                           post=post, attachments=attachments)


# ── 게시글 삭제 ───────────────────────────────────────
@board_bp.route('/post/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    db = get_db()
    post = db.execute("SELECT * FROM board_posts WHERE id=?", (post_id,)).fetchone()

    if not post:
        db.close()
        return redirect(url_for('board.list_posts'))

    if post['author_id'] != session['user_id'] and session.get('role') != 'admin':
        flash('삭제 권한이 없습니다.', 'error')
        db.close()
        return redirect(url_for('board.view_post', post_id=post_id))

    # 첨부파일 삭제
    atts = db.execute(
        "SELECT saved_name FROM board_attachments WHERE post_id=?", (post_id,)
    ).fetchall()
    for att in atts:
        try:
            os.remove(os.path.join(UPLOAD_DIR, att['saved_name']))
        except Exception:
            pass

    db.execute("DELETE FROM board_attachments WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM board_comments WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM board_posts WHERE id=?", (post_id,))
    db.commit()
    db.close()
    flash('🗑️ 게시글이 삭제되었습니다.', 'success')
    return redirect(url_for('board.list_posts'))


# ── 댓글 작성 ─────────────────────────────────────────
@board_bp.route('/post/<int:post_id>/comment', methods=['POST'])
@login_required
def add_comment(post_id):
    content = request.form.get('content', '').strip()
    if not content:
        flash('댓글 내용을 입력해주세요.', 'error')
        return redirect(url_for('board.view_post', post_id=post_id))

    db = get_db()
    db.execute('''
        INSERT INTO board_comments (post_id, author_id, content)
        VALUES (?, ?, ?)
    ''', (post_id, session['user_id'], content))
    db.commit()
    db.close()
    return redirect(url_for('board.view_post', post_id=post_id) + '#comments')


# ── 댓글 삭제 ─────────────────────────────────────────
@board_bp.route('/comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def delete_comment(comment_id):
    db = get_db()
    comment = db.execute(
        "SELECT * FROM board_comments WHERE id=?", (comment_id,)
    ).fetchone()

    if comment and (comment['author_id'] == session['user_id']
                    or session.get('role') == 'admin'):
        post_id = comment['post_id']
        db.execute("DELETE FROM board_comments WHERE id=?", (comment_id,))
        db.commit()
        db.close()
        return redirect(url_for('board.view_post', post_id=post_id) + '#comments')

    db.close()
    flash('삭제 권한이 없습니다.', 'error')
    return redirect(url_for('board.list_posts'))


# ── 파일 다운로드 ─────────────────────────────────────
@board_bp.route('/download/<int:attach_id>')
@login_required
def download_file(attach_id):
    db = get_db()
    att = db.execute(
        "SELECT * FROM board_attachments WHERE id=?", (attach_id,)
    ).fetchone()
    db.close()

    if not att:
        abort(404)

    return send_from_directory(
        UPLOAD_DIR,
        att['saved_name'],
        as_attachment=True,
        download_name=att['filename']
    )
