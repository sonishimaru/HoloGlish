// 検索バックエンドの抽象。
// - サーバモード（既定）: FastAPI の /api/* を叩く。
// - 静的モード: config.js が window.HOLOGLISH_DATA_URL を設定していると、
//   data.json を読み込んでブラウザ内で検索する（サーバ不要）。
//
// どちらのモードでも Api.search / Api.context / Api.facets / Api.stats は
// server/search.py と同じ形の結果を返す。

const Api = (function () {
  const qs = (params) => {
    const p = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") p.set(k, v);
    });
    return p.toString();
  };

  // ---------- サーバモード ----------
  if (!window.HOLOGLISH_DATA_URL) {
    return {
      mode: "server",
      async search(p) { return (await fetch(`/api/search?${qs(p)}`)).json(); },
      async context(p) { return (await fetch(`/api/context?${qs(p)}`)).json(); },
      async facets() { return (await fetch("/api/facets")).json(); },
      async stats() { return (await fetch("/api/stats")).json(); },
    };
  }

  // ---------- 静的モード ----------
  let store = null;

  async function load() {
    if (store) return store;
    const d = await (await fetch(window.HOLOGLISH_DATA_URL)).json();
    // segments を rowid 相当の配列で保持。videos メタは辞書。
    store = {
      videos: d.videos || {},
      segments: d.segments || [],
      facets: d.facets || { members: [], branches: [], langs: [] },
      stats: d.stats || { videos: 0, segments: 0, members: 0, by_branch: {} },
      // 動画ごとの segment index（context 用）
      byVideo: null,
    };
    return store;
  }

  // server/search.py の _highlight_snippet 相当
  function snippet(text, query, radius = 40) {
    const idx = text.toLowerCase().indexOf(query.toLowerCase());
    if (idx < 0) return text.slice(0, radius * 2);
    const start = Math.max(0, idx - radius);
    const end = Math.min(text.length, idx + query.length + radius);
    return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
  }

  function matches(seg, v, q, f) {
    if (f.member && v.member !== f.member) return false;
    if (f.branch && v.branch !== f.branch) return false;
    if (f.lang && seg.l !== f.lang) return false;
    return seg.t.toLowerCase().includes(q);
  }

  async function search(p) {
    const s = await load();
    const query = (p.q || "").trim();
    const sort = p.sort === "relevance" ? "relevance" : "date";
    const page = Math.max(1, parseInt(p.page || 1, 10));
    const pageSize = Math.min(Math.max(parseInt(p.page_size || 20, 10), 1), 100);
    if (!query) return { query, page, page_size: pageSize, total: 0, sort, results: [] };

    const q = query.toLowerCase();
    const f = { member: p.member || "", branch: p.branch || "", lang: p.lang || "" };

    const hits = [];
    for (const seg of s.segments) {
      const v = s.videos[seg.v];
      if (!v) continue;
      if (matches(seg, v, q, f)) hits.push({ seg, v });
    }

    // 並び替え（server/search.py に対応）
    if (sort === "relevance") {
      // 語が目立つ短い発話を優先（LIKE 経路の近似と同じ）
      hits.sort((a, b) => a.seg.t.length - b.seg.t.length ||
        (b.v.published_at || "").localeCompare(a.v.published_at || ""));
    } else {
      hits.sort((a, b) => (b.v.published_at || "").localeCompare(a.v.published_at || "") ||
        a.seg.s - b.seg.s);
    }

    const total = hits.length;
    const offset = (page - 1) * pageSize;
    const results = hits.slice(offset, offset + pageSize).map(({ seg, v }) => ({
      video_id: seg.v,
      member: v.member,
      member_ja: v.member_ja || "",
      branch: v.branch,
      title: v.title,
      url: v.url,
      lang: seg.l,
      sub_kind: v.sub_kind,
      start: seg.s,
      dur: seg.d,
      text: seg.t,
      snippet: snippet(seg.t, query),
    }));
    return { query, page, page_size: pageSize, total, sort, results };
  }

  async function context(p) {
    const s = await load();
    const videoId = p.video_id;
    const start = parseFloat(p.start || 0);
    let window_ = parseInt(p.window || 3, 10);
    window_ = Math.max(0, Math.min(window_, 20));

    const v = s.videos[videoId] || null;
    if (!v) return { video_id: videoId, video: null, segments: [] };

    if (!s.byVideo) {
      s.byVideo = {};
      for (const seg of s.segments) (s.byVideo[seg.v] = s.byVideo[seg.v] || []).push(seg);
      for (const k in s.byVideo) s.byVideo[k].sort((a, b) => a.s - b.s);
    }
    const rows = s.byVideo[videoId] || [];
    if (!rows.length) return { video_id: videoId, video: { member: v.member, ...v }, segments: [] };

    let center = 0, best = Infinity;
    rows.forEach((r, i) => { const d = Math.abs(r.s - start); if (d < best) { best = d; center = i; } });
    const lo = Math.max(0, center - window_);
    const hi = Math.min(rows.length, center + window_ + 1);
    const segments = rows.slice(lo, hi).map((r, i) => ({
      start: r.s, dur: r.d, text: r.t, is_current: (lo + i) === center,
    }));
    return { video_id: videoId, video: v, segments };
  }

  async function facets() { return (await load()).facets; }
  async function stats() { return (await load()).stats; }

  return { mode: "static", search, context, facets, stats };
})();

window.Api = Api;
