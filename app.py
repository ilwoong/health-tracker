"""Health Tracker - 가족 건강검진 기록 관리 (spec/design.md 기반)

테이블 구조 (spec/design.md의 타입을 SQLite 관례로 매핑: VARCHAR→TEXT, ENUM→TEXT+CHECK, DATE→TEXT(ISO), FLOAT→REAL)
- users:               사용자 (이름 UNIQUE, 생일, 성별 M/F)
- checkup_categories:  검진 항목 카테고리 (이름 UNIQUE, 표시 순서 nullable)
- checkup_items:       검진 항목 (이름 UNIQUE, 단위, 대상 성별 ALL/M/F, 카테고리 FK nullable, 표시 순서 nullable)
- checkup_item_ranges: 항목별 판정 구간 (정상/경계/위험 등, 항목당 판정 수준별 1개)
- checkup_results:     사용자별 검진 결과 (UNIQUE(user_id, item_id, date) → 같은 날짜 재입력 시 덮어쓰기)
"""
import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError, field_validator, model_validator

GENDER_LABEL = {"M": "남", "F": "여"}
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "health.db"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Health Tracker")


# ---------- DB ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            birth_date TEXT NOT NULL,
            gender     TEXT NOT NULL CHECK (gender IN ('M', 'F'))
        );
        CREATE TABLE IF NOT EXISTS checkup_categories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            sort_order INTEGER                    -- NULL이면 이름순으로 뒤에
        );
        CREATE TABLE IF NOT EXISTS checkup_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name     TEXT NOT NULL,
            unit          TEXT NOT NULL DEFAULT '',
            value_type    TEXT NOT NULL DEFAULT 'NUMBER' CHECK (value_type IN ('NUMBER', 'TEXT')),
            target_gender TEXT NOT NULL DEFAULT 'ALL' CHECK (target_gender IN ('ALL', 'M', 'F')),
            category_id   INTEGER REFERENCES checkup_categories(id) ON DELETE SET NULL,  -- NULL이면 미분류
            sort_order    INTEGER,               -- 카테고리 안 표시 순서. NULL이면 이름순으로 뒤에
            UNIQUE (item_name, target_gender)
        );
        CREATE TABLE IF NOT EXISTS checkup_item_ranges (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         INTEGER NOT NULL REFERENCES checkup_items(id) ON DELETE CASCADE,
            min_value       REAL,
            max_value       REAL,
            judgement_level TEXT NOT NULL,
            color           TEXT NOT NULL DEFAULT 'etc'
                            CHECK (color IN ('ok', 'lowish', 'warn', 'danger', 'etc')),
            UNIQUE (item_id, judgement_level)
        );
        CREATE TABLE IF NOT EXISTS checkup_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id    INTEGER NOT NULL REFERENCES checkup_items(id) ON DELETE CASCADE,
            date       TEXT NOT NULL,
            value      REAL,               -- 숫자형(NUMBER) 항목의 측정값
            value_text TEXT,               -- 문자형(TEXT) 항목의 측정값
            note       TEXT NOT NULL DEFAULT '',
            UNIQUE (user_id, item_id, date),
            CHECK ((value IS NULL) != (value_text IS NULL))  -- 정확히 한쪽만 채워짐
        );
        CREATE INDEX IF NOT EXISTS idx_results_user_item_date
            ON checkup_results (user_id, item_id, date);
        """)
        # 마이그레이션: 컬럼 도입 이전에 만든 DB에 컬럼 추가
        item_cols = [r["name"] for r in conn.execute("PRAGMA table_info(checkup_items)")]
        if "category_id" not in item_cols:   # spec/001
            conn.execute("""ALTER TABLE checkup_items ADD COLUMN category_id INTEGER
                            REFERENCES checkup_categories(id) ON DELETE SET NULL""")
        if "sort_order" not in item_cols:    # spec/003
            conn.execute("ALTER TABLE checkup_items ADD COLUMN sort_order INTEGER")
        cat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(checkup_categories)")]
        if "sort_order" not in cat_cols:     # spec/003
            conn.execute("ALTER TABLE checkup_categories ADD COLUMN sort_order INTEGER")


init_db()


# ---------- 스키마 ----------

def _valid_iso(v: str, label: str) -> str:
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{label} 형식은 YYYY-MM-DD 입니다")
    return v


class UserIn(BaseModel):
    name: str
    birth_date: str
    gender: Literal["M", "F"]

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("이름을 입력하세요")
        return v

    @field_validator("birth_date")
    @classmethod
    def birth_valid(cls, v):
        return _valid_iso(v, "생일")


class CategoryIn(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_valid(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("카테고리 이름을 입력하세요")
        if v == "미분류":
            raise ValueError("'미분류'는 카테고리 이름으로 쓸 수 없습니다")
        return v


class OrderIn(BaseModel):
    category_ids: list[int] = []
    item_ids: list[int] = []

    @model_validator(mode="after")
    def no_duplicates(self):
        if len(self.category_ids) != len(set(self.category_ids)) or len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("같은 id가 중복되었습니다")
        return self


class RangeIn(BaseModel):
    min_value: float | None = None
    max_value: float | None = None
    judgement_level: str
    color: Literal["ok", "lowish", "warn", "danger", "etc"] = "etc"

    @field_validator("judgement_level")
    @classmethod
    def level_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("판정 수준 이름을 입력하세요")
        return v


class ItemIn(BaseModel):
    item_name: str
    unit: str = ""
    value_type: Literal["NUMBER", "TEXT"] = "NUMBER"
    target_gender: Literal["ALL", "M", "F"] = "ALL"
    category_id: int | None = None   # None이면 미분류
    ranges: list[RangeIn] = []

    @field_validator("item_name")
    @classmethod
    def item_name_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("항목 이름을 입력하세요")
        return v

    @model_validator(mode="after")
    def ranges_valid(self):
        if self.value_type == "TEXT":
            if self.ranges:
                raise ValueError("문자형 항목은 판정 구간을 가질 수 없습니다")
            return self
        # NUMBER: 정상 구간 최소 1개 필수
        if not self.ranges:
            raise ValueError("숫자형 항목은 판정 구간을 최소 1개(정상 구간) 입력해야 합니다")
        levels = [r.judgement_level for r in self.ranges]
        if len(levels) != len(set(levels)):
            raise ValueError("판정 수준 이름이 중복되었습니다")
        for r in self.ranges:
            if r.min_value is None and r.max_value is None:
                raise ValueError(f"'{r.judgement_level}' 구간의 최소값 또는 최대값을 입력하세요")
            if r.min_value is not None and r.max_value is not None and r.min_value > r.max_value:
                raise ValueError(f"'{r.judgement_level}' 구간의 최소값이 최대값보다 큽니다")
        return self


class ResultValueIn(BaseModel):
    value: float | None = None       # 숫자형 항목용
    value_text: str | None = None    # 문자형 항목용
    note: str = ""

    @field_validator("value_text")
    @classmethod
    def text_strip(cls, v):
        if v is not None:
            v = v.strip()
        return v or None

    @model_validator(mode="after")
    def exactly_one_value(self):
        if (self.value is None) == (self.value_text is None):
            raise ValueError("value(숫자) 또는 value_text(문자) 중 하나만 입력해야 합니다")
        return self


class ResultIn(ResultValueIn):
    date: str

    @field_validator("date")
    @classmethod
    def date_valid(cls, v):
        return _valid_iso(v, "날짜")


class BatchEntryIn(ResultValueIn):
    item_id: int


class BatchResultIn(BaseModel):
    date: str
    entries: list[BatchEntryIn]

    @field_validator("date")
    @classmethod
    def date_valid(cls, v):
        return _valid_iso(v, "날짜")

    @model_validator(mode="after")
    def entries_valid(self):
        if not self.entries:
            raise ValueError("저장할 항목이 없습니다")
        ids = [e.item_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 항목이 중복되었습니다")
        return self


# ---------- 공통 ----------

def _get_or_404(conn, table: str, row_id: int, label: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{label}을(를) 찾을 수 없습니다")
    return row


def _check_item_name_rule(conn, name: str, gender: str, exclude_id: int | None = None):
    """같은 이름 항목 규칙:
    - target_gender가 ALL이면 같은 이름은 하나만 존재 가능
    - M/F로 나뉘는 경우 성별당 하나씩 존재 가능
    - ALL 항목과 동명의 M/F 항목은 공존 불가
    """
    sql = "SELECT target_gender FROM checkup_items WHERE item_name = ?"
    params: list = [name]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    existing = [r["target_gender"] for r in conn.execute(sql, params).fetchall()]
    if not existing:
        return
    if gender == "ALL":
        raise HTTPException(
            409, f"'{name}' 항목이 이미 있습니다. 대상 성별 '전체' 항목은 이름당 하나만 만들 수 있습니다")
    if "ALL" in existing:
        raise HTTPException(
            409, f"'{name}'은(는) 대상 성별 '전체' 항목으로 이미 있습니다. "
                 "성별별로 나누려면 기존 항목의 대상 성별을 먼저 수정하세요")
    if gender in existing:
        raise HTTPException(409, f"같은 이름·같은 대상 성별의 '{name}' 항목이 이미 있습니다")


def _check_category_exists(conn, category_id: int | None):
    if category_id is None:
        return
    if not conn.execute("SELECT 1 FROM checkup_categories WHERE id = ?", (category_id,)).fetchone():
        raise HTTPException(422, "존재하지 않는 카테고리입니다")


def _item_with_ranges(conn, item_row) -> dict:
    ranges = conn.execute(
        """SELECT id, min_value, max_value, judgement_level, color
             FROM checkup_item_ranges WHERE item_id = ?
            ORDER BY COALESCE(min_value, -1e308)""",
        (item_row["id"],),
    ).fetchall()
    d = dict(item_row)
    d["ranges"] = [dict(r) for r in ranges]
    return d


# ---------- 사용자 API ----------

@app.get("/api/users")
def list_users():
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/users", status_code=201)
def create_user(body: UserIn):
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (name, birth_date, gender) VALUES (?, ?, ?)",
                (body.name, body.birth_date, body.gender),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름의 사용자가 있습니다. 별명이나 이니셜로 구분해 주세요")
        return dict(_get_or_404(conn, "users", cur.lastrowid, "사용자"))


@app.put("/api/users/{user_id}")
def update_user(user_id: int, body: UserIn):
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        try:
            conn.execute(
                "UPDATE users SET name = ?, birth_date = ?, gender = ? WHERE id = ?",
                (body.name, body.birth_date, body.gender, user_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름의 사용자가 있습니다. 별명이나 이니셜로 구분해 주세요")
        return dict(_get_or_404(conn, "users", user_id, "사용자"))


@app.delete("/api/users/{user_id}", status_code=204)
def delete_user(user_id: int):
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------- 카테고리 API ----------

def _category_row(conn, category_id: int) -> dict:
    row = conn.execute(
        """SELECT c.id, c.name, c.sort_order,
                  (SELECT COUNT(*) FROM checkup_items WHERE category_id = c.id) AS item_count
             FROM checkup_categories c WHERE c.id = ?""",
        (category_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "카테고리를 찾을 수 없습니다")
    return dict(row)


@app.get("/api/categories")
def list_categories():
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.sort_order,
                      (SELECT COUNT(*) FROM checkup_items WHERE category_id = c.id) AS item_count
                 FROM checkup_categories c
                ORDER BY c.sort_order IS NULL, c.sort_order, c.name COLLATE NOCASE, c.id"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/categories", status_code=201)
def create_category(body: CategoryIn):
    with db() as conn:
        try:
            cur = conn.execute("INSERT INTO checkup_categories (name) VALUES (?)", (body.name,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름의 카테고리가 있습니다")
        return _category_row(conn, cur.lastrowid)


@app.put("/api/categories/{category_id}")
def update_category(category_id: int, body: CategoryIn):
    with db() as conn:
        _category_row(conn, category_id)
        try:
            conn.execute("UPDATE checkup_categories SET name = ? WHERE id = ?", (body.name, category_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름의 카테고리가 있습니다")
        return _category_row(conn, category_id)


@app.delete("/api/categories/{category_id}", status_code=204)
def delete_category(category_id: int):
    """소속 항목은 ON DELETE SET NULL로 미분류가 된다."""
    with db() as conn:
        _category_row(conn, category_id)
        conn.execute("DELETE FROM checkup_categories WHERE id = ?", (category_id,))


# ---------- 표시 순서 API ----------

@app.put("/api/order", status_code=204)
def save_order(body: OrderIn):
    """배열 인덱스를 sort_order로 저장. 배열에 없는 것은 건드리지 않는다."""
    with db() as conn:
        for i, cid in enumerate(body.category_ids):
            _get_or_404(conn, "checkup_categories", cid, "카테고리")
            conn.execute("UPDATE checkup_categories SET sort_order = ? WHERE id = ?", (i, cid))
        for i, iid in enumerate(body.item_ids):
            _get_or_404(conn, "checkup_items", iid, "검진 항목")
            conn.execute("UPDATE checkup_items SET sort_order = ? WHERE id = ?", (i, iid))


# ---------- 검진 항목 API ----------

@app.get("/api/items")
def list_items(gender: Literal["M", "F"] | None = None):
    """gender를 주면 해당 성별에게 보이는 항목만 반환 (target_gender가 ALL 또는 일치)."""
    with db() as conn:
        if gender:
            rows = conn.execute(
                """SELECT * FROM checkup_items WHERE target_gender IN ('ALL', ?)
                    ORDER BY sort_order IS NULL, sort_order, item_name COLLATE NOCASE, id""",
                (gender,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM checkup_items ORDER BY sort_order IS NULL, sort_order, item_name COLLATE NOCASE, id"
            ).fetchall()
        return [_item_with_ranges(conn, r) for r in rows]


@app.post("/api/items", status_code=201)
def create_item(body: ItemIn):
    with db() as conn:
        _check_item_name_rule(conn, body.item_name, body.target_gender)
        _check_category_exists(conn, body.category_id)
        try:
            cur = conn.execute(
                """INSERT INTO checkup_items (item_name, unit, value_type, target_gender, category_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.item_name, body.unit.strip(), body.value_type, body.target_gender, body.category_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름·같은 대상 성별의 검진 항목이 있습니다")
        item_id = cur.lastrowid
        for r in body.ranges:
            conn.execute(
                """INSERT INTO checkup_item_ranges
                       (item_id, min_value, max_value, judgement_level, color)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, r.min_value, r.max_value, r.judgement_level, r.color),
            )
        return _item_with_ranges(conn, _get_or_404(conn, "checkup_items", item_id, "검진 항목"))


@app.put("/api/items/{item_id}")
def update_item(item_id: int, body: ItemIn):
    """항목 기본 정보 수정 + 판정 구간 전체 교체."""
    with db() as conn:
        item = _get_or_404(conn, "checkup_items", item_id, "검진 항목")
        _check_item_name_rule(conn, body.item_name, body.target_gender, exclude_id=item_id)
        _check_category_exists(conn, body.category_id)
        if item["value_type"] != body.value_type:
            has_results = conn.execute(
                "SELECT 1 FROM checkup_results WHERE item_id = ? LIMIT 1", (item_id,)
            ).fetchone()
            if has_results:
                raise HTTPException(409, "이미 기록이 있는 항목의 값 유형은 변경할 수 없습니다")
        try:
            conn.execute(
                """UPDATE checkup_items
                      SET item_name = ?, unit = ?, value_type = ?, target_gender = ?, category_id = ?
                    WHERE id = ?""",
                (body.item_name, body.unit.strip(), body.value_type, body.target_gender,
                 body.category_id, item_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 이름·같은 대상 성별의 검진 항목이 있습니다")
        conn.execute("DELETE FROM checkup_item_ranges WHERE item_id = ?", (item_id,))
        for r in body.ranges:
            conn.execute(
                """INSERT INTO checkup_item_ranges
                       (item_id, min_value, max_value, judgement_level, color)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, r.min_value, r.max_value, r.judgement_level, r.color),
            )
        return _item_with_ranges(conn, _get_or_404(conn, "checkup_items", item_id, "검진 항목"))


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    with db() as conn:
        _get_or_404(conn, "checkup_items", item_id, "검진 항목")
        conn.execute("DELETE FROM checkup_items WHERE id = ?", (item_id,))


