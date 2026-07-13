"""
================================================================================
  QA_TestManager 스케줄러 설정 (daily_trigger.py)
================================================================================

Flask 앱(단일 프로세스)에 넣어서 매일 아침 8시(월~금)에
오늘 IT 일정이 있는지 확인하고, 있을 때만 get_testbinary.get_binary() 를 실행한다.

[동작 규칙]
    · 오늘(월~금 08:00 기준) IT 일정이 하나라도 있으면  → get_binary() 실행
    · 오늘 IT 일정이 없으면                            → "오늘은 IT 일정이 없습니다" 출력 후 실행 안 함

[IT 일정 판단 방법]  ※ 일정 캘린더 모듈(schedule.py)의 저장 방식과 동일
    it_schedule 테이블은 반복 일정도 등록 시점에 날짜별 레코드로 펼쳐 저장한다(B 방식).
    (매주 반복 5주 → 레코드 5개, parent_id 로 묶임)
    그래서 오늘 일정은 scheduled_date = 오늘 로 조회하면 단일·반복 구분 없이 전부 나온다.

[사전 설치]
    pip install APScheduler tzdata
    (tzdata 는 윈도우에서 Asia/Seoul 타임존을 올바로 처리하기 위해 필요)

[사용법] Flask 앱 초기화 부분에서 아래처럼 호출:
    from flask import Flask
    from daily_trigger import init_scheduler

    app = Flask(__name__)
    init_scheduler()
    ...
    # 개발 중 debug 모드로 켤 때는 리로더 때문에 스케줄러가
    # 두 번 뜨는 것을 막기 위해 use_reloader=False 를 권장:
    #   app.run(debug=True, use_reloader=False)
================================================================================
"""

import atexit
import datetime
from apscheduler.schedulers.background import BackgroundScheduler

from get_testbinary import get_binary
from database import get_db

# 개발 중 테스트할 때 True 로 바꾸면 매 분 실행됨 (동작 확인용).
# 확인 후 반드시 False 로 되돌릴 것.
TEST_MODE = False


# ──────────────────────────────────────────
#  오늘 IT 일정 조회 (프로젝트 / 장비 정보 반환)
# ──────────────────────────────────────────
def get_today_schedules(today=None):
    """
    오늘 날짜의 IT 일정 목록을 반환한다.

    ※ 일정 캘린더(schedule.py)는 반복 일정도 등록 시점에 날짜별 레코드로 펼쳐
      저장한다(B 방식). 따라서 오늘 일정은 scheduled_date = 오늘 로 조회하면
      단일·반복 구분 없이 전부 나온다. (반복 주기를 따로 계산할 필요 없음)

    반환: [{'id', 'project', 'board', 'location'}, ...]
          일정이 없으면 빈 리스트 []
    """
    if today is None:
        today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')

    db = get_db()
    try:
        rows = db.execute('''
            SELECT s.id, s.location,
                   p.name AS project_name,
                   b.name AS board_name
            FROM it_schedule s
            LEFT JOIN projects p ON s.project_id = p.id
            LEFT JOIN boards   b ON s.board_id   = b.id
            WHERE s.scheduled_date = ?
            ORDER BY s.location, p.name
        ''', (today_str,)).fetchall()

        return [{
            'id':       r['id'],
            'project':  r['project_name'],
            'board':    r['board_name'],
            'location': r['location'],
        } for r in rows]
    finally:
        db.close()


def has_it_schedule_today(today=None):
    """오늘 IT 일정이 하나라도 있으면 True (단순 확인용)"""
    return len(get_today_schedules(today)) > 0


# ──────────────────────────────────────────
#  스케줄러가 실행할 작업
# ──────────────────────────────────────────
def scheduled_job():
    """매일 아침 8시에 트리거되는 작업 (오늘 IT 일정이 있을 때만 실행)"""
    today = datetime.date.today()
    print(f"[스케줄러] {today} 자동 실행 확인 중...")

    # 오늘 IT 일정 목록 조회 (프로젝트 / 장비 정보 포함)
    try:
        schedules = get_today_schedules(today)
    except Exception as e:
        print(f"[스케줄러] 일정 확인 중 오류: {e}")
        return

    # 오늘 IT 일정이 없으면 아무것도 하지 않음
    if not schedules:
        print("오늘은 IT 일정이 없습니다")
        return

    print(f"[스케줄러] 오늘 IT 일정 {len(schedules)}건 - 자동 실행 시작")

    # 일정 건마다 get_binary(project, board) 실행
    #   예) 테라에 반복 일정 1건 + DSR에 단일 일정 1건 → 총 2번 실행
    success = 0
    for sch in schedules:
        project  = sch['project']
        board    = sch['board']
        location = sch['location']
        print(f"  - {project} / {board} ({location})")
        try:
            result = get_binary(project, board)
            print(f"    완료: {result}")
            success += 1
        except Exception as e:
            print(f"    오류: {e}")

    print(f"[스케줄러] 완료: {len(schedules)}건 중 {success}건 성공")


def init_scheduler():
    """스케줄러를 생성/시작하고 종료 시 정리하도록 등록"""
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    if TEST_MODE:
        # 테스트: 매 분 실행
        scheduler.add_job(scheduled_job, trigger="cron", minute="*")
        print("[스케줄러] TEST_MODE - 매 분 실행으로 등록됨")
    else:
        # 운영: 월~금 08:00 실행
        scheduler.add_job(
            scheduled_job,
            trigger="cron",
            day_of_week="mon-fri",
            hour=8,
            minute=0,
        )
        print("[스케줄러] 월~금 08:00 실행으로 등록됨")

    scheduler.start()

    # 앱 종료 시 스케줄러도 깔끔하게 종료
    atexit.register(lambda: scheduler.shutdown())

    return scheduler


if __name__ == "__main__":
    # 단독 실행 시 스케줄러만 띄워서 동작 확인 (Ctrl+C 로 종료)
    import time
    init_scheduler()
    print("스케줄러 단독 실행 중... (Ctrl+C 로 종료)")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("\n종료합니다.")
