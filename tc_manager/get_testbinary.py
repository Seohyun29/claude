"""
테스트 바이너리 다운로드 + Gerrit 태그 검증 통합 스크립트

[기능 1] is_tested_binary()
  Gerrit SSH로 기준 태그가 가리키는 커밋에 '다른 태그'가 더 있는지 확인.
  다른 태그가 더 있으면 True, 기준 태그 하나뿐이면 False.

[기능 2] get_binary()
  CICD API에서 테스트 파이프라인 정보를 받아, test_status가 FAIL/PASS인
  첫 항목의 바이너리를 WebDAV로 (하위 폴더까지 재귀) 다운로드.

============================================================
[사전 준비] Gerrit SSH 키 생성 및 등록 (PC마다 최초 1회만)
  - is_tested_binary() 사용 시 필요
============================================================
1) ssh-keygen -t ed25519 -C "사용자명"   (전부 엔터)
2) cat ~/.ssh/id_ed25519.pub                                   (전체 복사)
3) Gerrit 웹 > Settings > SSH Keys > 붙여넣기 > Add New SSH Key
4) ssh -p 29414 <USERNAME>@10.166.211.148 gerrit version
※ 개인키는 실행 PC의 ~/.ssh 에 있어야 함.
============================================================
"""

import requests
import json
import os
import re
import subprocess
import urllib3
from urllib.parse import urljoin, unquote, urlparse
from xml.etree import ElementTree

# SSL 인증서 검증 없이(verify=False) 요청하므로 관련 경고를 끈다.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 설정: CICD / WebDAV ==========
BASE_URL = "https://automotive-cicd.samsungds.net:3090"
# 인증이 필요한 경우 아래 설정
CICD_COOKIE = ""       # 브라우저 쿠키값 (필요시)
WEBDAV_USER = "share"  # WebDAV 사용자명
WEBDAV_PASS = "share"  # WebDAV 패스워드

# ========== 설정: Gerrit SSH (태그 검증) ==========
GERRIT_HOST = "10.166.211.148"
GERRIT_SSH_PORT = 29414
USERNAME = ""
REPO_PATH = ""  # 태깅 Repo

BRANCH_NAME = ""  # 브랜치명(전체)
BRANCH_TAG_STRIP = "exynosauto9_"  # 태그명 조합 시 브랜치명 맨 앞에서 제거할 접두어
# ==========================================

REMOTE = f"ssh://{USERNAME}@{GERRIT_HOST}:{GERRIT_SSH_PORT}/{REPO_PATH}"


# =====================================================================
#  기능 1) Gerrit 태그 검증 (같은 커밋에 다른 태그가 있는지)
# =====================================================================

def branch_to_tag_suffix(branch):
    """브랜치명 맨 앞 exynosauto9_ 제거 -> 태그 접미사"""
    if BRANCH_TAG_STRIP and branch.startswith(BRANCH_TAG_STRIP):
        return branch[len(BRANCH_TAG_STRIP):]
    return branch

def get_commit_of_tag(tag):
    """기준 태그 하나가 가리키는 실제 커밋 SHA 조회.
    annotated 태그면 ^{} 참조(실제 커밋)를 우선 사용.
    태그만 지정해 조회하므로 전체 태그를 받지 않아 빠름.
    """
    cmd = ["git", "ls-remote", REMOTE, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"[오류] {r.stderr.strip()}")
        return None

    plain_sha = None
    peeled_sha = None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if ref.endswith("^{}"):
            peeled_sha = sha
        else:
            plain_sha = sha
    return peeled_sha or plain_sha

