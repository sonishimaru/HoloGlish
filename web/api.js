// 検索バックエンドの抽象。
// - サーバモード（既定）: FastAPI の /api/* を叩く。
// - 静的モード: config.js が window.HOLOGLISH_INDEX_BASE を設定していると、
//   シャード化した n-gram 索引をブラウザ内で検索する（サーバ不要）。
//
// 照合は正規化テキスト（NFKC・小文字化・空白除去・カナ→かな）に対して行い、
// 空白区切りの複数語 AND に対応。3文字以上は 3-gram、2文字は 2-gram でシャードを
// 絞り、1文字は全シャード走査。server/search.py と同じ結果を返す。

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

  // ---------- 静的モード（シャード n-gram 索引） ----------
  const BASE = window.HOLOGLISH_INDEX_BASE.replace(/\/$/, "");
  let manifest = null;
  let triIndex = null; // { 3gram: [shard,...] }
  let biIndex = null;  // { 2gram: [shard,...] }
  const shardCache = new Map();

  // pipeline/normalize.py と同一の正規化（NFKC・小文字・空白除去・カナ→かな）
  function normalizeText(s) {
    if (!s) return "";
    s = s.normalize("NFKC").toLowerCase().replace(/\s+/g, "");
    let out = "";
    for (const ch of s) {
      const o = ch.codePointAt(0);
      out += (o >= 0x30a1 && o <= 0x30f6) ? String.fromCodePoint(o - 0x60) : ch;
    }
    return out;
  }
  function queryTerms(q) {
    return q.trim().split(/\s+/).map(normalizeText).filter(Boolean);
  }
  function ngrams(s, n) {
    const set = new Set();
    for (let i = 0; i + n <= s.length; i++) set.add(s.slice(i, i + n));
    return [...set];
  }

  async function load() {
    if (manifest) return;
    [manifest, triIndex, biIndex] = await Promise.all([
      fetch(`${BASE}/manifest.json`).then((r) => r.json()),
      fetch(`${BASE}/tri-index.json`).then((r) => r.json()),
      fetch(`${BASE}/bi-index.json`).then((r) => r.json()),
    ]);
  }

  async function getShard(b) {
    if (shardCache.has(b)) return shardCache.get(b);
    const sh = await fetch(`${BASE}/shard-${b}.json`).then((r) => r.json());
    // 照合用の正規化テキストを前計算（検索の度に再計算しない）
    sh.norms = sh.segs.map((vsegs) => vsegs.map((seg) => normalizeText(seg[2])));
    shardCache.set(b, sh);
    return sh;
  }
  const getShards = (buckets) => Promise.all(buckets.map(getShard));

  function snippet(text, terms, radius = 40) {
    const lower = text.toLowerCase();
    for (const t of terms) {
      const i = lower.indexOf(t);
      if (i >= 0) {
        const start = Math.max(0, i - radius);
        const end = Math.min(text.length, i + t.length + radius);
        return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
      }
    }
    return text.slice(0, radius * 2);
  }

  const intersect = (a, b) => new Set([...a].filter((x) => b.has(x)));

  // 1語の候補シャード集合（null は「絞れない＝全シャード」）
  function termShards(term) {
    if (term.length >= 3) {
      let set = null;
      for (const g of ngrams(term, 3)) {
        const bs = triIndex[g];
        if (!bs) return new Set();
        const s = new Set(bs);
        set = set === null ? s : intersect(set, s);
        if (!set.size) return set;
      }
      return set || new Set();
    }
    if (term.length === 2) {
      const bs = biIndex[term];
      return bs ? new Set(bs) : new Set();
    }
    return null; // 1文字は絞れない
  }

  async function collectHits(terms, f) {
    // 候補シャード = 各語の候補シャードの積集合（1文字語は絞りに寄与しない）
    let shardSet = null;
    for (const term of terms) {
      const ts = termShards(term);
      if (ts === null) continue;
      shardSet = shardSet === null ? ts : intersect(shardSet, ts);
      if (shardSet.size === 0) break;
    }
    const buckets = shardSet === null
      ? Array.from({ length: manifest.shards }, (_, i) => i)
      : [...shardSet];
    await getShards(buckets);

    const hits = [];
    for (const b of buckets) {
      const shard = shardCache.get(b);
      for (let vi = 0; vi < shard.vids.length; vi++) {
        const meta = shard.meta[vi];
        if (f.member && meta.member !== f.member) continue;
        if (f.branch && meta.branch !== f.branch) continue;
        const segs = shard.segs[vi];
        const norms = shard.norms[vi];
        for (let si = 0; si < segs.length; si++) {
          if (f.lang && segs[si][3] !== f.lang) continue;
          const nt = norms[si];
          if (terms.every((t) => nt.includes(t))) hits.push({ b, vi, si });
        }
      }
    }
    return hits;
  }

  function toResult(shard, vi, si, terms) {
    const meta = shard.meta[vi];
    const seg = shard.segs[vi][si];
    return {
      video_id: shard.vids[vi], member: meta.member, member_ja: meta.member_ja || "",
      branch: meta.branch, title: meta.title, url: meta.url, lang: seg[3],
      sub_kind: meta.sub_kind, start: seg[0], dur: seg[1], text: seg[2],
      snippet: snippet(seg[2], terms),
    };
  }

  async function search(p) {
    await load();
    const query = (p.q || "").trim();
    const sort = p.sort === "relevance" ? "relevance" : "date";
    const page = Math.max(1, parseInt(p.page || 1, 10));
    const pageSize = Math.min(Math.max(parseInt(p.page_size || 20, 10), 1), 100);
    const terms = queryTerms(query);
    if (!query || !terms.length) return { query, page, page_size: pageSize, total: 0, sort, results: [] };

    const f = { member: p.member || "", branch: p.branch || "", lang: p.lang || "" };
    const hits = await collectHits(terms, f);

    const nt = (h) => shardCache.get(h.b).norms[h.vi][h.si];
    const meta = (h) => shardCache.get(h.b).meta[h.vi];
    if (sort === "relevance") {
      // 自然な並び: 語が早く現れる → 発話が短い（語が目立つ）→ 新しい
      const pos = (h) => { const i = nt(h).indexOf(terms[0]); return i < 0 ? 1e9 : i; };
      hits.sort((a, b) =>
        pos(a) - pos(b) ||
        nt(a).length - nt(b).length ||
        (meta(b).published_at || "").localeCompare(meta(a).published_at || ""));
    } else {
      hits.sort((a, b) =>
        (meta(b).published_at || "").localeCompare(meta(a).published_at || "") ||
        shardCache.get(a.b).segs[a.vi][a.si][0] - shardCache.get(b.b).segs[b.vi][b.si][0]);
    }

    const total = hits.length;
    const results = hits.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize)
      .map((h) => toResult(shardCache.get(h.b), h.vi, h.si, terms));
    return { query, page, page_size: pageSize, total, sort, results };
  }

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
    let win = Math.max(0, Math.min(parseInt(p.window || 3, 10), 20));

    let hit = findVideo(videoId);
    if (!hit && manifest.shards <= 16) {
      await getShards(Array.from({ length: manifest.shards }, (_, i) => i));
      hit = findVideo(videoId);
    }
    if (!hit) return { video_id: videoId, video: null, segments: [] };

    const meta = hit.shard.meta[hit.vi];
    const rows = hit.shard.segs[hit.vi];
    if (!rows.length) return { video_id: videoId, video: meta, segments: [] };
    let center = 0, best = Infinity;
    rows.forEach((r, i) => { const d = Math.abs(r[0] - start); if (d < best) { best = d; center = i; } });
    const lo = Math.max(0, center - win), hi = Math.min(rows.length, center + win + 1);
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
