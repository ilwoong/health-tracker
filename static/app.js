/* ===== 상태 ===== */
let users = [];
let currentUser = null;
let items = [];          // 대시보드 요약 (ranges 포함)
let currentItem = null;
let editingUserId = null;
let editingItemId = null;
let chart = null;
let period = { preset: "all", from: null, to: null };

const LS_KEY = "ht_user_id";

/* ===== 공통 ===== */
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let msg = "요청에 실패했습니다";
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : (j.detail?.[0]?.msg ?? msg);
    } catch {}
    alert(msg);
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function fmt(v) {
  if (v == null) return "";
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}

/* 브라우저 로컬 시간대 기준 YYYY-MM-DD (toISOString은 UTC라 KST 오전 9시 전에는 어제가 됨) */
function toLocalISO(d) {
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function ageOf(birth) {
  const b = new Date(birth), t = new Date();
  let a = t.getFullYear() - b.getFullYear();
  if (t.getMonth() < b.getMonth() || (t.getMonth() === b.getMonth() && t.getDate() < b.getDate())) a--;
  return a;
}

const GENDER_LABEL = { M: "남", F: "여", ALL: "전체" };

/* ===== 판정 ===== */
/* 값이 속하는 구간 객체(judgement_level, color 포함)를 반환.
   구간이 하나(정상)만 정의된 항목은 범위 밖을 '위험'으로 판정한다. */
const OUT_OF_RANGE = { judgement_level: "위험", color: "danger", synthetic: true };

function judgeRange(value, ranges) {
  if (value == null || !ranges?.length) return null;
  for (const r of ranges) {
    const okMin = r.min_value == null || value >= r.min_value;
    const okMax = r.max_value == null || value <= r.max_value;
    if (okMin && okMax) return r;
  }
  if (ranges.length === 1) return OUT_OF_RANGE;
  return null;
}

/* 라벨 키워드 → 색상 제안 (항목 폼에서 초기값 제안 용도로만 사용) */
function suggestColor(label) {
  if (!label) return "etc";
  if (/정상/.test(label)) return "ok";
  if (/(저|낮)/.test(label)) return "lowish";
  if (/(경계|주의|전단계|전 단계|양호)/.test(label)) return "warn";
  if (/(위험|비만|고도|이상|높|고)/.test(label)) return "danger";
  return "etc";
}

const COLOR_RGB = {
  ok:     "14, 124, 107",
  warn:   "180, 83, 9",
  danger: "192, 57, 43",
  lowish: "37, 99, 235",
  etc:    "107, 114, 128",
};

const COLOR_LABEL = { ok: "청록", lowish: "파랑", warn: "주황", danger: "빨강", etc: "회색" };

function badgeHtml(range) {
  if (!range) return "";
  return `<span class="badge lv-${range.color}">${escapeHtml(range.judgement_level)}</span>`;
}

/* ===== 화면 전환 ===== */
function show(viewId) {
  for (const id of ["view-login", "view-dashboard", "view-detail", "view-batch"]) {
    document.getElementById(id).hidden = id !== viewId;
  }
}

/* ===== 로그인 화면 ===== */
async function loadLogin() {
  users = await api("/api/users");
  if (users.length === 0) {
    // 최초 접속: 사용자가 없으면 바로 추가 폼 노출
    show("view-login");
    document.getElementById("user-list").innerHTML = "";
    openUserForm();
    return;
  }
  show("view-login");
  const list = document.getElementById("user-list");
  list.innerHTML = "";
  for (const u of users) {
    const row = document.createElement("div");
    row.className = "user-row";
    row.innerHTML = `
      <button class="user-enter">
        <span class="avatar">${escapeHtml(u.name.slice(0, 1))}</span>
        <span class="user-meta">
          <div class="name">${escapeHtml(u.name)}</div>
          <div class="info">${GENDER_LABEL[u.gender]} · ${ageOf(u.birth_date)}세</div>
        </span>
      </button>
      <span class="user-manage">
        <button class="btn btn-ghost btn-small">수정</button>
        <button class="btn btn-ghost btn-small btn-danger">삭제</button>
      </span>`;
    row.querySelector(".user-enter").onclick = () => login(u);
    const [editBtn, delBtn] = row.querySelectorAll(".user-manage button");
    editBtn.onclick = () => openUserForm(u);
    delBtn.onclick = async () => {
      if (!confirm(`'${u.name}' 사용자와 모든 검진 기록을 삭제할까요?`)) return;
      await api(`/api/users/${u.id}`, { method: "DELETE" });
      if (localStorage.getItem(LS_KEY) == u.id) localStorage.removeItem(LS_KEY);
      loadLogin();
    };
    list.appendChild(row);
  }
}

function login(user) {
  currentUser = user;
  localStorage.setItem(LS_KEY, user.id);
  showDashboard();
}

function logout() {
  currentUser = null;
  localStorage.removeItem(LS_KEY);
  loadLogin();
}

/* ===== 대시보드 ===== */
async function showDashboard() {
  show("view-dashboard");
  document.getElementById("dash-username").textContent = `${currentUser.name}님`;
  document.getElementById("dash-userinfo").textContent =
    `${GENDER_LABEL[currentUser.gender]} · ${ageOf(currentUser.birth_date)}세`;
  items = await api(`/api/users/${currentUser.id}/summary`);
  renderDashboard();
}

function renderDashboard() {
  const q = document.getElementById("item-search").value.trim().toLowerCase();
  const filtered = items.filter((i) => i.item_name.toLowerCase().includes(q));

  const grid = document.getElementById("item-cards");
  grid.innerHTML = "";
  document.getElementById("dash-empty").hidden = !(items.length === 0);

  for (const it of filtered) {
    const isText = it.value_type === "TEXT";
    const matched = isText ? null : judgeRange(it.latest_value, it.ranges);
    const card = document.createElement("button");
    card.className = "metric-card";
    card.onclick = () => showDetail(it.id);
    let valueHtml;
    if (isText) {
      valueHtml = it.latest_value_text != null
        ? `<span class="text-value">${escapeHtml(it.latest_value_text)}</span>`
        : `<span class="no-data">기록 없음</span>`;
    } else {
      valueHtml = it.latest_value != null
        ? `${fmt(it.latest_value)}<span class="unit">${escapeHtml(it.unit)}</span>`
        : `<span class="no-data">기록 없음</span>`;
    }
    const genderTag = it.target_gender !== "ALL"
      ? `<span class="gender-tag">${GENDER_LABEL[it.target_gender]}</span>` : "";
    card.innerHTML = `
      <div class="card-top">
        <span class="card-name">${escapeHtml(it.item_name)}${genderTag}</span>
        <span class="card-date">${it.latest_date ?? ""}</span>
      </div>
      <div class="card-bottom">
        <div class="card-value">${valueHtml}</div>
        ${badgeHtml(matched)}
      </div>`;
    grid.appendChild(card);
  }
}

/* ===== 상세 화면 ===== */
async function showDetail(itemId) {
  currentItem = items.find((i) => i.id === itemId);
  if (!currentItem) return;

  show("view-detail");
  renderDetailHeader();

  // 기간 초기화: 전체
  period = { preset: "all", from: null, to: null };
  document.getElementById("p-from").value = "";
  document.getElementById("p-to").value = "";
  updatePresetChips();

  document.getElementById("r-date").value = toLocalISO(new Date());
  const vInput = document.getElementById("r-value");
  if (currentItem.value_type === "TEXT") {
    vInput.type = "text";
    vInput.removeAttribute("step");
    vInput.removeAttribute("inputmode");
    vInput.placeholder = "예: 음성";
  } else {
    vInput.type = "number";
    vInput.setAttribute("step", "any");
    vInput.setAttribute("inputmode", "decimal");
    vInput.placeholder = "";
  }
  vInput.value = "";
  document.getElementById("r-note").value = "";

  await refreshResults();
}

function renderDetailHeader() {
  document.getElementById("detail-name").textContent =
    currentItem.item_name + (currentItem.unit ? ` (${currentItem.unit})` : "");
  let parts;
  if (currentItem.value_type === "TEXT") {
    parts = ["문자열 항목"];
  } else {
    parts = currentItem.ranges.map((r) => {
      let span;
      if (r.min_value != null && r.max_value != null) span = `${fmt(r.min_value)}~${fmt(r.max_value)}`;
      else if (r.max_value != null) span = `${fmt(r.max_value)} 이하`;
      else span = `${fmt(r.min_value)} 이상`;
      return `${r.judgement_level} ${span}`;
    });
    if (currentItem.ranges.length === 1) parts.push("범위 밖은 위험으로 판정");
  }
  if (currentItem.target_gender !== "ALL") {
    parts.push(`${GENDER_LABEL[currentItem.target_gender]}성 항목`);
  }
  document.getElementById("detail-ranges").textContent = parts.join(" · ");
}

/* ===== 기간 필터 ===== */
document.querySelectorAll(".period-presets .chip").forEach((chip) => {
  chip.onclick = () => {
    const p = chip.dataset.preset;
    period.preset = p;
    if (p === "all") {
      period.from = null;
      period.to = null;
    } else {
      const d = new Date();
      d.setFullYear(d.getFullYear() - Number(p));
      period.from = toLocalISO(d);
      period.to = null;
    }
    document.getElementById("p-from").value = period.from ?? "";
    document.getElementById("p-to").value = "";
    updatePresetChips();
    refreshResults();
  };
});

function onCustomPeriod() {
  period.preset = null;
  period.from = document.getElementById("p-from").value || null;
  period.to = document.getElementById("p-to").value || null;
  updatePresetChips();
  refreshResults();
}

function updatePresetChips() {
  document.querySelectorAll(".period-presets .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.preset === period.preset);
  });
}