# ---------- 검진 항목 합치기 (spec/005) ----------

class MergeIn(BaseModel):
    from_id: int


def _merge_preview(conn, keep_id: int, from_id: int) -> dict:
    if keep_id == from_id:
        raise HTTPException(422, "같은 항목끼리는 합칠 수 없습니다")
    keep = _get_or_404(conn, "checkup_items", keep_id, "남길 항목")
    src = _get_or_404(conn, "checkup_items", from_id, "합칠 항목")
    if keep["value_type"] != src["value_type"]:
        raise HTTPException(422, "값 유형(숫자/문자)이 같은 항목끼리만 합칠 수 있습니다")
    total, users = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM checkup_results WHERE item_id = ?", (from_id,)).fetchone()
    dropped = conn.execute(
        """SELECT COUNT(*) FROM checkup_results s
            WHERE s.item_id = ? AND EXISTS (SELECT 1 FROM checkup_results k
                                            WHERE k.item_id = ? AND k.user_id = s.user_id AND k.date = s.date)""",
        (from_id, keep_id)).fetchone()[0]
    warnings, blocked = [], []
    if keep["unit"].strip() != src["unit"].strip():
        warnings.append(f"단위가 다릅니다: '{keep['unit']}' vs '{src['unit']}'. 값은 환산 없이 그대로 옮겨집니다")
    if conn.execute("SELECT 1 FROM checkup_item_ranges WHERE item_id = ? LIMIT 1", (from_id,)).fetchone():
        warnings.append("합칠 항목의 판정 구간은 버려지고 남길 항목의 구간을 씁니다")
    if keep["target_gender"] != "ALL":
        hidden = [r["name"] for r in conn.execute(
            """SELECT DISTINCT u.name FROM checkup_results r JOIN users u ON u.id = r.user_id
                WHERE r.item_id = ? AND u.gender != ? ORDER BY u.name""", (from_id, keep["target_gender"]))]
        if hidden:
            blocked.append(f"남길 항목이 {GENDER_LABEL[keep['target_gender']]}성 전용이라 "
                           f"{', '.join(hidden)}의 기록이 가려집니다. 남길 항목의 대상 성별을 '전체'로 바꾼 뒤 다시 하세요")
    return {"moved": total - dropped, "dropped": dropped, "users": users, "warnings": warnings, "blocked": blocked}


