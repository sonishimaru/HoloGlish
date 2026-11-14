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
      // 入力補完・表記ゆれ展開の語彙は静的索引の生成物（suggest.json）なので
      // サーバモードでは持たない（検索は表記どおりの一致で動く）
      async suggest() { return []; },
      prefetch() {},
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
    const p = fetch(url).then((r) => readJson(r, url)).finally(() => inflight.delete(url));
    inflight.set(url, p);
    return p;
  }

  // 索引本体は配信容量を抑えるため gzip（.json.gz）。GitHub Pages は .gz を
  // Content-Encoding 無しのバイナリとして配るので、ブラウザ内で展開する。
  async function readJson(r, url) {
    if (!url.endsWith(".gz")) return r.json();
    try {
      return await new Response(r.body.pipeThrough(new DecompressionStream("gzip"))).json();
    } catch (_) {
      // 配信側が展開済みの本文を返す環境（一部のローカルサーバ等）向けの保険
      return (await fetch(url)).json();
    }
  }

  async function load() {
    if (manifest) return;
    manifest = await fetchJson(`${BASE}/manifest.json`);
  }

  // manifest.gz が立っている索引は .json.gz で配信されている（manifest 自体は常に平文）
  const ext = () => (manifest && manifest.gz ? ".json.gz" : ".json");

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
      gramCache.set(ck, await fetchJson(`${BASE}/${kind}/${b}${ext()}`));
    }));
    return (gram) => (gramCache.get(`${kind}:${gramBucket(gram, buckets)}`) || {})[gram];
  }

  async function getVideo(vi) {
    if (videoCache.has(vi)) return videoCache.get(vi);
    const v = await fetchJson(`${BASE}/v/${manifest.vids[vi]}${ext()}`);
    if (!v.norms) {
      // 照合用の正規化テキストを前計算（検索の度に再計算しない）
      v.norms = v.segs.map((seg) => normalizeText(seg[2]));
    }
    videoCache.set(vi, v);
    return v;
  }
  const getVideos = (list) => Promise.all(list.map(getVideo));

  function snippet(text, needles, radius = 40) {
    const lower = text.toLowerCase();
    for (const t of needles) {
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

  // グループごとにマスクを OR（どちらかに在ればよい。表記ゆれの束ね用）
  function orMasks(a, b) {
    const out = new Map(a);
    for (const [g, m] of b) out.set(g, (out.get(g) || 0) | m);
    return out;
  }

  // 表記ゆれの組（例: [たのしい, 楽しい]）のどれかを含む動画のマスク
  async function groupMasks(variants) {
    const all = await Promise.all(variants.map(termMasks));
    return all.reduce((acc, m) => orMasks(acc, m), new Map());
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

  // groups: 語ごとの表記ゆれの組。語どうしは AND、組の中は OR。
  function scanVideo(v, vi, groups, f, hits) {
    const segs = v.segs, norms = v.norms;
    for (let si = 0; si < segs.length; si++) {
      if (f.lang && segs[si][3] !== f.lang) continue;
      const nt = norms[si];
      if (groups.every((g) => g.some((t) => nt.includes(t)))) hits.push({ vi, si });
    }
  }

  // 候補動画を新しい順に見て、必要件数が埋まったら打ち切る。
  // 返り値 partial:true は「まだ先に一致があり得る（total は下限）」の意。
  // onProgress は取得の進み具合を逐次通知する（画面を止めて見せないため）。
  async function collectHits(groups, f, needed, sort, onProgress) {
    const notify = (phase, candidates, scanned, hits) => {
      if (onProgress) onProgress({ phase, candidates, scanned, hits });
    };
    notify("index", 0, 0, 0);
    let masks = null;
    for (const group of groups) {
      const m = await groupMasks(group);
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
      batch.forEach((vi, k) => scanVideo(loaded[k], vi, groups, f, hits));
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

  function toResult(vi, si, needles) {
    const v = videoCache.get(vi);
    const meta = v.meta, seg = v.segs[si];
    return {
      video_id: manifest.vids[vi], member: meta.member, member_ja: meta.member_ja || "",
      branch: meta.branch, title: meta.title, url: meta.url, lang: seg[3],
      sub_kind: meta.sub_kind, start: seg[0], dur: seg[1], text: seg[2],
      snippet: snippet(seg[2], needles),
    };
  }

  async function search(p, onProgress) {
    if (onProgress) onProgress({ phase: "index", candidates: 0, scanned: 0, hits: 0 });
    const expand = !!p.expand;
    // 展開には語彙が要る。manifest と並行して取りに行く（直列にしない）
    await Promise.all([load(), expand ? loadVocab() : null]);
    const query = (p.q || "").trim();
    const sort = p.sort === "relevance" ? "relevance" : "date";
    const page = Math.max(1, parseInt(p.page || 1, 10));
    const pageSize = Math.min(Math.max(parseInt(p.page_size || 20, 10), 1), 100);
    const terms = queryTerms(query);
    if (!query || !terms.length) {
      return { query, page, page_size: pageSize, total: 0, sort, partial: false, results: [], variants: [] };
    }
    // 語ごとに表記ゆれの組へ展開（展開しないときは自分だけの組）
    const expanded = terms.map((t) => (expand ? expandTerm(t) : { norm: [t], display: [] }));
    const groups = expanded.map((e) => e.norm);
    // スニペットの切り出し位置: 生テキストに現れる表記（「楽しい」）も探す
    const needles = [...new Set(groups.flat().concat(expanded.flatMap((e) => e.display.map((d) => d.toLowerCase()))))];

    const f = { member: p.member || "", branch: p.branch || "", lang: p.lang || "" };
    // そのページを埋めるのに必要な件数（+1 で「まだ先がある」を判定できる）
    const needed = page * pageSize + 1;
    const { hits, partial } = await collectHits(groups, f, needed, sort, onProgress);

    const nt = (h) => videoCache.get(h.vi).norms[h.si];
    if (sort === "relevance") {
      // 自然な並び: 語が早く現れる → 発話が短い（語が目立つ）→ 新しい
      const pos = (h) => {
        const ix = groups[0].map((t) => nt(h).indexOf(t)).filter((i) => i >= 0);
        return ix.length ? Math.min(...ix) : 1e9;
      };
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
      .map((h) => toResult(h.vi, h.si, needles));
    // partial のとき total は下限（打ち切ったので、まだ先に一致があり得る）
    // variants: 語ごとに「入力した表記以外に含めた表記」（画面で明示・ハイライトに使う）
    return {
      query, page, page_size: pageSize, total, sort, partial, results,
      variants: expanded.map((e) => e.display),
    };
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

  // ---------- 語彙（入力補完・表記ゆれ展開の共通の土台） ----------
  // suggest.json は「実際に話されている言い回し」を頻度順に並べたもの。漢字を含む語には
  // 読み r（ひらがな）が付いている。これを2通りに使う:
  //  - 入力補完: 表記の前方一致に加え、読みの前方一致（「たの」→「楽しい」）。
  //  - 表記ゆれ展開: 「たのしい」で検索すると「楽しい」も含めて探す。
  //    自動字幕は漢字で書くことが多く、かなで打つと大半を取りこぼすため
  //    （実データ: たのしい 72本 / 楽しい 26,570本）。
  // 展開先はコーパスに実在する表記だけなので、展開しても空振りはしない。
  const MAX_VARIANTS = 8;     // 1語あたりの表記の上限（同音異義が多い読みで膨らませない）
  const MIN_READING = 2;      // 1文字の読み（「き」→ 木・気 …）は展開しない
  let vocab = null;
  let vocabP = null;

  function isKana(s) { return /^[\u3041-\u3096ー]+$/.test(s); }

  function loadVocab() {
    if (vocabP) return vocabP;
    vocabP = (async () => {
      const list = [];
      const byReading = new Map();  // 読み → [{t, k}]（頻度順）
      const readingsOf = new Map(); // 表記(正規化) → [読み]
      try {
        await load(); // 拡張子の判定に manifest が要る
        const data = await fetchJson(`${BASE}/suggest${ext()}`);
        for (const it of data.items || []) {
          const e = { t: it.t, k: normalizeText(it.t), r: (it.r || []).map(normalizeText) };
          list.push(e);
          if (e.r.length) readingsOf.set(e.k, e.r);
          for (const r of e.r) {
            if (!byReading.has(r)) byReading.set(r, []);
            byReading.get(r).push(e);
          }
        }
      } catch (_) {
        // 語彙が無くても検索・補完は表記どおりで動く
      }
      vocab = { list, byReading, readingsOf };
      return vocab;
    })();
    return vocabP;
  }

  // 1語（正規化済み）を表記ゆれの組へ展開する。
  // norm: 照合に使う正規化済みの表記（先頭は入力そのもの）
  // display: 入力以外に含めた表記（画面に出す。語彙の表記そのまま）
  function expandTerm(term) {
    const norm = [term];
    const display = [];
    if (!vocab) return { norm, display };
    const add = (k, shown) => {
      if (norm.length >= MAX_VARIANTS || norm.includes(k)) return;
      norm.push(k);
      display.push(shown);
    };
    // かなで打たれたら、それ自体が読み。漢字なら語彙にある読みを引く。
    const reads = isKana(term) ? [term] : vocab.readingsOf.get(term) || [];
    for (const r of reads) {
      if (r.length < MIN_READING) continue;
      add(r, r);                                   // 「可愛い」→「かわいい」
      for (const e of vocab.byReading.get(r) || []) add(e.k, e.t); // 「かわいい」→「可愛い」
    }
    return { norm, display };
  }

  // 入力補完。表記の前方一致に加え、読みの前方一致でも拾う。
  // 返り値: [{ t: 表記, reading: 読みで当たったときの読み（表記で当たったら null） }]
  async function suggest(prefix, limit = 8) {
    const key = normalizeText(prefix || "");
    if (!key) return [];
    await loadVocab();
    const out = [];
    for (const e of vocab.list) {
      if (e.k === key) continue; // 打った語そのものは出さない
      let reading = null;
      if (!e.k.startsWith(key)) {
        // 当たった読みが複数あれば長い方を見せる（「可愛い」は かわい/かわいい → かわいい。
        // 短い方は読み付け器の誤読であることが多い）
        reading = e.r.filter((r) => r.startsWith(key)).sort((a, b) => b.length - a.length)[0] || null;
        if (!reading) continue;
      }
      out.push({ t: e.t, reading });
      if (out.length >= limit) break;
    }
    return out;
  }

  // 入力欄にフォーカスが来た時点で語彙を先読みする（1打鍵目から候補を出すため）
  function prefetch() { loadVocab(); }

  return { mode: "static", search, context, facets, stats, suggest, prefetch };
})();

window.Api = Api;
