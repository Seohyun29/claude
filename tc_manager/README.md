# 수행방법
py app.py

## batch(run.bat) 파일

------------------------------


# claud prompt
- soc qa 업무수행, 보안환경(제안된 권한), 6명, 2곳(협업), 젠킨스, 지라 사용, 업무프로세스 설명, 레포트 출력 등..
단계별 진행


# TC Manager - 설치 및 실행 가이드

## 📦 필요한 것
- Python
---

## 1단계: 라이브러리 설치

명령 프롬프트(CMD)를 열고 아래 명령어 입력:

```
pip install flask
```

---

## 2단계: 폴더 위치 확인

tc_manager 폴더 구조:
```
tc_manager/
  app.py          ← 이걸 실행함
  config.py       ← 설정 파일
  database.py     ← DB 자동 생성
  routes/
  templates/
  static/
  uploads/
```

---

## 3단계: 실행

CMD에서 tc_manager 폴더로 이동 후:

```
python app.py
```

아래처럼 나오면 성공:
```
✅ DB 초기화 완료
TC Manager 시작!
접속 주소: http://[이 PC의 IP주소]:5000
```

---

## 4단계: 접속

### 관리자 PC (이 PC)에서:
```
http://localhost:5000
```

### 다른 팀원 PC에서:
```
http://[관리자 PC의 IP]:5000
```

> IP 확인 방법: CMD에서 `ipconfig` 입력 → IPv4 주소 확인

---

## 초기 계정

처음 실행 시 자동으로 "관리자" 계정이 생성됩니다.
사용자 선택 화면에서 [관리자]를 클릭하면 관리자 기능을 사용할 수 있습니다.

---

## Jira/Jenkins 연동 설정 (선택, 나중에)
### jenkins aiworkx_user jenkins token : 11f4e6047410f651a61678d328d1266ba0
---

## 개발 단계 계획

| 단계 | 기능 | 상태 |
|------|------|------|
| 1단계 | 사용자 관리 | ✅ 완료 |
| 2단계 | 프로젝트/장비 관리 | 🔜 다음 |
| 3단계 | TC 리뷰 (엑셀 업로드·상태변경) | 예정 |
| 4단계 | 대시보드·현황 | 예정 |
| 5단계 | Jira/Jenkins 연동 | 예정 |
| 6단계 | PDF/Excel Export | 예정 |
| 7단계 | IT 검증, CI스크립트, 게시판 | 추후 |

---

## 문제 해결

**한글 깨짐:**
CMD 창에서 `chcp 65001` 입력 후 다시 실행

**DB 초기화 하고 싶을 때:**
tc_manager.db 파일 삭제 후 재실행 (데이터 모두 삭제됨, 주의!)