@app.get("/api/items/{keep_id}/merge-preview")
def merge_preview(keep_id: int, from_: int = Query(alias="from")):
    with db() as conn:
        return _merge_preview(conn, keep_id, from_)


@app.post("/api/items/{keep_id}/merge")
def merge_items(keep_id: int, body: MergeIn):
    """합칠 항목의 기록을 남길 항목으로 옮기고 합칠 항목을 삭제한다. 충돌 기록은 남길 항목 값을 유지."""
    with db() as conn:
        preview = _merge_preview(conn, keep_id, body.from_id)
        if preview["blocked"]:
            raise HTTPException(422, " ".join(preview["blocked"]))
        conn.execute(
            """DELETE FROM checkup_results
                WHERE item_id = ? AND EXISTS (SELECT 1 FROM checkup_results k
                                              WHERE k.item_id = ? AND k.user_id = checkup_results.user_id
                                                AND k.date = checkup_results.date)""",
            (body.from_id, keep_id))
        conn.execute("UPDATE checkup_results SET item_id = ? WHERE item_id = ?", (keep_id, body.from_id))
        conn.execute("DELETE FROM checkup_items WHERE id = ?", (body.from_id,))   # 기록을 옮긴 뒤에 삭제 (CASCADE 주의)
        return {"moved": preview["moved"], "dropped": preview["dropped"]}


