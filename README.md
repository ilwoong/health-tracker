# Health Tracker — 가족 건강검진 기록 관리

[spec/design.md](spec/design.md) 요구사항 기반의 셀프호스팅 웹앱(PWA)입니다.
Raspberry Pi 5 + Docker 환경을 대상으로 하며, PC/모바일 브라우저에서 동작합니다.

## 주요 기능
- 사용자(가족 구성원) 추가/수정/삭제, 이름 선택만으로 로그인
- 최초 접속 시 사용자가 없으면 사용자 추가 폼이 바로 노출
- 사용자별 데이터 분리 (checkup_results.user_id)
- 검진 항목 추가/수정/삭제: 이름, 단위, 값 유형(숫자/문자), 대상 성별(ALL/M/F), 카테고리(선택)
  - 대상 성별에 따라 로그인 사용자에게 불필요한 항목은 자동으로 숨김
  - 같은 이름의 항목은 대상 성별이 ALL이면 하나만, M/F로 나누면 성별당 하나씩 생성 가능
    (ALL 항목과 동명의 M/F 항목은 공존 불가 — 나누려면 기존 항목의 대상 성별을 먼저 수정)
  - 결과가 이미 있는 항목은 값 유형 변경 불가
- 값 유형
  - 숫자형(NUMBER): 판정 구간 + 그래프 + 기록 표
  - 문자형(TEXT): 판정 구간 없이 기록 표로만 관리 (예: 요잠혈 음성/양성)
- 판정 구간 (숫자형 항목)
  - 판정 구간 1개 이상 필수(UI 기본값 '정상'), 경계/위험 등 자유 라벨 구간 추가 가능
  - 항목당 판정 수준별 1개 (UNIQUE 제약), 조회 시 실시간 판정
  - 구간별 색상 5종 직접 지정: 청록(ok) / 파랑(lowish) / 주황(warn) / 빨강(danger) / 회색(etc)
    — 판정 수준 이름의 키워드로 초기값 제안, 배지·그래프 밴드·데이터 포인트에 일관 적용
  - 구간이 하나뿐이면 범위를 벗어난 값은 '위험'(빨강)으로 표시,
    구간이 2개 이상이면 어느 구간에도 속하지 않는 값은 판정 없음
- 카테고리: 검진 항목을 묶는 1단계 분류 (예: 혈액, 간기능). 대시보드 헤더의 `카테고리 관리`에서 추가/이름 변경/삭제
  - 카테고리가 없는 항목은 '미분류'. 카테고리를 삭제해도 항목은 남고 미분류가 됨
  - 카테고리가 지정된 항목이 하나라도 있으면 대시보드와 일괄 입력 화면을 카테고리별 섹션(미분류 마지막)으로 묶음
  - 대시보드에는 필터 칩(전체/카테고리/미분류)이 추가되고 검색과 함께(AND) 적용됨
- 표시 순서: 대시보드 헤더의 `순서 편집`에서 ▲▼ 버튼으로 카테고리와 항목 순서를 지정 (모든 사용자 공유)
  - 지정하지 않은 것은 이름순. 새 항목·카테고리는 이름순으로 뒤에 붙음
  - 대시보드·일괄 입력·항목 폼의 카테고리 선택이 같은 순서를 따름
- 대시보드: 항목별 최근 결과 카드 + 판정 배지 + 항목 검색
- 상세: Chart.js 선 그래프(판정 구간을 색상 밴드로 표시) + 기록 표
  - 조회 기간 설정: 전체 / 최근 1·3·5년 프리셋 + 사용자 지정 기간
- 결과 입력: 과거 날짜 소급 입력 가능(항상 날짜순 정렬), 같은 날짜 재입력 시 덮어쓰기
- 일괄 입력: 날짜 하나에 여러 항목 결과를 한 화면에서 입력 (종합검진 등)
  - 그 날짜의 기존 기록을 미리 채움, 값을 입력/변경한 항목만 저장
  - 공통 비고 + 항목별 비고 (항목별 비고가 비어 있으면 공통 비고 저장)
  - 하나라도 실패하면 전체 저장 취소

## 배포 (Raspberry Pi 5, Docker)
```bash
docker compose up -d --build
# → http://<Pi IP>:8000
```
데이터는 ./data/health.db (SQLite) 하나에 저장됩니다. 이 폴더만 백업하면 됩니다.