async function refreshResults() {
  const params = new URLSearchParams();
  if (period.from) params.set("date_from", period.from);
  if (period.to) params.set("date_to", period.to);
  const qs = params.toString() ? `?${params}` : "";
  const rows = await api(`/api/users/${currentUser.id}/items/${currentItem.id}/results${qs}`);
  const isText = currentItem.value_type === "TEXT";
  document.getElementById("chart-wrap").hidden = isText;
  document.getElementById("chart-na").hidden = !isText;
  if (isText) {
    if (chart) { chart.destroy(); chart = null; }
  } else {
    renderChart(rows);
  }
  renderHistory(rows);
}

/* ===== 차트: 판정 구간을 색상 밴드로 표시 ===== */
const rangeBandPlugin = {
  id: "rangeBands",
  beforeDatasetsDraw(c, _args, opts) {
    const ranges = opts.ranges || [];
    if (!ranges.length) return;
    const { ctx, chartArea, scales } = c;
    const y = scales.y;
    ctx.save();
    for (const r of ranges) {
      const rgb = COLOR_RGB[r.color] || COLOR_RGB.etc;
      const topVal = r.max_value, botVal = r.min_value;
      let top = topVal != null ? y.getPixelForValue(topVal) : chartArea.top;
      let bottom = botVal != null ? y.getPixelForValue(botVal) : chartArea.bottom;
      top = Math.max(top, chartArea.top);
      bottom = Math.min(bottom, chartArea.bottom);
      if (bottom <= top) continue;
      ctx.fillStyle = `rgba(${rgb}, 0.08)`;
      ctx.fillRect(chartArea.left, top, chartArea.right - chartArea.left, bottom - top);
      // 구간 라벨
      ctx.fillStyle = `rgba(${rgb}, 0.75)`;
      ctx.font = "10px sans-serif";
      ctx.textBaseline = "top";
      ctx.fillText(r.judgement_level, chartArea.left + 4, top + 3);
      // 경계선
      ctx.strokeStyle = `rgba(${rgb}, 0.35)`;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1;
      for (const [val, yy] of [[topVal, top], [botVal, bottom]]) {
        if (val == null) continue;
        ctx.beginPath();
        ctx.moveTo(chartArea.left, yy);
        ctx.lineTo(chartArea.right, yy);
        ctx.stroke();
      }
    }
    ctx.restore();
  },
};

