import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'tc_manager.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # 사용자 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'member',
        location TEXT NOT NULL DEFAULT 'DSR',
        is_active INTEGER NOT NULL DEFAULT 1,
        use_password INTEGER NOT NULL DEFAULT 0,  -- 비밀번호 사용 여부
        password TEXT,                             -- 비밀번호 (평문, 간단 보안)
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # users 마이그레이션
    user_cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    for col, col_type in [
        ('use_password', 'INTEGER NOT NULL DEFAULT 0'),
        ('password',     'TEXT'),
    ]:
        if col not in user_cols:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")

    # 프로젝트 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # 테스트 장비/보드 테이블 (프로젝트별)
    c.execute('''CREATE TABLE IF NOT EXISTS boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        version TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (project_id) REFERENCES projects(id),
        UNIQUE(project_id, name)
    )''')

    # TC 리뷰 데이터 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS tc_review (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        target_project TEXT,
        tc_id TEXT,
        sub_system TEXT,
        ip TEXT,
        title TEXT,
        domain TEXT,
        sitl_id INTEGER,
        ci_test_automated TEXT,
        ci_script_file_name TEXT,
        alm_status TEXT,
        overall_status TEXT DEFAULT 'Draft',
        uploaded_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )''')

    # 기존 DB 마이그레이션 (컬럼이 없으면 추가)
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(tc_review)").fetchall()]
    for col, col_type in [
        ('target_project',      'TEXT'),
        ('ci_script_file_name', 'TEXT'),
        ('alm_status',          'TEXT'),
        ('tc_url',              'TEXT'),   # TC ID 하이퍼링크 (Polarion ALM URL)
    ]:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE tc_review ADD COLUMN {col} {col_type}")

    # sitl_id 컬럼 타입 마이그레이션: TEXT → INTEGER (기존 데이터 보존)
    col_info = {row[1]: row[2] for row in c.execute("PRAGMA table_info(tc_review)").fetchall()}
    if col_info.get('sitl_id', '').upper() != 'INTEGER':
        try:
            c.execute("ALTER TABLE tc_review RENAME TO tc_review_old")
            c.execute('''CREATE TABLE tc_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                target_project TEXT,
                tc_id TEXT,
                sub_system TEXT,
                ip TEXT,
                title TEXT,
                domain TEXT,
                sitl_id INTEGER,
                ci_test_automated TEXT,
                ci_script_file_name TEXT,
                alm_status TEXT,
                overall_status TEXT DEFAULT 'Draft',
                tc_url TEXT,
                uploaded_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )''')
            c.execute('''INSERT INTO tc_review
                SELECT id, project_id, target_project, tc_id, sub_system, ip, title, domain,
                       CAST(sitl_id AS INTEGER), ci_test_automated, ci_script_file_name,
                       alm_status, overall_status, tc_url, uploaded_at
                FROM tc_review_old''')
            c.execute("DROP TABLE tc_review_old")
        except Exception:
            pass  # 마이그레이션 실패 시 기존 유지

    # TC별 보드별 리뷰 상태 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS review_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tc_id INTEGER NOT NULL,
        board_id INTEGER NOT NULL,
        reviewer_id INTEGER,
        status TEXT DEFAULT 'Draft',  -- Draft, In Review, Reviewed, BL in Review
        comment TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (tc_id) REFERENCES tc_review(id),
        FOREIGN KEY (board_id) REFERENCES boards(id),
        FOREIGN KEY (reviewer_id) REFERENCES users(id),
        UNIQUE(tc_id, board_id)
    )''')

    # IT 일정 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS it_schedule (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        location       TEXT NOT NULL,
        project_id     INTEGER,
        board_id       INTEGER,
        scheduled_date TEXT,
        jenkins_job    TEXT,           -- Jenkins Job 경로
        status         TEXT DEFAULT 'pending',
        notes          TEXT,
        created_at     TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (project_id) REFERENCES projects(id),
        FOREIGN KEY (board_id)   REFERENCES boards(id)
    )''')

    # it_schedule 마이그레이션
    it_cols = [r[1] for r in c.execute("PRAGMA table_info(it_schedule)").fetchall()]
    if 'jenkins_job' not in it_cols:
        c.execute("ALTER TABLE it_schedule ADD COLUMN jenkins_job TEXT")

    # Jira 연동 설정
    c.execute('''CREATE TABLE IF NOT EXISTS jira_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_url TEXT,
        project_key TEXT,
        token TEXT,
        email TEXT,
        is_enabled INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # Jenkins 연동 설정
    c.execute('''CREATE TABLE IF NOT EXISTS jenkins_config (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        server_url  TEXT,
        token       TEXT,
        username    TEXT,
        job_filters TEXT,   -- 관심 Job 경로 목록 (줄바꿈 구분)
        is_enabled  INTEGER DEFAULT 0,
        updated_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # jenkins_config 마이그레이션
    jk_cols = [r[1] for r in c.execute("PRAGMA table_info(jenkins_config)").fetchall()]
    if 'job_filters' not in jk_cols:
        c.execute("ALTER TABLE jenkins_config ADD COLUMN job_filters TEXT")

    # 게시판 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS board_posts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        category    TEXT NOT NULL DEFAULT 'general', -- 'notice' or 'general'
        title       TEXT NOT NULL,
        content     TEXT NOT NULL,
        author_id   INTEGER NOT NULL,
        view_count  INTEGER DEFAULT 0,
        is_pinned   INTEGER DEFAULT 0,  -- 공지 상단 고정
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        updated_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (author_id) REFERENCES users(id)
    )''')

    # 댓글 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS board_comments (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id   INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        content   TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (post_id)   REFERENCES board_posts(id),
        FOREIGN KEY (author_id) REFERENCES users(id)
    )''')

    # 첨부파일 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS board_attachments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id     INTEGER NOT NULL,
        filename    TEXT NOT NULL,   -- 화면 표시용 원본 파일명
        saved_name  TEXT NOT NULL,   -- 서버 저장 파일명 (충돌 방지)
        file_size   INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (post_id) REFERENCES board_posts(id)
    )''')

    # 초기 관리자 계정 (없으면 생성)
    c.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO users (name, role, location) VALUES ('관리자', 'admin', 'DSR')")

    conn.commit()
    conn.close()
    print("✅ DB 초기화 완료:", DB_PATH)
