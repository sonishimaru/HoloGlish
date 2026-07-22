// HoloGlish フロントエンド
// - /api/search を叩いて結果を表示
// - YouTube IFrame Player で該当秒から再生、prev/next で用例を巡回、連続再生に対応
// - 再生速度・ループ・リプレイ・キーボード操作・前後トランスクリプト（/api/context）
// - 検索条件は URL ハッシュに反映され、共有・リロードで復元できる

const state = {
  results: [],
  index: -1,
  page: 1,
  pageSize: 20,
  total: 0,
  query: "",
  sort: "date",
  speed: 1,
  loop: false,
  player: null,
  playerReady: false,
  pollTimer: null,
  ctxToken: 0,
  pendingClip: null, // 共有リンクで指定された用例 {v, t}
};

const $ = (id) => document.getElementById(id);

// ---------- YouTube IFrame API ----------
window.onYouTubeIframeAPIReady = function () {
  state.player = new YT.Player("player", {
    playerVars: { playsinline: 1, rel: 0 },
    events: {
      onReady: () => {
        state.playerReady = true;
        applySpeed();
      },
    },
  });
};

function applySpeed() {
  if (state.playerReady && state.player && state.player.setPlaybackRate) {
    try { state.player.setPlaybackRate(state.speed); } catch (_) { /* noop */ }
  }
}

function playCurrent() {
  const r = state.results[state.index];
  if (!r) return;
  $("player-area").classList.remove("hidden");
  renderNowPlaying(r);
  markActiveRow();
  loadContext(r);
  writeHash(); // 再生中の用例を共有URLへ反映

  const startSeconds = Math.max(0, Math.floor(r.start));
  const doLoad = () => {
    state.player.loadVideoById({ videoId: r.video_id, startSeconds });
    applySpeed();
  };
  if (state.playerReady && state.player && state.player.loadVideoById) {
    doLoad();
  } else {
    // プレイヤー未準備なら準備でき次第再生
    const wait = setInterval(() => {
      if (state.playerReady) { clearInterval(wait); doLoad(); }
    }, 120);
  }
  startSegmentWatch(r);
}

// セグメント終端の監視: ループなら先頭へ、連続再生なら次の用例へ
function startSegmentWatch(r) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  const startSeconds = Math.max(0, Math.floor(r.start));
  const end = r.start + Math.max(r.dur || 0, 2) + 1.0; // 余韻を少し
  state.pollTimer = setInterval(() => {
    if (!state.playerReady || !state.player.getCurrentTime) return;
    const t = state.player.getCurrentTime();
    if (t >= end) {
      if (state.loop) {
        state.player.seekTo(startSeconds, true); // 同じ用例を繰り返す
        return;
      }
      clearInterval(state.pollTimer);
      if ($("autoplay").checked && state.index < state.results.length - 1) {
        goNext();
      }
    }
  }, 300);
}

function replayCurrent() {
  const r = state.results[state.index];
  if (!r || !state.playerReady) return;
  state.player.seekTo(Math.max(0, Math.floor(r.start)), true);
  state.player.playVideo();
  startSegmentWatch(r);
}

// 再生中の用例へのリンクをコピー（YouGlish の共有相当）
async function shareCurrent() {
  if (state.index < 0) return;
  writeHash(); // 最新のクリップ情報を URL に反映
  const url = location.href;
  const btn = $("share-btn");
  const done = (msg) => { btn.textContent = msg; setTimeout(() => { btn.textContent = "🔗 共有"; }, 1500); };
  try {
    await navigator.clipboard.writeText(url);
    done("✓ コピーしました");
  } catch (_) {
    // clipboard 不可の環境ではプロンプトで手動コピー
    window.prompt("この用例へのリンク:", url);
  }
}

function togglePlay() {
  if (!state.playerReady || !state.player.getPlayerState) return;
  const s = state.player.getPlayerState();
  if (s === YT.PlayerState.PLAYING) state.player.pauseVideo();
  else state.player.playVideo();
}

function goNext() {
  if (state.index < state.results.length - 1) { state.index++; playCurrent(); }
  else { loadPage(state.page + 1, true); } // 次ページの先頭へ
}
function goPrev() {
  if (state.index > 0) { state.index--; playCurrent(); }
}