# ---------- 대시보드 요약 API ----------

@app.get("/api/users/{user_id}/summary")
def user_summary(user_id: int):
    """사용자 성별에 맞는 항목 목록 + 항목별 최근 결과."""
    with db() as conn:
        user = _get_or_404(conn, "users", user_id, "사용자")
        items = conn.execute(
            """SELECT i.*,
                      (SELECT value FROM checkup_results
                        WHERE user_id = ? AND item_id = i.id
                        ORDER BY date DESC LIMIT 1) AS latest_value,
                      (SELECT value_text FROM checkup_results
                        WHERE user_id = ? AND item_id = i.id
                        ORDER BY date DESC LIMIT 1) AS latest_value_text,
                      (SELECT date FROM checkup_results
                        WHERE user_id = ? AND item_id = i.id
                        ORDER BY date DESC LIMIT 1) AS latest_date,
                      (SELECT COUNT(*) FROM checkup_results
                        WHERE user_id = ? AND item_id = i.id) AS result_count
                 FROM checkup_items i
                WHERE i.target_gender IN ('ALL', ?)
                ORDER BY i.sort_order IS NULL, i.sort_order, i.item_name COLLATE NOCASE, i.id""",
            (user_id, user_id, user_id, user_id, user["gender"]),
        ).fetchall()
        return [_item_with_ranges(conn, r) for r in items]


