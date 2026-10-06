# Health Tracker — 가족 건강검진 기록 관리

design.md 요구사항 기반의 셀프호스팅 웹앱(PWA)입니다.
Raspberry Pi 5 + Docker 환경을 대상으로 하며, PC/모바일 브라우저에서 동작합니다.

## 주요 기능
- 사용자(가족 구성원) 추가/수정/삭제, 이름 선택만으로 로그인
- 최초 접속 시 사용자가 없으면 사용자 추가 폼이 바로 노출
- 사용자별 데이터 분리 (checkup_results.user_id)
- 검진 항목 추가/수정/삭제: 이름, 단위, 대상 성별(ALL/M/F)
  - 대상 성별에 따라 로그인 사용자에게 불필요한 항목은 자동으로 숨김
- 판정 구간: 정상 구간 필수 + 경계/위험 등 자유 라벨 구간 추가 가능
  - 항목당 판정 수준별 1개 (UNIQUE 제약), 조회 시 실시간 판정
- 대시보드: 항목별 최근 결과 카드 + 판정 배지 + 항목 검색
- 상세: Chart.js 선 그래프(판정 구간을 색상 밴드로 표시) + 기록 표
  - 조회 기간 설정: 전체 / 최근 1·3·5년 프리셋 + 사용자 지정 기간
- 결과 입력: 과거 날짜 소급 입력 가능(항상 날짜순 정렬), 같은 날짜 재입력 시 덮어쓰기

## 배포 (Raspberry Pi 5, Docker)
```bash
docker compose up -d --build
# → http://<Pi IP>:8000
```
데이터는 ./data/health.db (SQLite) 하나에 저장됩니다.

## Docker 없이 실행
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## DB 구조 (design.md 매핑)
SQLite 관례에 따라 VARCHAR→TEXT, ENUM→TEXT+CHECK, DATE→TEXT(ISO), FLOAT→REAL로 매핑했습니다.
- users(id, name UNIQUE, birth_date, gender CHECK(M,F))
- checkup_items(id, item_name UNIQUE, unit, target_gender CHECK(ALL,M,F))
- checkup_item_ranges(id, item_id FK, min_value, max_value, judgement_level, UNIQUE(item_id, judgement_level))
- checkup_results(id, user_id FK, item_id FK, date, value, note, UNIQUE(user_id, item_id, date))

## API 요약
- `GET/POST /api/users`, `PUT/DELETE /api/users/{id}`
- `GET /api/items[?gender=M|F]`, `POST /api/items`, `PUT/DELETE /api/items/{id}` (ranges 포함, PUT은 구간 전체 교체)
- `GET /api/users/{uid}/summary` — 성별 필터 적용된 항목 + 최근 결과
- `GET /api/users/{uid}/items/{iid}/results[?date_from=&date_to=]` — 날짜 오름차순
- `POST /api/users/{uid}/items/{iid}/results` — 같은 날짜면 upsert
- `DELETE /api/results/{id}`

## 보안 참고
- 요구사항대로 인증 없이 이름 선택 로그인만 제공하므로, 반드시 내부망 전용으로 운영하세요.
- 외부 접속이 필요해지면 포트포워딩 대신 VPN(Tailscale/WireGuard)을 사용하세요.
