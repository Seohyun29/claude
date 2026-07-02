# =============================================
# TC Manager 설정 파일
# =============================================

import os

# Flask 설정
SECRET_KEY = 'aiworkx-peg1-tc-manager-2026'
DEBUG = True
HOST = '0.0.0.0'   # 팀원들이 네트워크로 접속 가능
PORT = 5000

# 업로드 파일 설정
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB

# =============================================
# Jira 연동 설정
# ※ 토큰 확인 후 아래 값을 채워주세요
# =============================================
JIRA_ENABLED = False          # True로 바꾸면 연동 활성화
JIRA_SERVER_URL = ''          # 예: https://jira.samsung.com
JIRA_EMAIL = ''               # 예: yourname@samsung.com
JIRA_TOKEN = ''               # Jira API 토큰
JIRA_PROJECT_KEY = ''         # 예: ALM

# Jira 상태 매핑 (Jira 상태명 → 우리 시스템 상태명)
JIRA_STATUS_MAP = {
    'Draft': 'Draft',
    'In Review': 'In Review',
    'Reviewed': 'Reviewed',
    'Approved': 'Approved',
}

# =============================================
# Jenkins 연동 설정
# ※ 토큰 확인 후 아래 값을 채워주세요
# =============================================
JENKINS_ENABLED = False       # True로 바꾸면 연동 활성화
JENKINS_SERVER_URL = ''       # 예: https://jenkins.samsung.com
JENKINS_USERNAME = ''         # 예: yourname
JENKINS_TOKEN = ''            # Jenkins API 토큰

# 장소 설정
LOCATIONS = ['DSR', 'Tera']
MAX_IT_PER_LOCATION = 2       # 장소당 최대 IT 일정 수
