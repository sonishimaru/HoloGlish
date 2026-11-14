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
    const expand = !!p.expand;    // 表記ゆれ（漢字⇔かな・のばし棒）
    const inflect = !!p.inflect;  // 活用形
    const query = (p.q || "").trim();
    const latin = /^[a-z' -]+$/i.test(query); // ローマ字なら「もしかして」を出す
    // 展開・提案には語彙が要る。manifest と並行して取りに行く（直列にしない）
    await Promise.all([load(), expand || inflect || latin ? loadVocab() : null]);
    const sort = p.sort === "relevance" ? "relevance" : "date";
    const page = Math.max(1, parseInt(p.page || 1, 10));
    const pageSize = Math.min(Math.max(parseInt(p.page_size || 20, 10), 1), 100);
    // のばし棒の判定は正規化前の表記で行う（カタカナ語の「ー」を残すため）ので、組で持つ
    const raws = query.split(/\s+/).filter((w) => normalizeText(w));
    const terms = raws.map(normalizeText);
    const suggestion = latin ? romajiSuggestion(query) : null;
    if (!query || !terms.length) {
      return { query, page, page_size: pageSize, total: 0, sort, partial: false, results: [], variants: [], suggestion };
    }
    // 語ごとに、同じものとして探す表記の組へ展開（展開しないときは自分だけの組）
    const expanded = terms.map((t, i) => (expand || inflect
      ? expandTerm(t, raws[i], { spelling: expand, inflect })
      : { norm: [t], display: [] }));
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
      suggestion, // ローマ字入力への「もしかして」（無ければ null）
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

  // ---------- 語彙（入力補完・検索語の展開の共通の土台） ----------
  // suggest.json は「実際に話されている言い回し」を頻度順に並べたもの。注釈付き:
  //   r: 漢字を含む語の読み（ひらがな）  b: 活用した語の原形（楽しかった → 楽しい）
  // これを使って、入力した語だけでは届かない発話まで検索を広げる:
  //  - 表記ゆれ: たのしい → 楽しい（自動字幕は漢字で書くことが多い。
  //    実データ: たのしい 72本 / 楽しい 26,570本）。
  //  - のばし棒・小書き文字: おはよー → おはよ（おはよう に届く。
  //    実データ: おはよー 232本 / おはよう 12,348本）。
  //  - 活用形（任意）: 楽しい → 楽しかった・楽しく（「楽しい」の部分一致では出ない）。
  //  - ローマ字: arigatou → 「もしかして: ありがとう」（置き換えはせず提案だけ）。
  // 展開先はコーパスに実在する表記だけなので、展開しても空振りはしない。
  const MAX_VARIANTS = 8;      // 表記ゆれの上限（同音異義が多い読みで膨らませない）
  const MAX_INFLECTIONS = 6;   // 活用形の上限（頻度の高い順。索引の取得量を抑える）
  const MIN_READING = 2;       // 1文字の読み（「き」→ 木・気 …）は展開しない
  const ROMAJI_RATIO = 10;     // ローマ字が英語として普通に使われていれば提案しない
  let vocab = null;
  let vocabP = null;

  function isKana(s) { return /^[ぁ-ゖー]+$/.test(s); }
  const isHira = (c) => c >= "ぁ" && c <= "ゖ";

  function loadVocab() {
    if (vocabP) return vocabP;
    vocabP = (async () => {
      const list = [];
      const byKey = new Map();      // 表記(正規化) → 語
      const byReading = new Map();  // 読み → [語]（頻度順）
      const readingsOf = new Map(); // 表記(正規化) → [読み]
      const inflOf = new Map();     // 原形(正規化) → [活用形]（頻度順）
      try {
        await load(); // 拡張子の判定に manifest が要る
        const data = await fetchJson(`${BASE}/suggest${ext()}`);
        for (const it of data.items || []) {
          const e = {
            t: it.t, k: normalizeText(it.t), n: it.n || 0,
            r: (it.r || []).map(normalizeText),
            frag: !!it.f, // 助詞などで始まる断片（って・にね）
          };
          list.push(e);
          if (!byKey.has(e.k)) byKey.set(e.k, e);
          if (e.r.length) readingsOf.set(e.k, e.r);
          for (const r of e.r) {
            if (!byReading.has(r)) byReading.set(r, []);
            byReading.get(r).push(e);
          }
          if (it.b) {
            const b = normalizeText(it.b);
            if (!inflOf.has(b)) inflOf.set(b, []);
            inflOf.get(b).push(e);
          }
        }
        // 他の活用形を含む形は冗長（「楽しかったね」は「楽しかった」で当たる）なので外す
        for (const [b, forms] of inflOf) {
          inflOf.set(b, forms.filter((f) => !forms.some((g) => g !== f && f.k.includes(g.k))));
        }
      } catch (_) {
        // 語彙が無くても検索・補完は表記どおりで動く
      }
      vocab = { list, byKey, byReading, readingsOf, inflOf };
      return vocab;
    })();
    return vocabP;
  }

  // 口語の打ち方をゆるめた形。ひらがなの後ろの のばし棒・波線・小書きの母音と、
  // 末尾の「っ」を外す（おはよー → おはよ、すごーい → すごい、やばっ → やば）。
  // カタカナ語の「ー」（コーヒー）は語の一部なので残す。入力は正規化前の表記。
  function looseForm(raw) {
    const s = (raw || "").normalize("NFKC").toLowerCase();
    let out = "";
    for (const c of s) {
      const prev = out[out.length - 1] || "";
      if ((c === "ー" || c === "~" || c === "〜") && isHira(prev)) continue;
      if ("ぁぃぅぇぉ".includes(c) && isHira(prev)) continue;
      out += c;
    }
    return normalizeText(out.replace(/っ+$/, ""));
  }

  // ローマ字 → ひらがな（ヘボン式・訓令式・IME の打ち方を受ける）。変換しきれなければ null。
  const ROMAJI = (() => {
    const t = {
      a: "あ", i: "い", u: "う", e: "え", o: "お",
      ka: "か", ki: "き", ku: "く", ke: "け", ko: "こ", kya: "きゃ", kyu: "きゅ", kyo: "きょ",
      ga: "が", gi: "ぎ", gu: "ぐ", ge: "げ", go: "ご", gya: "ぎゃ", gyu: "ぎゅ", gyo: "ぎょ",
      sa: "さ", si: "し", shi: "し", su: "す", se: "せ", so: "そ",
      sha: "しゃ", shu: "しゅ", sho: "しょ", she: "しぇ", sya: "しゃ", syu: "しゅ", syo: "しょ",
      za: "ざ", zi: "じ", ji: "じ", zu: "ず", ze: "ぜ", zo: "ぞ",
      ja: "じゃ", ju: "じゅ", jo: "じょ", je: "じぇ", jya: "じゃ", jyu: "じゅ", jyo: "じょ",
      zya: "じゃ", zyu: "じゅ", zyo: "じょ",
      ta: "た", ti: "ち", chi: "ち", tu: "つ", tsu: "つ", te: "て", to: "と",
      cha: "ちゃ", chu: "ちゅ", cho: "ちょ", che: "ちぇ", tya: "ちゃ", tyu: "ちゅ", tyo: "ちょ",
      da: "だ", di: "ぢ", du: "づ", de: "で", do: "ど",
      na: "な", ni: "に", nu: "ぬ", ne: "ね", no: "の", nya: "にゃ", nyu: "にゅ", nyo: "にょ",
      ha: "は", hi: "ひ", hu: "ふ", fu: "ふ", he: "へ", ho: "ほ", hya: "ひゃ", hyu: "ひゅ", hyo: "ひょ",
      fa: "ふぁ", fi: "ふぃ", fe: "ふぇ", fo: "ふぉ",
      ba: "ば", bi: "び", bu: "ぶ", be: "べ", bo: "ぼ", bya: "びゃ", byu: "びゅ", byo: "びょ",
      pa: "ぱ", pi: "ぴ", pu: "ぷ", pe: "ぺ", po: "ぽ", pya: "ぴゃ", pyu: "ぴゅ", pyo: "ぴょ",
      ma: "ま", mi: "み", mu: "む", me: "め", mo: "も", mya: "みゃ", myu: "みゅ", myo: "みょ",
      ya: "や", yu: "ゆ", yo: "よ",
      ra: "ら", ri: "り", ru: "る", re: "れ", ro: "ろ", rya: "りゃ", ryu: "りゅ", ryo: "りょ",
      wa: "わ", wo: "を", vu: "ゔ", "-": "ー",
      xtu: "っ", ltu: "っ", xtsu: "っ", ltsu: "っ",
    };
    for (const v of "aiueo") { t["x" + v] = "ぁぃぅぇぉ"["aiueo".indexOf(v)]; t["l" + v] = t["x" + v]; }
    return t;
  })();
  function romajiToKana(word) {
    const s = word.toLowerCase();
    let out = "";
    let i = 0;
    while (i < s.length) {
      const c = s[i], next = s[i + 1] || "";
      // 子音の重なりは促音（kitte → きって、matcha → まっちゃ）
      if (c === next && /[bcdfghjkmpqrstvwxyz]/.test(c)) { out += "っ"; i++; continue; }
      if (c === "t" && next === "c" && s[i + 2] === "h") { out += "っ"; i++; continue; }
      // n の扱い: 母音・y が続けば な行（表で引く）。それ以外は「ん」。
      // n' は区切り、nn は「ん」。ただし nn の後に母音が続けば ん＋な行
      // （konnichiwa → こん|にち|わ。ヘボン式の書き方）。
      if (c === "n" && !/[aiueoy]/.test(next)) {
        out += "ん";
        const after = s[i + 2] || "";
        i += next === "'" || (next === "n" && !/[aiueoy]/.test(after)) ? 2 : 1;
        continue;
      }
      let hit = false;
      for (const len of [4, 3, 2, 1]) {
        const k = s.slice(i, i + len);
        if (k.length === len && ROMAJI[k]) { out += ROMAJI[k]; i += len; hit = true; break; }
      }
      if (!hit) return null;
    }
    return out;
  }

  // ローマ字の入力に「もしかして」を出す（置き換えはしない）。
  // 変換結果がコーパスに実在する語のときだけ。語の断片（nine → にね）は除く。
  // 英語として普通に使われている語
  // （made → まで、take → たけ など）は、日本語側の頻度が十分に高くない限り提案しない。
  function romajiSuggestion(query) {
    if (!vocab || !/^[a-z' -]+$/i.test(query)) return null;
    const out = [];
    for (const w of query.toLowerCase().split(/\s+/).filter(Boolean)) {
      const kana = romajiToKana(w);
      if (!kana || kana.length < MIN_READING) return null;
      // 「は」と書く助詞を wa と打つことが多い（konnichiwa → こんにちは）
      const cands = [kana];
      if (kana.endsWith("わ")) cands.push(kana.slice(0, -1) + "は");
      let hit = null;
      for (const c of cands) {
        const own = vocab.byKey.get(c);
        if (own && !own.frag) { hit = { t: own.t, n: own.n }; break; }
        // かな表記が語彙に無くても、その読みを持つ漢字の語があれば（tanoshii → 楽しい）
        const kanji = (vocab.byReading.get(c) || [])[0];
        if (kanji) { hit = { t: c, n: kanji.n }; break; }
      }
      if (!hit) return null;
      const latin = vocab.byKey.get(w);
      if (latin && hit.n < latin.n * ROMAJI_RATIO) return null;
      out.push(hit.t);
    }
    return out.length ? out.join(" ") : null;
  }

  // 1語を、それと同じものとして探す表記の組へ展開する。
  //   term: 正規化済みの語  raw: 正規化前の入力（のばし棒の判定に使う）
  //   spelling: 表記ゆれ（漢字⇔かな・のばし棒）  inflect: 活用形
  // 返り値 norm: 照合に使う正規化済みの表記（先頭は入力そのもの）
  //        display: 入力以外に含めた表記（画面に出す。語彙の表記そのまま）
  function expandTerm(term, raw, { spelling = true, inflect = false } = {}) {
    const norm = [term];
    const display = [];
    if (!vocab) return { norm, display };
    const add = (k, shown, cap) => {
      if (norm.length >= cap || norm.includes(k)) return;
      norm.push(k);
      display.push(shown);
    };
    if (spelling) {
      const loose = looseForm(raw || term);
      if (loose !== term && loose.length >= MIN_READING) add(loose, loose, MAX_VARIANTS);
      for (const base of norm.slice()) {
        // かなで打たれたら、それ自体が読み。漢字なら語彙にある読みを引く。
        const reads = isKana(base) ? [base] : vocab.readingsOf.get(base) || [];
        for (const r of reads) {
          if (r.length < MIN_READING) continue;
          add(r, r, MAX_VARIANTS);                                     // 可愛い → かわいい
          for (const e of vocab.byReading.get(r) || []) add(e.k, e.t, MAX_VARIANTS); // かわいい → 可愛い
        }
      }
    }
    if (inflect) {
      const cap = norm.length + MAX_INFLECTIONS;
      for (const base of norm.slice()) {
        for (const e of vocab.inflOf.get(base) || []) add(e.k, e.t, cap); // 楽しい → 楽しかった
      }
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

  return {
    mode: "static", search, context, facets, stats, suggest, prefetch,
    _internals: { romajiToKana, looseForm }, // テスト用（tests/test_client_js.py）
  };
})();

window.Api = Api;