function renderChart(rows) {
  const it = currentItem;
  const ctx = document.getElementById("chart");
  if (chart) chart.destroy();

  // 구간이 정상 하나뿐이면 그 바깥을 '위험' 밴드로 표시
  let bandRanges = it.ranges;
  if (it.ranges.length === 1) {
    const r = it.ranges[0];
    const extra = [];
    if (r.max_value != null)
      extra.push({ min_value: r.max_value, max_value: null, ...OUT_OF_RANGE });
    if (r.min_value != null)
      extra.push({ min_value: null, max_value: r.min_value, ...OUT_OF_RANGE });
    bandRanges = [...it.ranges, ...extra];
  }

  const values = rows.map((r) => r.value);
  // y축 범위: 측정값 + 유한한 구간 경계 포함
  const bounds = [...values];
  for (const r of it.ranges) {
    if (r.min_value != null) bounds.push(r.min_value);
    if (r.max_value != null) bounds.push(r.max_value);
  }

  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [{
        data: values,
        borderColor: "#0E7C6B",
        pointRadius: 4,
        pointBackgroundColor: rows.map((r) => {
          const m = judgeRange(r.value, it.ranges);
          const rgb = m ? COLOR_RGB[m.color] : COLOR_RGB.etc;
          return `rgb(${rgb})`;
        }),
        tension: 0.25,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        rangeBands: { ranges: bandRanges },
        tooltip: {
          callbacks: {
            label: (c) => {
              const r = rows[c.dataIndex];
              const m = judgeRange(r.value, it.ranges);
              let s = `${fmt(r.value)} ${it.unit}`.trim();
              if (m) s += ` (${m.judgement_level})`;
              if (r.note) s += ` — ${r.note}`;
              return s;
            },
          },
        },
      },
      scales: {
        y: {
          suggestedMin: bounds.length ? Math.min(...bounds) : undefined,
          suggestedMax: bounds.length ? Math.max(...bounds) : undefined,
          grace: "10%",
          grid: { color: "#EAF0EF" },
          ticks: { font: { size: 11 } },
        },
        x: {
          grid: { display: false },
          ticks: { font: { size: 11 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 },
        },
      },
    },
    plugins: [rangeBandPlugin],
  });
}

