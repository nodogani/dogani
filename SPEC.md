# SPEC.md — Dogani 명세

> 코드가 실제로 하는 일의 기준 문서. 동작·API·설정·파일 형식이 바뀌면 같은 작업에서 이 문서도 고친다.
> 사용법(사용자용)은 [README.md](README.md), 결정·미확인 사항은 [CHECKSHEET.md](CHECKSHEET.md), 이력은 [CHANGELOG.md](CHANGELOG.md).

## 1. 개요

- 목적: 옵시디언 데일리 노트(todo)를 바탕화면 오버레이로 띄우고, 사내망 동료에게 실시간(~1초, SSE) 공유
- 형태: Windows 전용 단일 프로그램 `Dogani` = 공유 서버 + 뷰어 + 트레이. 같은 프로그램끼리 HTTP 로 통신
- 공간
  - **개인 공간**: 내 todo 폴더(읽기·쓰기) / 다른 사람 노트(읽기 전용)
  - **공용 공간(팀)**: 팀 호스트 PC 의 공용 노트·공용 일정. 팀원 모두 편집
- 규모: Python ≈ 2.9k줄, JS ≈ 1.0k줄(+ CodeMirror 번들). 자동 테스트 없음

## 2. 구성

| 파일 | 역할 |
|---|---|
| `app/launcher.py` | exe 진입점. `%APPDATA%\Dogani\app` 의 받은 코드 → 실패 시 exe 내장 코드 실행. 받은 `app.new` 적용 |
| `app/dogani.py` | Qt 앱 본체: 설정, 트레이(pystray, Win32 기본 메뉴), 오버레이 창(QWebEngine), JS 브리지, 자동 실행, 이전 설정 이전 |
| `app/server.py` | 공유 서버(ThreadingHTTPServer, 스레드). `NoteStore`(폴더 감시·읽기·쓰기), `Hub`(SSE·접속자·차단), 라우팅 |
| `app/team.py` | 팀 호스트(`TeamHost`: 팀원·초대장·공용 노트), 토큰(`TeamAuth`) |
| `app/schedule.py` | 일정 파일 파서 |
| `app/updater.py` | GitHub 최신 버전 태그 확인(`check`) / 받기(`stage`, 태그 zip 의 `app/` → `app.new`) |
| `app/liveupdate.py` | exe: 실행 중 업데이트 확인만(트레이 표시, 재시작 안 함). 소스 실행: `devreload` 연결 |
| `app/devreload.py` | 개발 모드 파일 감시(ui → 새로고침, .py → 문법 확인 후 재시작) |
| `app/deps.py` | exe 에 넣을 모듈 목록(받은 코드가 쓸 수 있는 import 범위) |
| `app/ui/` | 오버레이 UI: `index.html`, `app.js`(노트·바·공간), `calendar.js`, `markdown.js`, `style.css`, `vendor/editor.js` |
| `app/editor/` | CodeMirror 6 라이브 프리뷰 편집기 소스 → `build.bat`(Node) → `ui/vendor/editor.js`. 배포 zip 제외 |
| `build.bat` | PyInstaller 로 런처 exe 빌드, 기본 코드 `version.json` = 마지막 태그 |

## 3. 데이터 형식

### 3.1 데일리 노트
- 경로: `todo폴더/yyyy.mm/YYMMDD 요일.md` (예 `2026.10/261030 금.md`). 읽을 때는 폴더 안 어디든 파일명으로 인식
- 진행도: 체크박스 `- [ ]` / `- [x]` 개수 → `완료/전체`
- 없는 날짜에서 편집 시작하면 위 경로로 새로 만듦. "어제 노트 복사"는 내용 그대로 복사
- 표시는 옵시디언 라이브 프리뷰를 따름 (커서가 닿은 서식만 원문 기호 노출)

### 3.2 일정 파일
- 파일명: `2026일정.md`, `2026 일정.md`, `일정.md` (`^(\d{4}\s*)?일정\.md$`), todo 폴더 안 어디든
- 한 줄 = 일정 하나: `일시, 제목, #태그…, 장소, 메모` (목록 기호·체크박스 앞머리 허용)
- 일시
  - 날짜 `YYMMDD` / `YYYY-MM-DD`, 기간 `시작-끝`·`시작~끝` (끝은 `YYMMDD`/`MMDD`/`DD`)
  - 시작 미정 `-261119`, 끝 미정 `261119-` (달력 2칸 페이드)
  - 시간 `15:00`, `9:00-12:00`, 끝에 `?` = 시간 미정(○ 점, "쯤")