def get_all_tag_commit_map():
    """전체 태그 -> 커밋 SHA 매핑 (커밋 기준으로 다른 태그를 찾기 위해 필요).
    annotated 태그는 ^{}(실제 커밋) 우선.

    [왜 전체 태그를 다시 받아서 비교하나? — 나중에 또 궁금해할까봐 기록]
    get_commit_of_tag() 로 기준 태그의 커밋 SHA는 이미 알아냈다.
    그런데 "그 SHA를 가리키는 '다른' 태그가 있는지" 확인하려면,
    결국 다른 태그들이 각각 어떤 커밋을 가리키는지 알아야 SHA를 비교할 수 있다.

    문제는 git ls-remote 가 'ref 이름(패턴)'으로만 서버 필터링이 되고,
    '커밋 SHA가 이것인 ref만 줘' 같은 역방향 조회는 지원하지 않는다는 점이다.
    (git 프로토콜은 태그->커밋 방향으로만 조회 가능. 커밋->태그는 불가)
    그래서 전체 태그의 커밋 매핑을 받아 클라이언트에서 뒤집어 비교하는 구조가 됐다.

    즉 성능 때문이 아니라 git ls-remote 의 한계 때문이다.
    태그 수가 아주 많지 않으면 이 방식으로 충분히 빠르다.
    만약 커밋->태그를 서버에서 바로 하고 싶다면 Gerrit 전용 SSH 명령
    (예: ssh -p PORT user@host gerrit query commit:<SHA>)을 써야 한다.
    """
    cmd = ["git", "ls-remote", "--tags", REMOTE]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print(f"[오류] 태그 목록 조회 실패: {r.stderr.strip()}")
        return {}

    peeled, plain = {}, {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        m = re.match(r"refs/tags/(.+)", ref)
        if not m:
            continue
        name = m.group(1)
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            plain[name] = sha

    tag_commit = {}
    for name, sha in plain.items():
        tag_commit[name] = peeled.get(name, sha)
    return tag_commit

def is_tested_binary(prefix, branch=BRANCH_NAME):
    """기준 태그가 가리키는 커밋에 '다른 태그'가 더 있으면 True, 아니면 False.

    prefix : IR<날짜>_<시간> 부분 (예: IR260707_125629)
    branch : 브랜치명(전체). 기본값은 설정의 BRANCH_NAME.

    다른 태그가 더 있다 = 이미 다른 시점/조합으로 태깅(=테스트)된 바이너리로 볼 수 있음.
    태그가 기준 태그 하나뿐 = 아직 다른 태그가 없음.
    """
    tag_suffix = branch_to_tag_suffix(branch)
    target_tag = f"{prefix}_{tag_suffix}"

    print("=" * 60)
    print(f"  기준 태그: {target_tag}")
    print(f"  레포     : {REPO_PATH}")
    print("=" * 60)

    # 1) 기준 태그가 가리키는 커밋 SHA 조회
    target_sha = get_commit_of_tag(target_tag)
    if not target_sha:
        print(f"\n❌ 태그 '{target_tag}' 을 찾을 수 없어요. prefix/브랜치명을 확인해 주세요.")
        return None
    print(f"\n  '{target_tag}' 이 가리키는 커밋: {target_sha}")

    # 2) 같은 커밋을 가리키는 다른 태그 찾기
    tag_commit = get_all_tag_commit_map()
    same_commit = [t for t, sha in tag_commit.items()
                   if sha == target_sha and t != target_tag]

    print("\n" + "=" * 60)
    print("  같은 커밋에 달린 다른 태그")
    print("=" * 60)
    if same_commit:
        print(f"\n  ⚠️ 같은 커밋({target_sha[:10]})에 다른 태그 {len(same_commit)}개가 더 있어요:")
        for t in sorted(same_commit):
            print(f"     - {t}")
        return True
    else:
        print(f"\n  ✅ 이 커밋에는 '{target_tag}' 태그 하나만 달려 있어요.")
        return False


# =====================================================================
#  기능 2) CICD 테스트 바이너리 다운로드
# =====================================================================

def get_headers():
    headers = {"Content-Type": "application/json"}
    if CICD_COOKIE:
        headers["Cookie"] = CICD_COOKIE
    return headers

def get_testpipeline(project):
    url = f"{BASE_URL}{'/api/bundle/get_request/'}{project}{'/TEST-PIPELINE/none'}"
    print(f"\n[GET] {url}")
    try:
        r = requests.get(url, headers=get_headers(), verify=False, timeout=10)

        print(f"  Status : {r.status_code}")
        try:
            data = r.json()
            # print(f"  Response:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
            print(f"Response: data[0] \n{data[0]}")
            return data
        except:
            print(f"  Response (text): {r.text[:500]}")
            return None
    except requests.exceptions.ConnectionError as e:
        print(f"  [연결 오류] {e}")
    except Exception as e:
        print(f"  [오류] {e}")
    return None

def list_webdav_entries(webdav_dir_url):
    """WebDAV 디렉토리(PROPFIND, Depth:1)에서 바로 아래 항목 조회.
    반환: (files, subdirs)
      files   = 파일 이름 리스트
      subdirs = 하위 폴더 이름 리스트
    """
    headers = {"Depth": "1", "Content-Type": "application/xml"}
    r = requests.request(
        "PROPFIND", webdav_dir_url,
        auth=(WEBDAV_USER, WEBDAV_PASS),
        headers=headers, verify=False, timeout=30
    )
    if r.status_code not in (207, 200):
        print(f"  [WebDAV 목록 오류] Status {r.status_code}")
        print(f"  {r.text[:300]}")
        return [], []

    files, subdirs = [], []
    # 요청한 디렉토리의 경로 부분(호스트 제외)을 정규화해서 자기 자신 판별에 사용
    self_path = urlparse(webdav_dir_url).path.rstrip("/")
    try:
        tree = ElementTree.fromstring(r.content)
        for resp in tree.iter("{DAV:}response"):
            href_el = resp.find("{DAV:}href")
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text)
            href_path = urlparse(href).path.rstrip("/") if "://" in href else href.rstrip("/")

            # 요청한 디렉토리 자기 자신은 제외 (무한재귀 방지)
            if href_path == self_path:
                continue

            # 폴더 여부: resourcetype 안에 <collection/> 있으면 폴더
            is_dir = resp.find(".//{DAV:}collection") is not None
            name = os.path.basename(href_path)
            if not name:
                continue

            if is_dir:
                subdirs.append(name)
            else:
                files.append(name)
    except Exception as e:
        print(f"  [XML 파싱 오류] {e}")
        return [], []

    return files, subdirs