// ---------- レンダリング ----------
// クエリ語の全出現箇所をハイライト（大小文字無視）
function highlight(text, q) {
  if (!q) return escapeHtml(text);
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  let out = "";
  let i = 0;
  while (i < text.length) {
    const hit = lower.indexOf(needle, i);
    if (hit < 0) { out += escapeHtml(text.slice(i)); break; }
    out += escapeHtml(text.slice(i, hit));
    out += "<mark>" + escapeHtml(text.slice(hit, hit + q.length)) + "</mark>";
    i = hit + q.length;
  }
  return out;
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(sec) {
  sec = Math.floor(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderResults() {
  const ul = $("results");
  ul.innerHTML = "";
  state.results.forEach((r, i) => {
    const li = document.createElement("li");
    li.className = "result";
    li.dataset.i = i;
    li.innerHTML = `
      <div class="time">${fmtTime(r.start)}</div>
      <div class="body">
        <div class="who">${escapeHtml(memberName(r))}
          <span class="badge">${escapeHtml(r.branch || "")}</span>
          <span class="badge">${escapeHtml(r.sub_kind || "")}</span>
        </div>
        <div class="snippet">${highlight(r.snippet || r.text || "", state.query)}</div>
      </div>`;
    li.addEventListener("click", () => { state.index = i; playCurrent(); });
    ul.appendChild(li);
  });
  markActiveRow();
}

function markActiveRow() {
  document.querySelectorAll(".result").forEach((el) => {
    el.classList.toggle("active", Number(el.dataset.i) === state.index);
  });
}

function renderNowPlaying(r) {
  $("now-member").textContent = `${memberName(r)} ・ ${r.branch || ""} ・ ${r.lang || ""}`;
  $("now-title").textContent = r.title || "";
  $("now-caption").innerHTML = highlight(r.text || "", state.query);
  $("counter").textContent = `${state.index + 1} / ${state.results.length}（全 ${state.total} 件）`;
  $("prev-btn").disabled = state.index <= 0;
}

// 前後のトランスクリプト（YouGlish 風）
async function loadContext(r) {
  const box = $("context");
  const token = ++state.ctxToken;
  box.innerHTML = "";
  try {
    const data = await Api.context({ video_id: r.video_id, start: r.start, window: 3 });
    if (token !== state.ctxToken) return; // 古いレスポンスは破棄
    (data.segments || []).forEach((s) => {
      const li = document.createElement("li");
      li.className = "ctx-line" + (s.is_current ? " current" : "");
      li.innerHTML = `<span class="ctx-t">${fmtTime(s.start)}</span>` +
        `<span class="ctx-x">${highlight(s.text || "", state.query)}</span>`;
      li.addEventListener("click", () => {
        if (state.playerReady) {
          state.player.seekTo(Math.max(0, Math.floor(s.start)), true);
          state.player.playVideo();
        }
      });
      box.appendChild(li);
    });
  } catch (_) { /* 文脈は補助情報なので失敗しても本体は動く */ }
}

function renderPager() {
  const pager = $("pager");
  const pages = Math.ceil(state.total / state.pageSize) || 1;
  pager.innerHTML = "";
  if (state.total === 0) return;
  const prev = document.createElement("button");
  prev.textContent = "← 前のページ";
  prev.disabled = state.page <= 1;
  prev.onclick = () => loadPage(state.page - 1);
  const info = document.createElement("span");
  info.textContent = ` ${state.page} / ${pages} `;
  info.style.alignSelf = "center";
  const next = document.createElement("button");
  next.textContent = "次のページ →";
  next.disabled = state.page >= pages;
  next.onclick = () => loadPage(state.page + 1);
  pager.append(prev, info, next);
}

// ---------- URL 同期（共有・復元） ----------
// includeClip=true のとき、再生中の用例(video_id + 秒)も URL に載せ、
// 共有リンクからその用例へ直接ジャンプできるようにする（YouGlish 風）。
function writeHash(includeClip = true) {
  const p = new URLSearchParams();
  if (state.query) p.set("q", state.query);
  const branch = $("f-branch").value, member = $("f-member").value, lang = $("f-lang").value;
  if (branch) p.set("branch", branch);
  if (member) p.set("member", member);
  if (lang) p.set("lang", lang);
  if (state.sort && state.sort !== "date") p.set("sort", state.sort);
  const r = includeClip ? state.results[state.index] : null;
  if (r) {
    p.set("v", r.video_id);
    p.set("t", Math.floor(r.start));
  }
  const s = p.toString();
  const next = s ? `#${s}` : "#";
  if (location.hash !== next) history.replaceState(null, "", next);
}

function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ""));
  return {
    q: p.get("q") || "",
    branch: p.get("branch") || "",
    member: p.get("member") || "",
    lang: p.get("lang") || "",
    sort: p.get("sort") || "date",
    v: p.get("v") || "",
    t: p.get("t") || "",
  };
}

// ---------- API ----------
async function loadPage(page, playFirst = false) {
  page = Math.max(1, page);
  const branch = $("f-branch").value, member = $("f-member").value, lang = $("f-lang").value;

  writeHash();
  hideLanding();
  $("status").textContent = "検索中…";
  const data = await Api.search({
    q: state.query, page, page_size: state.pageSize,
    branch, member, lang, sort: state.sort,
  });

  state.results = data.results || [];
  state.total = data.total || 0;
  state.page = data.page || page;
  state.index = -1;

  $("status").textContent = state.total
    ? `「${state.query}」の用例: ${state.total} 件`
    : `「${state.query}」は見つかりませんでした`;

  renderResults();
  renderPager();

  // 共有リンク由来の用例指定があれば、その用例を選んで再生
  let startIndex = -1;
  if (state.pendingClip && state.results.length) {
    const { v, t } = state.pendingClip;
    startIndex = state.results.findIndex(
      (r) => r.video_id === v && Math.abs(Math.floor(r.start) - t) <= 1
    );
    state.pendingClip = null;
  }

  if (state.results.length && startIndex >= 0) {
    state.index = startIndex;
    playCurrent();
  } else if (state.results.length && (playFirst || page === 1)) {
    state.index = 0;
    playCurrent();
  } else if (!state.results.length) {
    $("player-area").classList.add("hidden");
  }
}