/* ===== 기록 테이블 (최신이 위로) ===== */
function renderHistory(rows) {
  document.getElementById("history-count").textContent = rows.length ? `${rows.length}건` : "";
  document.getElementById("history-empty").hidden = rows.length > 0;
  const body = document.getElementById("history-body");
  body.innerHTML = "";
  for (const r of [...rows].reverse()) {
    const isText = currentItem.value_type === "TEXT";
    const matched = isText ? null : judgeRange(r.value, currentItem.ranges);
    const valueCell = isText ? escapeHtml(r.value_text ?? "") : fmt(r.value);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.date}</td>
      <td class="num">${valueCell}</td>
      <td>${badgeHtml(matched)}</td>
      <td class="note-cell">${escapeHtml(r.note)}</td>
      <td><button class="del-btn" title="삭제" aria-label="삭제">&times;</button></td>`;
    tr.querySelector(".del-btn").onclick = async () => {
      if (!confirm(`${r.date} 기록을 삭제할까요?`)) return;
      await api(`/api/results/${r.id}`, { method: "DELETE" });
      refreshResults();
    };
    body.appendChild(tr);
  }
}

/* ===== 검진 결과 입력 ===== */
document.getElementById("result-form").onsubmit = async (e) => {
  e.preventDefault();
  const raw = document.getElementById("r-value").value;
  const payload = {
    date: document.getElementById("r-date").value,
    note: document.getElementById("r-note").value,
  };
  if (currentItem.value_type === "TEXT") {
    payload.value_text = raw;
  } else {
    payload.value = parseFloat(raw);
  }
  await api(`/api/users/${currentUser.id}/items/${currentItem.id}/results`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  document.getElementById("r-value").value = "";
  document.getElementById("r-note").value = "";
  refreshResults();
};

/* ===== 검진 결과 일괄 입력 ===== */
let batchDate = null;   // 기존 값을 불러온 날짜 (날짜 변경 취소 시 되돌릴 값)

async function showBatch() {
  show("view-batch");
  document.getElementById("b-date").value = toLocalISO(new Date());
  document.getElementById("b-note").value = "";
  await loadBatchRows();
}

/* 항목별 입력 줄을 만들고, 선택한 날짜의 기존 기록을 미리 채운다 */
async function loadBatchRows() {
  batchDate = document.getElementById("b-date").value;
  const existing = batchDate
    ? await api(`/api/users/${currentUser.id}/results?date=${batchDate}`)
    : [];
  const byItem = new Map(existing.map((r) => [r.item_id, r]));

  const box = document.getElementById("batch-rows");
  box.innerHTML = "";
  document.getElementById("batch-empty").hidden = items.length > 0;

  for (const it of items) {
    const isText = it.value_type === "TEXT";
    const old = byItem.get(it.id);
    const row = document.createElement("div");
    row.className = "batch-row";
    row.dataset.itemId = it.id;
    row.dataset.existing = old ? "1" : "";
    row.dataset.origValue = old ? String(isText ? old.value_text : old.value) : "";
    row.dataset.origNote = old ? old.note : "";
    const genderTag = it.target_gender !== "ALL"
      ? `<span class="gender-tag">${GENDER_LABEL[it.target_gender]}</span>` : "";
    const inputAttrs = isText
      ? `type="text" placeholder="예: 음성"`
      : `type="number" step="any" inputmode="decimal"`;
    row.innerHTML = `
      <div class="br-name">${escapeHtml(it.item_name)}${genderTag}</div>
      <div class="br-value">
        <input class="br-input" ${inputAttrs} aria-label="${escapeHtml(it.item_name)} 값">
        <span class="unit">${escapeHtml(it.unit)}</span>
      </div>
      <div class="br-badge"></div>
      <input type="text" class="br-note" placeholder="항목별 비고" aria-label="${escapeHtml(it.item_name)} 비고">`;

    const input = row.querySelector(".br-input");
    input.value = row.dataset.origValue;
    row.querySelector(".br-note").value = row.dataset.origNote;
    input.addEventListener("input", () => {
      updateBatchBadge(row, it);
      updateBatchSaveButton();
    });
    row.querySelector(".br-note").addEventListener("input", updateBatchSaveButton);
    // Enter는 저장 대신 다음 항목의 값 입력칸으로 이동
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      row.nextElementSibling?.querySelector(".br-input").focus();
    });
    updateBatchBadge(row, it);
    box.appendChild(row);
  }
  updateBatchSaveButton();
}

function updateBatchBadge(row, it) {
  const raw = row.querySelector(".br-input").value;
  const matched = it.value_type === "TEXT" || raw === "" ? null : judgeRange(parseFloat(raw), it.ranges);
  row.querySelector(".br-badge").innerHTML = badgeHtml(matched);
}

function batchRowValues(row) {
  return {
    value: row.querySelector(".br-input").value.trim(),
    note: row.querySelector(".br-note").value.trim(),
  };
}

/* 저장 대상: 값이 있고, 기존 기록이 없거나 값/항목별 비고가 바뀐 줄 */
function isBatchRowToSave(row) {
  const { value, note } = batchRowValues(row);
  if (value === "") return false;
  return !row.dataset.existing || value !== row.dataset.origValue || note !== row.dataset.origNote;
}

/* 날짜 변경·화면 이탈 시 확인용: 불러온 상태에서 하나라도 바뀌었는지 */
function batchHasEdits() {
  return [...document.querySelectorAll("#batch-rows .batch-row")].some((row) => {
    const { value, note } = batchRowValues(row);
    return value !== row.dataset.origValue || note !== row.dataset.origNote;
  });
}

function updateBatchSaveButton() {
  const count = [...document.querySelectorAll("#batch-rows .batch-row")].filter(isBatchRowToSave).length;
  const btn = document.getElementById("b-save");
  btn.disabled = count === 0;
  btn.textContent = count ? `${count}개 항목 저장` : "저장";
}

async function onBatchDateChange() {
  const dateInput = document.getElementById("b-date");
  if (batchHasEdits() && !confirm("입력 중인 값이 있습니다. 날짜를 바꾸면 입력한 값이 사라집니다. 계속할까요?")) {
    dateInput.value = batchDate;
    return;
  }
  await loadBatchRows();
}

function leaveBatch() {
  if (batchHasEdits() && !confirm("입력한 값이 저장되지 않았습니다. 나갈까요?")) return;
  showDashboard();
}

document.getElementById("batch-form").onsubmit = async (e) => {
  e.preventDefault();
  const commonNote = document.getElementById("b-note").value.trim();
  const entries = [];
  for (const row of document.querySelectorAll("#batch-rows .batch-row")) {
    if (!isBatchRowToSave(row)) continue;
    const it = items.find((i) => i.id === Number(row.dataset.itemId));
    const { value, note } = batchRowValues(row);
    const entry = { item_id: it.id, note: note || commonNote };
    if (it.value_type === "TEXT") entry.value_text = value;
    else entry.value = parseFloat(value);
    entries.push(entry);
  }
  if (!entries.length) return;

  const btn = document.getElementById("b-save");
  btn.disabled = true;
  try {
    await api(`/api/users/${currentUser.id}/results/batch`, {
      method: "POST",
      body: JSON.stringify({ date: document.getElementById("b-date").value, entries }),
    });
  } catch {
    updateBatchSaveButton();
    return;
  }
  showDashboard();
};

/* ===== 사용자 추가/수정 모달 ===== */
function openUserForm(user = null) {
  editingUserId = user ? user.id : null;
  document.getElementById("user-form-title").textContent = user ? "사용자 수정" : "사용자 추가";
  document.getElementById("u-name").value = user ? user.name : "";
  document.getElementById("u-birth").value = user ? user.birth_date : "";
  document.querySelectorAll('input[name="u-gender"]').forEach((r) => {
    r.checked = user ? r.value === user.gender : false;
  });
  document.getElementById("user-dialog").showModal();
}

document.getElementById("user-form").onsubmit = async (e) => {
  e.preventDefault();
  const gender = document.querySelector('input[name="u-gender"]:checked')?.value;
  const body = JSON.stringify({
    name: document.getElementById("u-name").value,
    birth_date: document.getElementById("u-birth").value,
    gender,
  });
  if (editingUserId != null) {
    const updated = await api(`/api/users/${editingUserId}`, { method: "PUT", body });
    if (currentUser && currentUser.id === updated.id) currentUser = updated;
  } else {
    await api("/api/users", { method: "POST", body });
  }
  document.getElementById("user-dialog").close();
  loadLogin();
};

/* ===== 항목 추가/수정 모달 ===== */
function openItemForm(item = null) {
  editingItemId = item ? item.id : null;
  document.getElementById("item-form-title").textContent = item ? "검진 항목 수정" : "검진 항목 추가";
  document.getElementById("i-name").value = item ? item.item_name : "";
  document.getElementById("i-unit").value = item ? item.unit : "";
  document.getElementById("i-vtype").value = item ? item.value_type : "NUMBER";
  document.getElementById("i-gender").value = item ? item.target_gender : "ALL";

  const rowsBox = document.getElementById("range-rows");
  rowsBox.innerHTML = "";
  if (item && item.ranges.length) {
    for (const r of item.ranges) addRangeRow(r);
  } else {
    addRangeRow({ judgement_level: "정상" }); // 정상 구간은 기본 필수
  }
  onValueTypeChange();
  document.getElementById("item-dialog").showModal();
}

function onValueTypeChange() {
  const isText = document.getElementById("i-vtype").value === "TEXT";
  document.getElementById("ranges-section").hidden = isText;
  document.getElementById("text-type-hint").hidden = !isText;
  // 숨겨진 구간 입력이 required로 폼 제출을 막지 않도록 토글
  document.querySelectorAll("#range-rows .rr-level").forEach((el) => {
    el.required = !isText;
  });
}

function addRangeRow(range = null) {
  const rowsBox = document.getElementById("range-rows");
  const first = rowsBox.children.length === 0;
  const row = document.createElement("div");
  row.className = "range-row";
  const colorOptions = Object.entries(COLOR_LABEL)
    .map(([v, label]) =>
      `<option value="${v}" ${range && range.color === v ? "selected" : ""}>${label}</option>`)
    .join("");
  row.innerHTML = `
    <input type="text" class="rr-level" placeholder="판정 (예: 정상)" required
           value="${range ? escapeHtml(range.judgement_level) : ""}">
    <input type="number" class="rr-min" step="any" inputmode="decimal" placeholder="최소값"
           value="${range && range.min_value != null ? range.min_value : ""}">
    <input type="number" class="rr-max" step="any" inputmode="decimal" placeholder="최대값"
           value="${range && range.max_value != null ? range.max_value : ""}">
    <select class="rr-color" title="배지·그래프 색상">${colorOptions}</select>
    <button type="button" class="del-btn" title="구간 삭제" aria-label="구간 삭제" ${first ? "disabled" : ""}>&times;</button>`;

  const levelInput = row.querySelector(".rr-level");
  const colorSelect = row.querySelector(".rr-color");
  colorSelect.className = `rr-color color-${colorSelect.value}`;
  // 기존 구간이 아니면 라벨 키워드로 색상을 자동 제안 (사용자가 직접 고르기 전까지)
  if (!range) {
    colorSelect.value = suggestColor(levelInput.value);
    colorSelect.className = `rr-color color-${colorSelect.value}`;
    levelInput.addEventListener("input", () => {
      if (colorSelect.dataset.touched) return;
      colorSelect.value = suggestColor(levelInput.value);
      colorSelect.className = `rr-color color-${colorSelect.value}`;
    });
  }
  colorSelect.addEventListener("change", () => {
    colorSelect.dataset.touched = "1";
    colorSelect.className = `rr-color color-${colorSelect.value}`;
  });

  row.querySelector(".del-btn").onclick = () => row.remove();
  rowsBox.appendChild(row);
}

function editCurrentItem() { openItemForm(currentItem); }

async function deleteCurrentItem() {
  if (!confirm(`'${currentItem.item_name}' 항목과 모든 사용자의 관련 기록을 삭제할까요?`)) return;
  await api(`/api/items/${currentItem.id}`, { method: "DELETE" });
  showDashboard();
}

document.getElementById("item-form").onsubmit = async (e) => {
  e.preventDefault();
  const valueType = document.getElementById("i-vtype").value;
  const ranges = valueType === "TEXT" ? [] :
    [...document.querySelectorAll("#range-rows .range-row")].map((row) => {
      const minRaw = row.querySelector(".rr-min").value;
      const maxRaw = row.querySelector(".rr-max").value;
      return {
        judgement_level: row.querySelector(".rr-level").value.trim(),
        min_value: minRaw === "" ? null : parseFloat(minRaw),
        max_value: maxRaw === "" ? null : parseFloat(maxRaw),
        color: row.querySelector(".rr-color").value,
      };
    });
  const body = JSON.stringify({
    item_name: document.getElementById("i-name").value,
    unit: document.getElementById("i-unit").value,
    value_type: valueType,
    target_gender: document.getElementById("i-gender").value,
    ranges,
  });

  let saved;
  if (editingItemId != null) {
    saved = await api(`/api/items/${editingItemId}`, { method: "PUT", body });
  } else {
    saved = await api("/api/items", { method: "POST", body });
  }
  document.getElementById("item-dialog").close();

  items = await api(`/api/users/${currentUser.id}/summary`);
  if (currentItem && editingItemId === currentItem.id) {
    currentItem = items.find((i) => i.id === saved.id);
    if (!currentItem) { showDashboard(); return; } // 성별 변경으로 더 이상 안 보이는 경우
    renderDetailHeader();
    refreshResults();
  } else if (!document.getElementById("view-dashboard").hidden) {
    renderDashboard();
  }
};

/* ===== 시작 ===== */
(async function start() {
  users = await api("/api/users");
  const savedId = Number(localStorage.getItem(LS_KEY));
  const saved = users.find((u) => u.id === savedId);
  if (saved) {
    currentUser = saved;
    showDashboard();
  } else {
    loadLogin();
  }
})();