def download_webdav_recursive(webdav_dir_url, local_dir):
    """WebDAV 디렉토리를 하위 폴더까지 재귀적으로 전부 다운로드.
    webdav_dir_url: 현재 조회할 원격 디렉토리 (끝에 / 포함)
    local_dir     : 저장할 로컬 폴더
    반환: 다운로드 성공한 파일 수
    """
    os.makedirs(local_dir, exist_ok=True)
    files, subdirs = list_webdav_entries(webdav_dir_url)

    count = 0
    # 1) 현재 폴더의 파일들 다운로드
    for fname in files:
        file_url = urljoin(webdav_dir_url, fname)
        save_path = os.path.join(local_dir, fname)
        if download_webdav_file(file_url, save_path):
            count += 1

    # 2) 하위 폴더로 재귀
    for sub in subdirs:
        sub_url = urljoin(webdav_dir_url, sub + "/")
        sub_local = os.path.join(local_dir, sub)
        print(f"  [폴더] {sub}/ 진입")
        count += download_webdav_recursive(sub_url, sub_local)

    return count

def download_webdav_file(file_url, save_path):
    """WebDAV 파일 하나를 다운로드해서 저장"""
    r = requests.get(
        file_url, auth=(WEBDAV_USER, WEBDAV_PASS),
        verify=False, timeout=120, stream=True
    )
    if r.status_code != 200:
        print(f"    [실패] {os.path.basename(save_path)} (Status {r.status_code})")
        return False
    with open(save_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    print(f"    [완료] {os.path.basename(save_path)}")
    return True

def get_binary(project, board):
    # 1) 테스트 파이프라인 정보 획득 후, test_status가 FAIL/PASS인 첫 항목 선택
    data = get_testpipeline(project)
    if not data:
        print("  [중단] 파이프라인 데이터를 받지 못했어요.")
        return None

    selected = None
    for item in data:
        if item.get("test_status") in ("FAIL", "PASS"):
            selected = item
            break

    if selected is None:
        print("  [중단] test_status가 FAIL 또는 PASS인 항목이 없어요.")
        print(f"  test_status 목록: {[d.get('test_status') for d in data]}")
        return None

    request_run_id = selected["request_run_id"]
    print(f"\n  선택된 test_status: {selected.get('test_status')}")
    print(f"  request_run_id: {request_run_id}")

    # 1-1) 이미 테스트된 바이너리인지 확인 (같은 커밋에 다른 태그가 있는지)
    #  True  = 다른 태그가 더 있음 → 이미 테스트됨
    #  False = 해당 태그 하나만 달림 → 다운로드 진행
    #  None  = 태그 조회 실패 등 오류 → 다운로드하지 않고 중단
    prefix = selected["tag"]
    tested = is_tested_binary(prefix)
    if tested is True:
        print("\n이미 테스트되었습니다.")
        return None
    if tested is None:
        print("\n[중단] 태그 확인에 실패해 다운로드를 진행하지 않아요.")
        return None
    # 여기까지 왔으면 tested is False → 다운로드 진행

    # 2) detail/status 로 바이너리(웹다브) 상세 정보 조회
    url = f"{BASE_URL}{'/api/detail/status/'}{project}{'/test-pipeline/'}{request_run_id}"
    print(f"\n[GET] {url}")
    try:
        r = requests.get(url, headers=get_headers(), verify=False, timeout=10)
        print(f"  Status : {r.status_code}")
        detail = r.json()
        print(f"Response: detail[0] \n{detail[0]}")
    except Exception as e:
        print(f"  [오류] {e}")
        return None

    # 3) board 규칙에 맞는 바이너리를 detail에서 찾아 다운로드
    #    (V920은 두 개, 그 외는 한 개)
    names = build_project_binaries(project, board)
    if not names:
        print(f"  [오류] 알 수 없는 board 값: {board} (V720/V820/V920 중 하나여야 함)")
        return []

    print(f"\n  board={board} → 찾을 PROJECT_BINARY: {names}")

    saved = []
    for name in names:
        # detail 목록에서 project 값이 name과 일치하는 항목 찾기
        target = None
        for item in detail:
            if item.get("project") == name:
                target = item
                break

        if target is None:
            print(f"  [경고] project == '{name}' 인 항목을 찾지 못했어요. 건너뜁니다.")
            print(f"  detail 내 project 목록: {[d.get('project') for d in detail]}")
            continue

        print(f"\n  === '{name}' 다운로드 ===")
        result = download_binary(target)
        if result:
            saved.append(result)

    print(f"\n  ✅ 다운로드된 폴더 {len(saved)}개: {saved}")
    return saved


def build_project_binaries(project, board):
    p = project.lower()
    mapping = {
        "V720": [f"{p}_la_720"],
        "V820": [f"{p}_la_820"],
        "V920": [f"{p}_bl_evt2", f"{p}_la_evt2"],
    }
    return mapping.get(board, [])

def download_binary(target):
    """넘겨받은 target(detail 항목 하나)의 WebDAV 경로에서
    파일들을 (하위 폴더 포함) 다운로드한다.
    반환: 저장 폴더 경로 (실패 시 None)
    """
    # 1) WebDAV 경로 조합 (file_link + '/' + project_image_path)
    file_link = target["file_link"]
    image_path = target["project_image_path"]
    # 사이에 슬래시가 없으면 넣고, 양쪽 다 있으면 중복 제거
    webdav_dir_url = file_link.rstrip("/") + "/" + image_path.lstrip("/")
    # 끝에 / 가 없으면 붙여줌 (디렉토리 조회용)
    if not webdav_dir_url.endswith("/"):
        webdav_dir_url += "/"
    print(f"\n  WebDAV 디렉토리: {webdav_dir_url}")

    # 2) 저장 폴더 결정: 다섯자리숫자 / project_image_path의 마지막 폴더명
    m = re.search(r"/(\d{5})/", image_path)
    if not m:
        print(f"  [오류] image_path에서 다섯자리 숫자 폴더를 찾지 못했어요: {image_path}")
        return None
    num_dir = m.group(1)
    last_dir = os.path.basename(image_path.rstrip("/"))  # 경로 마지막 폴더명
    save_dir = os.path.join(num_dir, last_dir)
    os.makedirs(save_dir, exist_ok=True)
    print(f"  저장 폴더: ./{save_dir}/")

    # 3) 디렉토리 내 모든 파일/폴더를 재귀적으로 다운로드
    print(f"\n  다운로드 시작 (하위 폴더 포함):")
    success = download_webdav_recursive(webdav_dir_url, save_dir)
    print(f"\n  ✅ 총 {success}개 파일 다운로드 완료 → ./{save_dir}/")
    return save_dir


# =====================================================================
#  실행부
# =====================================================================

if __name__ == "__main__":
    # 앱(QA_TestManager)에서 아래처럼 호출:
    #   binary = get_binary(project, board)
    pass