# ---------- 검진 결과 API ----------

@app.get("/api/users/{user_id}/items/{item_id}/results")
def list_results(user_id: int, item_id: int,
                 date_from: str | None = None, date_to: str | None = None):
    """날짜 오름차순 정렬. date_from/date_to로 조회 기간 제한."""
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        _get_or_404(conn, "checkup_items", item_id, "검진 항목")
        sql = "SELECT * FROM checkup_results WHERE user_id = ? AND item_id = ?"
        params: list = [user_id, item_id]
        if date_from:
            sql += " AND date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND date <= ?"
            params.append(date_to)
        sql += " ORDER BY date ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _upsert_result(conn, user_id: int, item, day: str, body: ResultValueIn) -> dict:
    """같은 (사용자, 항목, 날짜)에 값이 있으면 덮어쓴다. 항목의 값 유형과 일치해야 한다."""
    name = item["item_name"]
    if item["value_type"] == "NUMBER" and body.value is None:
        raise HTTPException(422, f"'{name}'은(는) 숫자형 항목입니다. value에 숫자를 입력하세요")
    if item["value_type"] == "TEXT" and body.value_text is None:
        raise HTTPException(422, f"'{name}'은(는) 문자형 항목입니다. value_text에 문자열을 입력하세요")
    conn.execute(
        """INSERT INTO checkup_results (user_id, item_id, date, value, value_text, note)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (user_id, item_id, date)
           DO UPDATE SET value = excluded.value,
                         value_text = excluded.value_text,
                         note = excluded.note""",
        (user_id, item["id"], day, body.value, body.value_text, body.note.strip()),
    )
    row = conn.execute(
        "SELECT * FROM checkup_results WHERE user_id = ? AND item_id = ? AND date = ?",
        (user_id, item["id"], day),
    ).fetchone()
    return dict(row)


