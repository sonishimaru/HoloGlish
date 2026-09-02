// 検索バックエンドの抽象。
// - サーバモード（既定）: FastAPI の /api/* を叩く。
// - 静的モード: config.js が window.HOLOGLISH_INDEX_BASE を設定していると、
//   n-gram 索引をブラウザ内で検索する（サーバ不要）。
//
// 照合は正規化テキスト（NFKC・小文字化・空白除去・カナ→かな）に対して行い、
// 空白区切りの複数語 AND に対応。server/search.py と同じ結果を返す。
//
// 速度の要点（索引 version 5）— 「その語を含む動画だけを取りに行く」:
//  - n-gram 索引は gram → [group, mask, ...]。動画は投稿日の新しい順に並び、
//    mask はグループ内のどの動画にその gram が在るかを示すビット列。
//    クエリの全 gram でマスクを AND すれば、**候補動画が動画単位で確定する**。
//  - 本文は動画単位ファイル（v/<video_id>.json）。候補動画だけを新しい順に、
//    そのページに必要な件数が埋まるまで取得する。含まない動画は取得しない。
//  - 索引自体も gram ハッシュでバケット分割してあり、クエリに出てくる gram の
//    バケットだけを取得する（起動時に読むのは manifest だけ）。
//  - 1文字クエリ用に uni 索引もあるため、1文字でも全走査しない。
//  - 打ち切った場合は partial:true（total は下限）。
//  - search(p, onProgress) は取得の進み具合を逐次通知する（画面が止まって見えないように）。
//
// 入力補完（suggest）は別ファイル suggest.json。最初の入力があって初めて取得する。

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
      // 入力補完の語彙は静的索引の生成物（suggest.json）なのでサーバモードでは持たない
      async suggest() { return []; },
    };
  }

  // ---------- 静的モード（動画単位の n-gram 索引） ----------
  const BASE = window.HOLOGLISH_INDEX_BASE.replace(/\/$/, "");
  let manifest = null;
  const videoCache = new Map();  // 動画index → {meta, segs, norms}
  const gramCache = new Map();   // "tri:12" → { gram: [group, mask, ...] }
  const inflight = new Map();    // 同じファイルの多重取得を防ぐ

  // 同時に取得する動画数の上限。1回目は少なく取り、足りなければ
  // ここまでの命中率から必要数を見積もって増やす。
  const VIDEO_BATCH_MAX = 12;
  // 一致度順は「どこに良い一致があるか」が事前に分からないので、新しい側から
  // 十分な候補が集まるまで見て打ち切る（上限も設ける）。厳密な最良ではない。
  const RELEVANCE_MIN_POOL = 400;
  const RELEVANCE_MAX_VIDEOS = 60;

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

  const KINDS = { 1: ["uni", "uni_buckets"], 2: ["bi", "bi_buckets"], 3: ["tri", "tri_buckets"] };

  // 索引の gram バケットを取得（必要なものだけ・キャッシュ付き）
  async function getGramIndex(grams, n) {
    const [kind, key] = KINDS[n];
    const buckets = manifest[key];
    const need = new Set(grams.map((g) => gramBucket(g, buckets)));
    await Promise.all([...need].map(async (b) => {
      const ck = `${kind}:${b}`;
      if (gramCache.has(ck)) return;
      gramCache.set(ck, await fetchJson(`${BASE}/${kind}/${b}.json`));
    }));
    return (gram) => (gramCache.get(`${kind}:${gramBucket(gram, buckets)}`) || {})[gram];
  }

  async function getVideo(vi) {
    if (videoCache.has(vi)) return videoCache.get(vi);
    const v = await fetchJson(`${BASE}/v/${manifest.vids[vi]}.json`);
    if (!v.norms) {
      // 照合用の正規化テキストを前計算（検索の度に再計算しない）
      v.norms = v.segs.map((seg) => normalizeText(seg[2]));
    }
    videoCache.set(vi, v);
    return v;
  }
  const getVideos = (list) => Promise.all(list.map(getVideo));

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

  // postings( [group, mask, ...] ) → Map(group → mask)
  function toMap(flat) {
    const m = new Map();
    for (let i = 0; i + 1 < flat.length; i += 2) m.set(flat[i], flat[i + 1]);
    return m;
  }
  // グループごとにマスクを AND（両方に在るグループだけ残る）
  function andMasks(a, b) {
    const out = new Map();
    for (const [g, m] of a) {
      const o = b.get(g);
      if (o === undefined) continue;
      const v = m & o;
      if (v) out.set(g, v);
    }
    return out;
  }

  // 1語を含む動画のマスク（null は「この語では絞れない」＝該当なしと区別する）
  async function termMasks(term) {
    const n = Math.min(term.length, 3);
    const grams = n === 3 ? ngrams(term, 3) : [term];
    const lookup = await getGramIndex(grams, n);
    let acc = null;
    for (const g of grams) {
      const flat = lookup(g);
      if (!flat) return new Map();       // この gram を含む動画は無い
      const m = toMap(flat);
      acc = acc === null ? m : andMasks(acc, m);
      if (acc.size === 0) return acc;
    }
    return acc || new Map();
  }

  // マスク → 候補動画index（新しい順）。member/branch 絞り込みもここで効かせる。
  function candidateVideos(masks, f) {
    const G = manifest.mask_group;
    const total = manifest.vids.length;
    let mi = -1, bi = -1;
    if (f.member) {
      mi = (manifest.facets.members || []).findIndex((m) => m.value === f.member);
      if (mi < 0) return [];
    }
    if (f.branch) {
      bi = (manifest.facets.branches || []).indexOf(f.branch);
      if (bi < 0) return [];
    }
    const out = [];
    for (const g of [...masks.keys()].sort((a, b) => a - b)) {
      const mask = masks.get(g);
      for (let b = 0; b < G; b++) {
        if (!(mask >> b & 1)) continue;
        const vi = g * G + b;
        if (vi >= total) continue;
        if (mi >= 0 && manifest.vmem[vi] !== mi) continue;
        if (bi >= 0 && manifest.vbr[vi] !== bi) continue;
        out.push(vi);
      }
    }
    return out;
  }

  function scanVideo(v, vi, terms, f, hits) {
    const segs = v.segs, norms = v.norms;
    for (let si = 0; si < segs.length; si++) {
      if (f.lang && segs[si][3] !== f.lang) continue;
      const nt = norms[si];
      if (terms.every((t) => nt.includes(t))) hits.push({ vi, si });
    }
  }

  // 候補動画を新しい順に見て、必要件数が埋まったら打ち切る。
  // 返り値 partial:true は「まだ先に一致があり得る（total は下限）」の意。
  // onProgress は取得の進み具合を逐次通知する（画面を止めて見せないため）。
  async function collectHits(terms, f, needed, sort, onProgress) {
    const notify = (phase, candidates, scanned, hits) => {
      if (onProgress) onProgress({ phase, candidates, scanned, hits });
    };
    notify("index", 0, 0, 0);
    let masks = null;
    for (const term of terms) {
      const m = await termMasks(term);
      masks = masks === null ? m : andMasks(masks, m);
      if (masks.size === 0) break;
    }
    const cands = masks ? candidateVideos(masks, f) : [];
    if (!cands.length) return { hits: [], partial: false, candidates: 0 };
    notify("scan", cands.length, 0, 0);

    const byDate = sort !== "relevance";
    const want = byDate ? needed : Math.max(needed, RELEVANCE_MIN_POOL);
    const limit = byDate ? cands.length : Math.min(cands.length, RELEVANCE_MAX_VIDEOS);

    const hits = [];
    let scanned = 0, size = 1;
    while (scanned < limit) {
      const batch = cands.slice(scanned, Math.min(scanned + size, limit));
      const loaded = await getVideos(batch);
      batch.forEach((vi, k) => scanVideo(loaded[k], vi, terms, f, hits));
      scanned += batch.length;
      notify("scan", cands.length, scanned, hits.length);
      if (hits.length >= want) break;
      // 次に取る数は、ここまでの「1動画あたりの命中数」から見積もる。
      // まだ0件のうちは手掛かりが無いので倍々で広げる。
      size = hits.length > 0
        ? Math.min(Math.max(Math.ceil((want - hits.length) / (hits.length / scanned)), 1), VIDEO_BATCH_MAX)
        : Math.min(size * 2, VIDEO_BATCH_MAX);
    }
    return { hits, partial: scanned < cands.length, candidates: cands.length };
  }

  function toResult(vi, si, terms) {
    const v = videoCache.get(vi);
    const meta = v.meta, seg = v.segs[si];
    return {
      video_id: manifest.vids[vi], member: meta.member, member_ja: meta.member_ja || "",
      branch: meta.branch, title: meta.title, url: meta.url, lang: seg[3],
      sub_kind: meta.sub_kind, start: seg[0], dur: seg[1], text: seg[2],
      snippet: snippet(seg[2], terms),
    };
  }

  async function search(p, onProgress) {
    if (onProgress) onProgress({ phase: "index", candidates: 0, scanned: 0, hits: 0 });
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
    const { hits, partial } = await collectHits(terms, f, needed, sort, onProgress);

    const nt = (h) => videoCache.get(h.vi).norms[h.si];
    if (sort === "relevance") {
      // 自然な並び: 語が早く現れる → 発話が短い（語が目立つ）→ 新しい
      const pos = (h) => { const i = nt(h).indexOf(terms[0]); return i < 0 ? 1e9 : i; };
      hits.sort((a, b) =>
        pos(a) - pos(b) || nt(a).length - nt(b).length || a.vi - b.vi);
    } else {
      // vids は投稿日の新しい順なので、index が小さいほど新しい
      hits.sort((a, b) =>
        a.vi - b.vi ||
        videoCache.get(a.vi).segs[a.si][0] - videoCache.get(b.vi).segs[b.si][0]);
    }

    const total = hits.length;
    const results = hits.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize)
      .map((h) => toResult(h.vi, h.si, terms));
    // partial のとき total は下限（打ち切ったので、まだ先に一致があり得る）
    return { query, page, page_size: pageSize, total, sort, partial, results };
  }

  async function context(p) {
    await load();
    const videoId = p.video_id;
    const start = parseFloat(p.start || 0);
    const win = Math.max(0, Math.min(parseInt(p.window || 3, 10), 20));

    const vi = manifest.vids.indexOf(videoId);
    if (vi < 0) return { video_id: videoId, video: null, segments: [] };
    const v = await getVideo(vi);            // 動画単位なので1ファイルで済む
    const meta = v.meta, rows = v.segs;
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

  // ---------- 入力補完 ----------
  // suggest.json は「実際に話されている言い回し」だけを頻度順に並べたもの。
  // 初回の入力時にだけ取りに行き（起動を遅らせない）、以後はメモリ上で前方一致。
  // 照合は検索と同じ正規化を通すので、カナ/かな・全角/半角の違いを吸収する。
  let suggestList = null;
  async function suggest(prefix, limit = 8) {
    const key = normalizeText(prefix || "");
    if (!key) return [];
    if (!suggestList) {
      try {
        const data = await fetchJson(`${BASE}/suggest.json`);
        suggestList = (data.items || []).map((it) => ({ t: it.t, k: normalizeText(it.t) }));
      } catch (_) {
        suggestList = []; // 候補が無くても検索そのものは動く
      }
    }
    const out = [];
    for (const s of suggestList) {
      if (s.k !== key && s.k.startsWith(key)) {
        out.push(s.t);
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  return { mode: "static", search, context, facets, stats, suggest };
})();

window.Api = Api;
