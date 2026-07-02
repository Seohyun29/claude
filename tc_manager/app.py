from flask import Flask
from config import SECRET_KEY, DEBUG, HOST, PORT, UPLOAD_FOLDER
from database import init_db
import os

def create_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # DB 초기화
    init_db()

    # ── 추가 모듈 DB 초기화 ──
    from routes.schedule import schedule_bp, init_schedule_db          # 일정 캘린더
    from routes.it_verification import itverify_bp, init_itverify_db   # IT 검증
    init_schedule_db()
    init_itverify_db()

    # 라우트 등록
    from routes.main import main_bp
    from routes.users import users_bp
    from routes.projects import projects_bp
    from routes.review import review_bp
    from routes.export import export_bp
    from routes.integration import integration_bp
    from routes.board import board_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(integration_bp)
    app.register_blueprint(board_bp)
    app.register_blueprint(schedule_bp)     # 일정 캘린더
    app.register_blueprint(itverify_bp)     # IT 검증

    return app


if __name__ == '__main__':
    app = create_app()
    print("=" * 50)
    print("  TC Manager 시작!")
    print(f"  접속 주소: http://[이 PC의 IP주소]:{PORT}")
    print("=" * 50)
    app.run(host=HOST, port=PORT, debug=DEBUG)