@app.post("/api/users/{user_id}/items/{item_id}/results", status_code=201)
def upsert_result(user_id: int, item_id: int, body: ResultIn):
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        item = _get_or_404(conn, "checkup_items", item_id, "검진 항목")
        return _upsert_result(conn, user_id, item, body.date, body)


@app.get("/api/users/{user_id}/results")
def list_results_by_date(user_id: int, date: str):
    """특정 날짜에 기록된 모든 항목의 결과 (일괄 입력 화면의 기존 값 채우기용)."""
    try:
        _valid_iso(date, "날짜")
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        rows = conn.execute(
            "SELECT * FROM checkup_results WHERE user_id = ? AND date = ? ORDER BY item_id",
            (user_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/users/{user_id}/results/batch", status_code=201)
def upsert_results_batch(user_id: int, body: BatchResultIn):
    """같은 날짜의 여러 항목 결과를 한 트랜잭션으로 저장. 하나라도 실패하면 전체 취소."""
    with db() as conn:
        _get_or_404(conn, "users", user_id, "사용자")
        saved = []
        for entry in body.entries:
            item = _get_or_404(conn, "checkup_items", entry.item_id, "검진 항목")
            saved.append(_upsert_result(conn, user_id, item, body.date, entry))
        return saved


@app.delete("/api/results/{result_id}", status_code=204)
def delete_result(result_id: int):
    with db() as conn:
        _get_or_404(conn, "checkup_results", result_id, "검진 결과")
        conn.execute("DELETE FROM checkup_results WHERE id = ?", (result_id,))


# ---------- CSV 가져오기 (spec/004) ----------

class CsvIn(BaseModel):
    csv: str
    keep_order: bool = False   # 항목 가져오기: 파일의 행 순서를 표시 순서(sort_order)로 저장


# 헤더로 파일 종류를 추정해, 종류를 잘못 고른 경우 모호한 오류 대신 한 줄로 안내한다
_KIND_SIGNATURE = {
    "categories": ("카테고리", {"name"}),
    "items": ("검진 항목", {"judgement_level", "value_type", "target_gender"}),
    "results": ("검진 결과", {"date", "value"}),
}


def _read_csv(text: str, required: list[str], kind: str) -> list[tuple[int, dict]]:
    """(행 번호, {열: 값}) 목록. 헤더 검사, 값 공백 제거, 빈 줄 제외."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    for other, (label, sig) in _KIND_SIGNATURE.items():
        if other != kind and sig & set(headers) and not (_KIND_SIGNATURE[kind][1] & set(headers)):
            raise HTTPException(422, [f"'{label}' 파일로 보입니다. 가져오기 종류를 '{label}'(으)로 바꾸세요"])
    missing = [c for c in required if c not in headers]
    if missing:
        raise HTTPException(422, [f"필요한 열이 없습니다: {', '.join(missing)}"])
    rows = []
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if any(row.values()):
            rows.append((reader.line_num, row))
    if not rows:
        raise HTTPException(422, ["데이터 줄이 없습니다"])
    return rows


def _validation_msgs(e: ValidationError) -> str:
    return "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())


def _suggest_color(label: str) -> str:
    """static/app.js의 suggestColor와 같은 키워드 규칙."""
    if "정상" in label:
        return "ok"
    if any(k in label for k in ("저", "낮")):
        return "lowish"
    if any(k in label for k in ("경계", "주의", "전단계", "전 단계", "양호")):
        return "warn"
    if any(k in label for k in ("위험", "비만", "고도", "이상", "높", "고")):
        return "danger"
    return "etc"


@app.post("/api/import/categories")
def import_categories(body: CsvIn):
    rows = _read_csv(body.csv, ["name"], "categories")
    errors, names = [], []
    for ln, row in rows:
        try:
            names.append(CategoryIn(name=row["name"]).name)
        except ValidationError as e:
            errors.append(f"{ln}행: {_validation_msgs(e)}")
    if errors:
        raise HTTPException(422, errors)
    created = updated = 0
    with db() as conn:
        for name in names:
            cur = conn.execute("INSERT OR IGNORE INTO checkup_categories (name) VALUES (?)", (name,))
            if cur.rowcount:
                created += 1
            else:
                updated += 1   # 이름뿐이라 바뀌는 건 없지만 '있던 것'으로 집계
    return {"created": created, "updated": updated}


@app.post("/api/import/items")
def import_items(body: CsvIn):
    rows = _read_csv(body.csv, ["item_name"], "items")
    errors: list[str] = []
    groups: dict[tuple[str, str], dict] = {}   # (이름, 성별) → {first_ln, fields, ranges}
    for ln, row in rows:
        key = (row["item_name"], row.get("target_gender") or "ALL")
        fields = {
            "item_name": row["item_name"],
            "category": row.get("category", ""),
            "unit": row.get("unit", ""),
            "value_type": row.get("value_type") or "NUMBER",
            "target_gender": key[1],
        }
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"ln": ln, "fields": fields, "ranges": []}
        elif g["fields"] != fields:
            errors.append(f"{ln}행: '{key[0]}' 항목의 카테고리/단위/값 유형이 {g['ln']}행과 다릅니다")
            continue
        level = row.get("judgement_level", "")
        has_range = any(row.get(c) for c in ("judgement_level", "min_value", "max_value", "color"))
        if not has_range:
            continue
        try:
            rng = {
                "judgement_level": level,
                "min_value": float(row["min_value"]) if row.get("min_value") else None,
                "max_value": float(row["max_value"]) if row.get("max_value") else None,
                "color": row.get("color") or _suggest_color(level),
            }
        except ValueError:
            errors.append(f"{ln}행: '{key[0]}' 항목의 min_value/max_value가 숫자가 아닙니다")
            g["bad"] = True
            continue
        g["ranges"].append(rng)

    items: list[ItemIn] = []
    for (name, _), g in groups.items():
        if g.get("bad"):
            continue
        try:
            f = dict(g["fields"]); category = f.pop("category")
            items.append((category, ItemIn(**f, ranges=g["ranges"])))
        except ValidationError as e:
            errors.append(f"{g['ln']}행: '{name}' {_validation_msgs(e)}")
    if errors:
        raise HTTPException(422, errors)

    created = updated = categories_created = 0
    item_ids: list[int] = []
    with db() as conn:
        for category, it in items:
            category_id = None
            if category:
                row = conn.execute("SELECT id FROM checkup_categories WHERE name = ?", (category,)).fetchone()
                if row:
                    category_id = row["id"]
                else:
                    category_id = conn.execute(
                        "INSERT INTO checkup_categories (name) VALUES (?)", (category,)).lastrowid
                    categories_created += 1
            existing = conn.execute(
                "SELECT * FROM checkup_items WHERE item_name = ? AND target_gender = ?",
                (it.item_name, it.target_gender),
            ).fetchone()
            try:
                _check_item_name_rule(conn, it.item_name, it.target_gender,
                                      exclude_id=existing["id"] if existing else None)
                if existing and existing["value_type"] != it.value_type and conn.execute(
                        "SELECT 1 FROM checkup_results WHERE item_id = ? LIMIT 1", (existing["id"],)).fetchone():
                    raise HTTPException(409, "이미 기록이 있는 항목의 값 유형은 변경할 수 없습니다")
            except HTTPException as e:
                raise HTTPException(422, [f"'{it.item_name}': {e.detail}"])
            if existing:
                item_id = existing["id"]
                conn.execute(
                    """UPDATE checkup_items
                          SET unit = ?, value_type = ?, category_id = ? WHERE id = ?""",
                    (it.unit.strip(), it.value_type, category_id, item_id))
                conn.execute("DELETE FROM checkup_item_ranges WHERE item_id = ?", (item_id,))
                updated += 1
            else:
                item_id = conn.execute(
                    """INSERT INTO checkup_items (item_name, unit, value_type, target_gender, category_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (it.item_name, it.unit.strip(), it.value_type, it.target_gender, category_id)).lastrowid
                created += 1
            for r in it.ranges:
                conn.execute(
                    """INSERT INTO checkup_item_ranges (item_id, min_value, max_value, judgement_level, color)
                       VALUES (?, ?, ?, ?, ?)""",
                    (item_id, r.min_value, r.max_value, r.judgement_level, r.color))
            item_ids.append(item_id)
        if body.keep_order:
            # 항목은 파일 행 순서, 카테고리는 파일에 처음 등장한 순서. 파일에 없는 것은 건드리지 않는다.
            for i, item_id in enumerate(item_ids):
                conn.execute("UPDATE checkup_items SET sort_order = ? WHERE id = ?", (i, item_id))
            seen = list(dict.fromkeys(category for category, _ in items if category))
            for i, name in enumerate(seen):
                conn.execute("UPDATE checkup_categories SET sort_order = ? WHERE name = ?", (i, name))
    return {"created": created, "updated": updated, "categories_created": categories_created}


@app.post("/api/users/{user_id}/import/results")
def import_results(user_id: int, body: CsvIn):
    rows = _read_csv(body.csv, ["date", "item_name", "value"], "results")
    with db() as conn:
        user = _get_or_404(conn, "users", user_id, "사용자")
        visible = {
            r["item_name"]: r for r in conn.execute(
                "SELECT * FROM checkup_items WHERE target_gender IN ('ALL', ?)", (user["gender"],))
        }
        errors, entries, seen = [], [], {}
        for ln, row in rows:
            item = visible.get(row["item_name"])
            if not item:
                errors.append(f"{ln}행: '{row['item_name']}' 항목이 없습니다")
                continue
            dup_key = (item["id"], row["date"])
            if dup_key in seen:
                errors.append(f"{ln}행: '{row['item_name']}' {row['date']} 결과가 {seen[dup_key]}행에도 있습니다")
                continue
            seen[dup_key] = ln
            if not row["value"]:
                errors.append(f"{ln}행: '{row['item_name']}' 값이 비어 있습니다")
                continue
            kw = {"date": row["date"], "note": row.get("note", "")}
            if item["value_type"] == "NUMBER":
                try:
                    kw["value"] = float(row["value"])
                except ValueError:
                    errors.append(f"{ln}행: '{row['item_name']}' 숫자형 항목인데 값이 숫자가 아닙니다")
                    continue
            else:
                kw["value_text"] = row["value"]
            try:
                entries.append((item, ResultIn(**kw)))
            except ValidationError as e:
                errors.append(f"{ln}행: {_validation_msgs(e)}")
        if errors:
            raise HTTPException(422, errors)
        created = updated = 0
        for item, value in entries:
            exists = conn.execute(
                "SELECT 1 FROM checkup_results WHERE user_id = ? AND item_id = ? AND date = ?",
                (user_id, item["id"], value.date)).fetchone()
            _upsert_result(conn, user_id, item, value.date, value)
            if exists:
                updated += 1
            else:
                created += 1
        return {"created": created, "updated": updated}


# ---------- CSV 내보내기 (spec/006) — 가져오기와 같은 형식, 그대로 다시 가져올 수 있다 ----------

def _fmt_num(v):
    return "" if v is None else f"{v:g}"


def _csv_response(filename: str, header: list[str], rows) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        "\ufeff" + buf.getvalue(),   # BOM: 엑셀이 한글을 바로 읽도록
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},   # 한글 파일명
    )


