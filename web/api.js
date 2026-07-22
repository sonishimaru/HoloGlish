// 検索バックエンドの抽象。
// - サーバモード（既定）: FastAPI の /api/* を叩く。
// - 静的モード: config.js が window.HOLOGLISH_INDEX_BASE を設定していると、
//   シャード化したトリグラム転置索引をブラウザ内で検索する（サーバ不要）。
//   クエリのトリグラムを含むシャードだけを取得するため、全アーカイブでも
//   索引全体をダウンロードしない。
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
  if (!window.HOLOGLISH_INDEX_BASE) {
    return {
      mode: "server",
      async search(p) { return (await fetch(`/api/search?${qs(p)}`)).json(); },
      async context(p) { return (await fetch(`/api/context?${qs(p)}`)).json(); },
      async facets() { return (await fetch("/api/facets")).json(); },
      async stats() { return (await fetch("/api/stats")).json(); },
    };
  }

  // ---------- 静的モード（シャード索引） ----------
  const BASE = window.HOLOGLISH_INDEX_BASE.replace(/\/$/, "");
  let manifest = null;         // {version, shards, videos, segments, facets, stats}
  let triIndex = null;         // { trigram: [shardBucket, ...] }
  const shardCache = new Map(); // b -> shard {vids, meta, segs, tri}

  async function load() {
    if (manifest) return;
    [manifest, triIndex] = await Promise.all([
      fetch(`${BASE}/manifest.json`).then((r) => r.json()),
      fetch(`${BASE}/tri-index.json`).then((r) => r.json()),
    ]);
  }

  async function getShard(b) {
    if (shardCache.has(b)) return shardCache.get(b);
    const sh = await fetch(`${BASE}/shard-${b}.json`).then((r) => r.json());
    shardCache.set(b, sh);
    return sh;
  }
  async function getShards(buckets) {
    return Promise.all(buckets.map(getShard));
  }

  // Python 側 _trigrams と同一定義（小文字化・3文字窓・相異なる）
  function trigrams(text) {
    const t = text.toLowerCase();
    const s = new Set();
    for (let i = 0; i + 3 <= t.length; i++) s.add(t.slice(i, i + 3));
    return [...s];
  }

  // server/search.py の _highlight_snippet 相当
  function snippet(text, query, radius = 40) {
    const idx = text.toLowerCase().indexOf(query.toLowerCase());
    if (idx < 0) return text.slice(0, radius * 2);
    const start = Math.max(0, idx - radius);
    const end = Math.min(text.length, idx + query.length + radius);
    return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
  }

  function passFacets(meta, segLang, f) {
    if (f.member && meta.member !== f.member) return false;
    if (f.branch && meta.branch !== f.branch) return false;
    if (f.lang && segLang !== f.lang) return false;
    return true;
  }

  // シャード内で、複数トリグラムの posting（[vi,si]）を積集合
  function intersectPostings(shard, tris) {
    const lists = tris.map((t) => shard.tri[t] || []);
    if (lists.some((l) => l.length === 0)) return [];
    lists.sort((a, b) => a.length - b.length);
    let cur = new Set(lists[0].map((e) => e[0] + ":" + e[1]));
    for (let k = 1; k < lists.length && cur.size; k++) {
      const nxt = new Set(lists[k].map((e) => e[0] + ":" + e[1]));
      cur = new Set([...cur].filter((key) => nxt.has(key)));
    }
    return [...cur].map((key) => key.split(":").map(Number)); // [[vi,si],...]
  }

  function toResult(shard, vi, si, query) {
    const meta = shard.meta[vi];
    const seg = shard.segs[vi][si]; // [start,dur,text,lang]
    return {
      video_id: shard.vids[vi],
      member: meta.member,
      member_ja: meta.member_ja || "",
      branch: meta.branch,
      title: meta.title,
      url: meta.url,
      lang: seg[3],
      sub_kind: meta.sub_kind,
      start: seg[0],
      dur: seg[1],
      text: seg[2],
      snippet: snippet(seg[2], query),
    };
  }

  async function collectHits(query, f) {
    const q = query.toLowerCase();
    const hits = [];
    if (query.length >= 3) {
      const tris = trigrams(query);
      // クエリの全トリグラムを含むシャードだけが候補
      let buckets = null;
      for (const t of tris) {
        const bs = triIndex[t];
        if (!bs) { buckets = []; break; }
        if (buckets === null) buckets = bs.slice();
        else { const set = new Set(bs); buckets = buckets.filter((b) => set.has(b)); }
        if (!buckets.length) break;
      }
      buckets = buckets || [];
      await getShards(buckets);
      for (const b of buckets) {
        const shard = shardCache.get(b);
        for (const [vi, si] of intersectPostings(shard, tris)) {
          const seg = shard.segs[vi][si];
          if (!seg[2].toLowerCase().includes(q)) continue; // 実体で部分一致を確認
          if (!passFacets(shard.meta[vi], seg[3], f)) continue;
          hits.push({ b, vi, si });
        }
      }
    } else {
      // 1〜2文字はトリグラム索引が使えない → 全シャードを走査（LIKE 相当）
      const all = Array.from({ length: manifest.shards }, (_, i) => i);
      await getShards(all);
      for (const b of all) {
        const shard = shardCache.get(b);
        shard.segs.forEach((segs, vi) => {
          if (!passFacets(shard.meta[vi], null, f) && !f.lang) return; // member/branch 早期除外
          segs.forEach((seg, si) => {
            if (!seg[2].toLowerCase().includes(q)) return;
            if (!passFacets(shard.meta[vi], seg[3], f)) return;
            hits.push({ b, vi, si });
          });
        });
      }
    }
    return hits;
  }

  async function search(p) {
    await load();
    const query = (p.q || "").trim();
    const sort = p.sort === "relevance" ? "relevance" : "date";
    const page = Math.max(1, parseInt(p.page || 1, 10));
    const pageSize = Math.min(Math.max(parseInt(p.page_size || 20, 10), 1), 100);
    if (!query) return { query, page, page_size: pageSize, total: 0, sort, results: [] };

    const f = { member: p.member || "", branch: p.branch || "", lang: p.lang || "" };
    const hits = await collectHits(query, f);

    const seg = (h) => shardCache.get(h.b).segs[h.vi][h.si];
    const pub = (h) => shardCache.get(h.b).meta[h.vi].published_at || "";
    if (sort === "relevance") {
      hits.sort((a, b) => seg(a)[2].length - seg(b)[2].length ||
        pub(b).localeCompare(pub(a)));
    } else {
      hits.sort((a, b) => pub(b).localeCompare(pub(a)) || seg(a)[0] - seg(b)[0]);
    }

    const total = hits.length;
    const offset = (page - 1) * pageSize;
    const results = hits.slice(offset, offset + pageSize)
      .map((h) => toResult(shardCache.get(h.b), h.vi, h.si, query));
    return { query, page, page_size: pageSize, total, sort, results };
  }

  // 取得済みシャードから video を探す（検索でその用例のシャードは取得済み）
  function findVideo(videoId) {
    for (const shard of shardCache.values()) {
      const vi = shard.vids.indexOf(videoId);
      if (vi >= 0) return { shard, vi };
    }
    return null;
  }

  async function context(p) {
    await load();
    const videoId = p.video_id;
    const start = parseFloat(p.start || 0);
    let win = parseInt(p.window || 3, 10);
    win = Math.max(0, Math.min(win, 20));

    let hit = findVideo(videoId);
    if (!hit && manifest.shards <= 16) { // 未取得なら小規模時のみ全ロードして再探索
      await getShards(Array.from({ length: manifest.shards }, (_, i) => i));
      hit = findVideo(videoId);
    }
    if (!hit) return { video_id: videoId, video: null, segments: [] };

    const meta = hit.shard.meta[hit.vi];
    const rows = hit.shard.segs[hit.vi]; // 時刻順 [start,dur,text,lang]
    if (!rows.length) return { video_id: videoId, video: meta, segments: [] };

    let center = 0, best = Infinity;
    rows.forEach((r, i) => { const d = Math.abs(r[0] - start); if (d < best) { best = d; center = i; } });
    const lo = Math.max(0, center - win);
    const hi = Math.min(rows.length, center + win + 1);
    const segments = [];
    for (let i = lo; i < hi; i++) {
      segments.push({ start: rows[i][0], dur: rows[i][1], text: rows[i][2], is_current: i === center });
    }
    return { video_id: videoId, video: meta, segments };
  }

  async function facets() { await load(); return manifest.facets; }
  async function stats() { await load(); return manifest.stats; }

  return { mode: "static", search, context, facets, stats };
})();

window.Api = Api;
