// 検索バックエンドの抽象。
// - サーバモード（既定）: FastAPI の /api/* を叩く。
// - 静的モード: config.js が window.HOLOGLISH_INDEX_BASE を設定していると、
//   シャード化した n-gram 索引をブラウザ内で検索する（サーバ不要）。
//
// 照合は正規化テキスト（NFKC・小文字化・空白除去・カナ→かな）に対して行い、
// 空白区切りの複数語 AND に対応。3文字以上は 3-gram、2文字は 2-gram でシャードを
// 絞り、1文字は全シャード走査。server/search.py と同じ結果を返す。
//
// 速度の要点（索引 version 4）:
//  - n-gram 索引は gram ハッシュでバケット分割されており、**クエリに出てくる
//    gram のバケットだけ**を取得する（全語彙をまとめた数十MBのファイルを読まない）。
//  - シャードは投稿日の新しい順（shard 0 が最新）。既定の新着順では先頭から
//    順に見て、必要件数が埋まった時点で打ち切る（よくある語でも全シャードを
//    取りに行かない）。打ち切った場合は partial:true を返す。
//  - メンバー/ブランチ絞り込みは manifest のシャード別一覧で事前に間引く。

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
  const shardCache = new Map();
  const gramCache = new Map();   // "tri:12" → { gram: [shard,...] }
  const inflight = new Map();    // 同じファイルの多重取得を防ぐ

  // 同時に取得するシャード数の上限。1回目は少なく取り、足りなければ倍々に
  // 増やす（よくある語は先頭シャードだけで足りるので取りすぎない。珍しい語は
  // すぐに並列度が上がるので往復回数も増えない）。
  const SHARD_BATCH_MAX = 8;
  // 一致度順は「どこに良い一致があるか」が事前に分からないので、新しい側から
  // 十分な候補が集まるまで見て打ち切る（上限シャード数も設ける）。
  // 全体を見ないため厳密な最良ではなく、partial:true を返す。
  const RELEVANCE_MAX_SHARDS = 24;
  const RELEVANCE_MIN_POOL = 400;

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

  // 取得は1ファイル1回だけ（同時に同じものを要求しても1リクエストに束ねる）
  function fetchJson(url) {
    if (inflight.has(url)) return inflight.get(url);
    const p = fetch(url).then((r) => r.json()).finally(() => inflight.delete(url));
    inflight.set(url, p);
    return p;
  }

  async function load() {
    if (manifest) return;
    manifest = await fetchJson(`${BASE}/manifest.json`);
  }

  // pipeline/export_static.py の gram_bucket と同じ計算（FNV-1a 32bit / UTF-8）
  const _enc = new TextEncoder();
  function gramBucket(gram, buckets) {
    let h = 2166136261;
    for (const byte of _enc.encode(gram)) {
      h ^= byte;
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h % buckets;
  }

  // 索引の gram バケットを取得（必要なものだけ・キャッシュ付き）
  async function getGramIndex(grams, kind) {
    const buckets = kind === "tri" ? manifest.tri_buckets : manifest.bi_buckets;
    const want = new Map(); // bucket → key
    for (const g of grams) {
      const b = gramBucket(g, buckets);
      want.set(b, `${kind}:${b}`);
    }
    await Promise.all([...want.entries()].map(async ([b, key]) => {
      if (gramCache.has(key)) return;
      gramCache.set(key, await fetchJson(`${BASE}/${kind}/${b}.json`));
    }));
    return (gram) => {
      const key = `${kind}:${gramBucket(gram, buckets)}`;
      const part = gramCache.get(key) || {};
      return part[gram];
    };
  }

  async function getShard(b) {
    if (shardCache.has(b)) return shardCache.get(b);
    const sh = await fetchJson(`${BASE}/shard-${b}.json`);
    if (!sh.norms) {
      // 照合用の正規化テキストを前計算（検索の度に再計算しない）
      sh.norms = sh.segs.map((vsegs) => vsegs.map((seg) => normalizeText(seg[2])));
    }
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
  async function termShards(term) {
    if (term.length >= 3) {
      const grams = ngrams(term, 3);
      const lookup = await getGramIndex(grams, "tri");
      let set = null;
      for (const g of grams) {
        const bs = lookup(g);
        if (!bs) return new Set();
        const s = new Set(bs);
        set = set === null ? s : intersect(set, s);
        if (!set.size) return set;
      }
      return set || new Set();
    }
    if (term.length === 2) {
      const lookup = await getGramIndex([term], "bi");
      const bs = lookup(term);
      return bs ? new Set(bs) : new Set();
    }
    return null; // 1文字は絞れない
  }

  // メンバー/ブランチ絞り込みで、そもそも該当し得ないシャードを落とす
  function pruneByFilters(buckets, f) {
    const sm = manifest.shard_members, sb = manifest.shard_branches;
    let mi = -1, bi = -1;
    if (f.member) {
      mi = (manifest.facets.members || []).findIndex((m) => m.value === f.member);
      if (mi < 0) return [];
      if (!sm) mi = -1; // 旧 manifest では間引けない
    }
    if (f.branch) {
      bi = (manifest.facets.branches || []).indexOf(f.branch);
      if (bi < 0) return [];
      if (!sb) bi = -1;
    }
    if (mi < 0 && bi < 0) return buckets;
    return buckets.filter((b) =>
      (mi < 0 || (sm[b] || []).includes(mi)) &&
      (bi < 0 || (sb[b] || []).includes(bi)));
  }

  function scanShard(shard, b, terms, f, hits) {
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

  // 候補シャードを新しい順に見て、必要件数が埋まったら打ち切る。
  // 返り値 partial:true は「まだ先に一致があり得る（total は下限）」の意。
  async function collectHits(terms, f, needed, sort) {
    let shardSet = null;
    for (const term of terms) {
      const ts = await termShards(term);
      if (ts === null) continue;
      shardSet = shardSet === null ? ts : intersect(shardSet, ts);
      if (shardSet.size === 0) break;
    }
    let buckets = shardSet === null
      ? Array.from({ length: manifest.shards }, (_, i) => i)
      : [...shardSet];
    buckets = pruneByFilters(buckets, f);
    buckets.sort((a, b) => a - b); // shard 0 = 最新

    // 新着順は「新しいシャードから順に見る」ので途中で打ち切れる。
    // 一致度順は全体を見ないと厳密にならないため、上限までに留める。
    const byDate = sort !== "relevance";
    const limitShards = byDate ? buckets.length : Math.min(buckets.length, RELEVANCE_MAX_SHARDS);

    const hits = [];
    let scanned = 0;
    let size = 1; // 取得済みで足りることが多いので、少なく始めて倍々に増やす
    while (scanned < limitShards) {
      const batch = buckets.slice(scanned, Math.min(scanned + size, limitShards));
      const loaded = await getShards(batch);
      batch.forEach((b, k) => scanShard(loaded[k], b, terms, f, hits));
      scanned += batch.length;
      // 新着順: 新しいシャードから順に見ているので、必要件数が揃えば確定。
      // 一致度順: 十分な候補が集まったら、そこから順位付けする。
      const enough = byDate ? hits.length >= needed
                            : hits.length >= Math.max(needed, RELEVANCE_MIN_POOL);
      if (enough) break;
      size = Math.min(size * 2, SHARD_BATCH_MAX);
    }
    return { hits, partial: scanned < buckets.length };
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
    if (!query || !terms.length) {
      return { query, page, page_size: pageSize, total: 0, sort, partial: false, results: [] };
    }

    const f = { member: p.member || "", branch: p.branch || "", lang: p.lang || "" };
    // そのページを埋めるのに必要な件数（+1 で「まだ先がある」を判定できる）
    const needed = page * pageSize + 1;
    const { hits, partial } = await collectHits(terms, f, needed, sort);

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
    // partial のとき total は下限（打ち切ったので、まだ先に一致があり得る）
    return { query, page, page_size: pageSize, total, sort, partial, results };
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