- 꾸밈: 날짜 `**굵게**` = 중요, `==하이라이트==` = 쉬는 날(날짜 이동에서 건너뜀), `*기울임*` = 기간을 화살표로, `***` = 중요+굵은 화살표
- 태그 여러 개면 첫 태그 색. 제목·장소의 `*`, `**`, `==` 는 지우고 표시

### 3.3 설정 `%APPDATA%\Dogani\dogani.json` (`DOGANI_HOME` 으로 변경)
`name, todo_dir, port(8765), servers[], visible, mode, space, date, pos, panel, windows, on_top, opacity, theme, paused, blocked[], team, team_host, team_only`

### 3.4 팀 호스트 데이터 `%APPDATA%\Dogani\team\`
- `team.json`: `name, sign_key(Ed25519 개인키 hex32, 호스트 밖으로 안 나감), invites{코드: {exp, mid}}, members{id: name, min_iat, 마지막 신호, 주소}` (예전 `code`·`pin` 필드는 로드 시 삭제)`
- `notes/`: 공용 노트(3.1 과 같은 형식) + `일정.md`(처음에 예시로 생성)

## 4. HTTP API

- 공통 헤더: `X-Dogani-Key` 없으면 403 (브라우저 차단). 선택 헤더: PC 이름, 표시 이름, 팀 토큰
- **owner** = 요청이 같은 PC 에서 옴. 쓰기는 개인 공간에선 owner 만, 팀 공간에선 팀원 모두
- 차단된 IP 는 owner 가 아니면 403 `blocked`
- 오류: `{"error": code}` — 400 bad request, 401 not_member, 403 read_only/team_only/blocked, 404, 409 conflict, 413 too_large, 503 paused

| 경로 | 메서드 | 설명 |
|---|---|---|
| `/api/info` | GET | `{app, name, pc, today, kind:"personal", paused, writable}` |
| `/api/events` | GET | SSE: `changed{dates}`, `schedule`, `status`, `reload` |
| `/api/note?date=` | GET/POST | 노트 원문·개수 / `{date, content, base_mtime}` 저장 (mtime 다르면 409) |
| `/api/month?ym=` | GET | 날짜별 완료/전체 |
| `/api/schedule` | GET | 파싱된 일정 목록 |
| `/api/sched-doc` | GET/POST | 일정 파일 원문 / 저장 |
| `/api/team/hello` | GET | 호스트 확인 (인증 전) |
| `/api/team/join` | POST | `{code, name}` → 토큰·공개키(`pub`). `name` = 그 PC 의 '내 이름'(설정 `name`, 처음엔 PC 호스트명). 다시 초대면 `name` 무시하고 팀에 있던 이름 유지(새 PC 의 '내 이름'이 그걸로 바뀜). 새 팀원 이름이 겹치면 409 `name_taken`, 초대장은 남고 참여 창이 다른 이름을 물어 다시 시도. 초대장은 성공 시 폐기 (5회 실패 시 IP 1분 잠금) |
| `/api/team/me?port=` | GET | 팀원 신호(20초 주기, 75초 안 = 접속 중) + 팀원 목록·비밀키 |
| `/api/team/profile` | POST | 이름 변경 |
| `/api/team/{info,events,note,month,schedule,sched-doc}` | | 개인 공간과 같은 형식, 팀원만 |
| `/api/update`, `/api/update/app.zip` | GET | 배포 PC 에서만: 버전·sha256 / zip |

- 개인 공간 "팀원에게만 공유"(`team_only`)가 켜져 있으면 팀원 토큰 없는 요청은 403 `team_only`
- 공유 일시 중지 중엔 남에게 `info`(paused)·`events`(status)만 응답, 나머지 503

## 5. 인증·보안 (사무실 수준)

- 개인 노트는 기본적으로 사내망 누구나 읽기 가능 (암호 없음). 차단·일시 중지·팀원 전용으로 제한
- 초대장: 사람마다 다른 일회용 코드, `A-Z 2-9`(0·O·1·I 제외) 12자(약 60비트), `XXXX-XXXX-XXXX` 표시(입력은 대소문자·구분자 무시), 15분 뒤 만료. 호스트 트레이에서 만들기(클립보드에 주소+코드 복사)·목록·복사·폐기
- 다시 초대(PC 바꿈) = 그 팀원 id 에 묶인 초대장. 쓰는 순간 그 사람의 다른 초대장 삭제 + `min_iat` = 지금+1초, 새 토큰 발급시각 = `min_iat` → 예전 PC 토큰 무효. 새로 다시 초대하면 이전 다시 초대 초대장은 폐기. 이름·PIN 로그인은 없음
- 토큰: `팀원ID.발급시각.Ed25519서명(hex)`. 호스트만 개인키로 서명, 팀원 PC 는 `/api/team/me` 로 받은 공개키·팀원 목록(`min_iat` 포함)으로 직접 검증 → 호스트가 꺼져도 팀원 전용 공유 동작, 공개키로는 위조 불가
- 내보내기 = 목록에서 삭제, 다시 초대로 참여 = `min_iat` 올림. 내보내면 그 사람 초대장도 삭제. 각 PC 는 다음 `/me` 폴링(20초) 때 반영. 남은 팀원 토큰은 그대로 유효(키 교체 없음)
- 예전 HMAC `secret` 은 로드 시 삭제 → 예전 토큰 전부 무효, 팀원은 초대장으로 1회 다시 참여
- 업데이트: GitHub `nodogani/dogani` 의 태그 zip(HTTPS)만 받음. 서명 확인 없음 (저장소 쓰기 권한 = 배포 권한)

## 6. 배포·업데이트

1. 배포 = GitHub 에 버전 태그(`vYY.MM.DD.NN`) push. 커밋·push 만으로는 안 나감. 태그의 `app/`(editor 제외)만 나감
2. 모든 exe(나·동료 동일, 디스코드식): 켤 때 런처가 최신 태그 확인(5초 제한) → 새 버전이면 받아 `app.new` → `app` 교체 후 실행. 확인 실패면 지금 버전으로 실행
3. 실행 중: 30초 뒤 + 1시간마다 확인만 → 트레이 버전 옆 "(재시작하면 적용)". 재시작은 사용자가
4. 버전 비교 = 태그 문자열 비교 (형식 고정이라 시간순과 같음)
5. 소스 실행(개발·테스트)은 업데이트 안 받음, 트레이 버전 = "마지막 태그 개발 중"
6. exe 재빌드 필요: `deps.py` 에 새 라이브러리 / `launcher.py` 수정

## 7. UI

- 트레이: Win32 기본 메뉴, CSS 없음. 아이콘 색 초록(공유)/주황(일시 중지)/회색(포트 사용 중)
- 오버레이: 네비게이션 바 + 할일 창 + 달력 창(각각 이동·크기 조절, 위치 기억). 바 = `● 이름 ▾ [할일][달력]` (아이콘 없음), 이름 클릭 → 공간 선택
- 달력 칸: 하루 일정은 위(남는 자리만), 기간 띠는 칸 아래 고정 영역(한 주 안에서 줄 수 같음, 숨기지 않음). 하루 일정이 넘치면 뒤에서부터 숨기고 `+N` 배지(날짜 숫자 오른쪽, 강조색)
- 달력 칸 우클릭 또는 칸 드래그(쓰기 권한 있을 때): 일정 추가 팝업. 세그먼트 [일정 | 기간], 제목 아래 칩(중요·애매함·쉬는 날), 날짜 `시작 - 끝`, 시간(종일 또는 30분 단위 `시작 - 끝`, 애매함=`?`), 태그(쉼표 구분), 장소. 일정 = 하는 일: 시작 필수, 끝 비우면 하루, 여러 날이면 띠. 기간 = 상태: 항상 `*기울임*`(화살표)로 저장, 한쪽 비우면 `261119-` 끝 미정 / `-261119` 마감. 일정 파일(`/sched-doc`) 끝에 한 줄 추가, 충돌(409) 시 한 번 재시도
- 달력 일정(쓰기 권한, 공용 일정 제외): 마우스오버 강조, 클릭 → 같은 팝업으로 수정·삭제(메모 칸 포함, 메모 없으면 `+ 메모`로 펼침), 왼쪽 드래그 → 날짜 이동(기간·시간 유지). `/schedule` 일정마다 `raw`(원래 줄, 앞뒤 공백 제거)를 주고, 일정 파일에서 같은 줄을 찾아 바꿈(목록 기호·들여쓰기 유지). 못 찾으면(다른 일정 파일 등) 오류 안내
- 개수 배지(`0/3`)는 칸 오른쪽 위 (숫자 줄)
- 할일 창: 위 막대 `‹ 날짜 › 오늘 ✕` (달력 창과 같은 형식, 화살표는 근무일만) + 진행 막대(할 일 없으면 빈 막대, 숫자 없음)
- 연결 점: 초록 접속 / 주황 상대 일시 중지 / 빨강 차단 / 회색 끊김
- 편집: CodeMirror 6, 0.7초 자동 저장, Ctrl+S, 체크박스 클릭 즉시 저장, Enter 목록 이어쓰기, Tab 들여쓰기
- 바탕화면 고정(Win+D 유지) / 항상 위 / 투명도 / 다크 테마

## 8. 제약·알려진 한계

- Windows 전용, 한국어 고정
- 투명 여백(창 가장자리 10px) 클릭 통과 안 됨
- 자동 테스트 없음. JS 는 Node 없는 환경에서 문법 검사 불가