## Docker 없이 실행
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
DB 경로는 환경변수 `DB_PATH`로 바꿀 수 있습니다 (기본값: `./data/health.db`).

## 프로젝트 구조
```
app.py               FastAPI 백엔드 (DB 초기화, API, 정적 파일 서빙)
static/index.html    단일 페이지 (로그인 / 대시보드 / 상세 / 폼)
static/app.js        프론트엔드 로직 (Vanilla JS, Chart.js)
static/style.css     스타일
static/sw.js         Service Worker (PWA 오프라인 캐시)
static/manifest.json PWA 매니페스트
spec/design.md       기본 요구사항 명세
spec/NNN-*.md        추가 기능 스펙
```

## DB 구조 (spec/design.md 매핑)
SQLite 관례에 따라 VARCHAR→TEXT, ENUM→TEXT+CHECK, DATE→TEXT(ISO), FLOAT→REAL로 매핑했습니다.
- users(id, name UNIQUE, birth_date, gender CHECK(M,F))
- checkup_categories(id, name UNIQUE, sort_order NULL)
- checkup_items(id, item_name, unit, value_type CHECK(NUMBER,TEXT), target_gender CHECK(ALL,M,F),
  category_id FK NULL ON DELETE SET NULL, sort_order NULL, UNIQUE(item_name, target_gender))
- checkup_item_ranges(id, item_id FK, min_value, max_value, judgement_level,
  color CHECK(ok,lowish,warn,danger,etc), UNIQUE(item_id, judgement_level))
- checkup_results(id, user_id FK, item_id FK, date, value NULL, value_text NULL, note,
  UNIQUE(user_id, item_id, date), CHECK(value / value_text 중 정확히 하나))

사용자·항목 삭제 시 관련 구간과 결과는 함께 삭제됩니다 (ON DELETE CASCADE).

> 참고: 테이블은 `CREATE TABLE IF NOT EXISTS`로 생성됩니다. 마이그레이션은 앱 시작 시 `init_db`에서
> 컬럼 존재 여부를 확인해 처리하며, 현재는 `category_id`(spec/001)와 `sort_order`(spec/003) 추가가 있습니다.
> 그보다 오래된 스키마(value_type, value_text, color가 없는 DB)는 자동으로 올라가지 않습니다.

## API 요약
- `GET/POST /api/users`, `PUT/DELETE /api/users/{id}`
- `GET/POST /api/categories`, `PUT/DELETE /api/categories/{id}` — 응답에 소속 항목 수(`item_count`) 포함
- `PUT /api/order` — body `{category_ids, item_ids}`, 배열 인덱스를 표시 순서로 저장 (한 트랜잭션)
- `GET /api/items[?gender=M|F]`, `POST /api/items`, `PUT/DELETE /api/items/{id}`
  - ranges 포함, PUT은 구간 전체 교체
  - TEXT 항목은 ranges를 비워야 하고, NUMBER 항목은 1개 이상 필요
  - `category_id`(nullable)로 카테고리 지정. 존재하지 않으면 422
- `GET /api/users/{uid}/summary` — 성별 필터 적용된 항목 + 최근 결과(latest_value / latest_value_text / latest_date / result_count)
- `GET /api/users/{uid}/items/{iid}/results[?date_from=&date_to=]` — 날짜 오름차순
- `POST /api/users/{uid}/items/{iid}/results` — 같은 날짜면 upsert
  - body: `{date, value | value_text, note}` — 항목의 값 유형에 맞는 필드 하나만 입력
- `GET /api/users/{uid}/results?date=YYYY-MM-DD` — 특정 날짜의 모든 항목 결과
- `POST /api/users/{uid}/results/batch` — 일괄 저장 (한 트랜잭션)
  - body: `{date, entries: [{item_id, value | value_text, note}]}` — entry별 규칙은 단건 입력과 동일
- `DELETE /api/results/{id}`

## 보안 참고
- 요구사항대로 인증 없이 이름 선택 로그인만 제공하므로, 반드시 내부망 전용으로 운영하세요.
- 외부 접속이 필요해지면 포트포워딩 대신 VPN(Tailscale/WireGuard)을 사용하세요.