async function doSearch(e) {
  if (e) e.preventDefault();
  state.query = $("q").value.trim();
  if (!state.query) return;
  await loadPage(1, true);
}

async function loadFacets() {
  try {
    const data = await Api.facets();
    fill($("f-branch"), data.branches, "全ブランチ");
    fill($("f-member"), data.members, "全メンバー");
    fill($("f-lang"), data.langs, "全言語");
  } catch (_) { /* DB 未生成でも UI は動く */ }
}
function fill(sel, items, allLabel) {
  const keep = sel.value;
  sel.innerHTML = `<option value="">${allLabel}</option>`;
  (items || []).forEach((v) => {
    // 文字列（ブランチ・言語）と {value,label}（メンバー）の両方を許容
    const value = (v && typeof v === "object") ? v.value : v;
    const label = (v && typeof v === "object") ? v.label : v;
    const o = document.createElement("option");
    o.value = value; o.textContent = label; sel.appendChild(o);
  });
  if (keep) sel.value = keep;
}

// 表示名（日本語優先、無ければ英語表記）
function memberName(r) {
  return (r && (r.member_ja || r.member)) || "";
}

// ---------- ランディング（検索前）: カバレッジ統計 + おすすめ検索 ----------
const SUGGESTED = ["おはよ", "ありがと", "ぺこ", "こんにちは", "hello", "です", "配信", "ました"];

async function loadLanding() {
  const landing = $("landing");
  const sug = $("suggestions");
  sug.innerHTML = "";
  SUGGESTED.forEach((w) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = w;
    b.addEventListener("click", () => {
      $("q").value = w;
      doSearch();
    });
    sug.appendChild(b);
  });
  try {
    const s = await Api.stats();
    if (s.videos > 0) {
      $("coverage").textContent =
        `${s.members} メンバー・${s.videos} 本の配信・${s.segments.toLocaleString()} 発話から検索`;
    } else {
      $("coverage").textContent =
        "まだ字幕が収集されていません（pipeline.run collect / ingest で取り込めます）";
    }
  } catch (_) { /* 統計は補助情報 */ }
  landing.classList.remove("hidden");
}

function hideLanding() {
  $("landing").classList.add("hidden");
}

// ---------- キーボード操作 ----------
function onKey(e) {
  // 入力欄では横取りしない（← → で文字カーソルを動かせるように）
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (state.index < 0) return;
  switch (e.key) {
    case "ArrowRight": e.preventDefault(); goNext(); break;
    case "ArrowLeft": e.preventDefault(); goPrev(); break;
    case " ": e.preventDefault(); togglePlay(); break;
    case "r": case "R": e.preventDefault(); replayCurrent(); break;
    case "l": case "L":
      e.preventDefault();
      $("loop").checked = !$("loop").checked;
      state.loop = $("loop").checked;
      break;
  }
}

// ---------- 初期化 ----------
function init() {
  const h = readHash();
  if (h.sort === "relevance") { state.sort = "relevance"; $("f-sort").value = "relevance"; }

  $("search-form").addEventListener("submit", doSearch);
  $("next-btn").addEventListener("click", goNext);
  $("prev-btn").addEventListener("click", goPrev);
  $("replay-btn").addEventListener("click", replayCurrent);
  $("share-btn").addEventListener("click", shareCurrent);
  $("loop").addEventListener("change", () => { state.loop = $("loop").checked; });
  $("speed").addEventListener("change", () => {
    state.speed = parseFloat($("speed").value) || 1;
    applySpeed();
  });
  $("f-sort").addEventListener("change", () => {
    state.sort = $("f-sort").value;
    if (state.query) loadPage(1, true);
  });
  ["f-branch", "f-member", "f-lang"].forEach((id) =>
    $(id).addEventListener("change", () => { if (state.query) loadPage(1, true); }));
  document.addEventListener("keydown", onKey);

  // 共有リンクで用例が指定されていれば、検索後にその用例へジャンプする
  if (h.v && h.t) state.pendingClip = { v: h.v, t: parseInt(h.t, 10) || 0 };

  loadFacets().then(() => {
    // ハッシュにフィルタがあれば復元して自動検索
    if (h.branch) $("f-branch").value = h.branch;
    if (h.member) $("f-member").value = h.member;
    if (h.lang) $("f-lang").value = h.lang;
    if (h.q) {
      $("q").value = h.q;
      state.query = h.q;
      loadPage(1, true);
    } else {
      loadLanding(); // 検索前はカバレッジ統計とおすすめ検索を表示
    }
  });
}

init();
