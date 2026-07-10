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
    it_schedule 테이블은 반복 일정을 레코드 하나로만 저장하고,
    화면에 보여줄 때 repeat_type 으로 날짜를 펼친다. 그래서 오늘 일정 여부는
    아래 두 경우를 모두 확인해야 한다.
        1) 단일 일정  : scheduled_date == 오늘
        2) 반복 일정  : scheduled_date <= 오늘 <= repeat_end 이고,
                        (오늘 - 시작일) 일수가 반복 주기(매주 7일 / 격주 14일)로 나누어떨어질 때

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
#  오늘 IT 일정이 있는지 확인
# ──────────────────────────────────────────
def has_it_schedule_today(today=None):
    """
    오늘 날짜에 IT 일정이 하나라도 있으면 True, 없으면 False.
    반복 일정(매주/격주)도 오늘에 해당하는지 계산해서 포함한다.
    """
    if today is None:
        today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')

    db = get_db()
    try:
        # [1] 단일 일정: 오늘 날짜와 정확히 일치
        row = db.execute('''
            SELECT COUNT(*) AS c FROM it_schedule
            WHERE scheduled_date = ?
              AND (repeat_type IS NULL OR repeat_type = 'none')
        ''', (today_str,)).fetchone()
        if row and row['c'] > 0:
            return True

        # [2] 반복 일정: 시작일 <= 오늘 <= 종료일 인 것들만 가져와서
        #     오늘이 반복 주기에 맞는 날인지 직접 계산
        repeats = db.execute('''
            SELECT scheduled_date, repeat_type, repeat_end FROM it_schedule
            WHERE repeat_type IN ('weekly', 'biweekly')
              AND repeat_end IS NOT NULL
              AND scheduled_date <= ?
              AND repeat_end     >= ?
        ''', (today_str, today_str)).fetchall()

        for r in repeats:
            start = datetime.date.fromisoformat(r['scheduled_date'])
            step  = 7 if r['repeat_type'] == 'weekly' else 14   # 매주 / 격주
            diff  = (today - start).days
            if diff >= 0 and diff % step == 0:
                return True

        return False
    finally:
        db.close()


# ──────────────────────────────────────────
#  스케줄러가 실행할 작업
# ──────────────────────────────────────────
def scheduled_job():
    """매일 아침 8시에 트리거되는 작업 (오늘 IT 일정이 있을 때만 실행)"""
    today = datetime.date.today()
    print(f"[스케줄러] {today} 자동 실행 확인 중...")

    # 오늘 IT 일정이 없으면 아무것도 하지 않음
    try:
        if not has_it_schedule_today(today):
            print("오늘은 IT 일정이 없습니다")
            return
    except Exception as e:
        print(f"[스케줄러] 일정 확인 중 오류: {e}")
        return

    print("[스케줄러] 오늘 IT 일정이 있습니다. 자동 실행 시작")
    try:
        result = get_binary()
        print(f"[스케줄러] 완료: {result}")
    except Exception as e:
        print(f"[스케줄러] 실행 중 오류: {e}")


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