@app.get("/api/export/categories.csv")
def export_categories():
    with db() as conn:
        rows = conn.execute(
            "SELECT name FROM checkup_categories ORDER BY sort_order IS NULL, sort_order, name COLLATE NOCASE, id")
        return _csv_response("categories.csv", ["name"], ([r["name"]] for r in rows))


@app.get("/api/export/items.csv")
def export_items():
    with db() as conn:
        items = conn.execute(
            """SELECT i.*, c.name AS category FROM checkup_items i
                 LEFT JOIN checkup_categories c ON c.id = i.category_id
                ORDER BY i.sort_order IS NULL, i.sort_order, i.item_name COLLATE NOCASE, i.id""").fetchall()
        out = []
        for it in items:
            base = [it["item_name"], it["category"] or "", it["unit"], it["value_type"], it["target_gender"]]
            ranges = conn.execute(
                """SELECT judgement_level, min_value, max_value, color FROM checkup_item_ranges
                    WHERE item_id = ? ORDER BY COALESCE(min_value, -1e308)""", (it["id"],)).fetchall()
            if not ranges:
                out.append(base + ["", "", "", ""])
            for r in ranges:
                out.append(base + [r["judgement_level"], _fmt_num(r["min_value"]), _fmt_num(r["max_value"]), r["color"]])
        return _csv_response(
            "items.csv",
            ["item_name", "category", "unit", "value_type", "target_gender",
             "judgement_level", "min_value", "max_value", "color"], out)


@app.get("/api/users/{user_id}/export/results.csv")
def export_results(user_id: int):
    with db() as conn:
        user = _get_or_404(conn, "users", user_id, "사용자")
        rows = conn.execute(
            """SELECT r.date, i.item_name, r.value, r.value_text, r.note
                 FROM checkup_results r JOIN checkup_items i ON i.id = r.item_id
                WHERE r.user_id = ?
                ORDER BY i.sort_order IS NULL, i.sort_order, i.item_name COLLATE NOCASE, i.id, r.date""",
            (user_id,))
        out = ([r["date"], r["item_name"], _fmt_num(r["value"]) if r["value_text"] is None else r["value_text"], r["note"]]
               for r in rows)
        return _csv_response(f"results_{user['name']}_{date.today().isoformat()}.csv",
                             ["date", "item_name", "value", "note"], out)


# ---------- 정적 파일 ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/sw.js")
def service_worker():
    """루트에서 서빙해야 Service Worker scope가 '/'가 되어 앱 페이지를 제어한다."""
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """정적 파일은 항상 재검증(ETag → 304). 헤더가 없으면 브라우저가 휴리스틱으로 오래 캐시해
    배포 후 새 HTML에 옛 CSS/JS가 섞여 보인다."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path == "/sw.js" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
