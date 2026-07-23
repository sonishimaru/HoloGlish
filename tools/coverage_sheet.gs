/**
 * HoloGlish 収集状況スプレッドシート — Google Apps Script
 * ----------------------------------------------------------------------------
 * ライバー別タブに「完了 / 未収集 / 字幕なし / エラー」を自動展開し、
 * 1時間ごとに coverage.json を取得して自動更新する。
 *
 * ▼ 使い方（初回だけ）
 *   1. Google スプレッドシートを新規作成
 *   2. 拡張機能 → Apps Script を開き、このファイルの内容を全部貼り付けて保存
 *   3. 関数 `installHourlyTrigger` を一度実行（初回は権限承認）
 *      → 以後1時間ごとに自動更新。手動更新はメニュー「HoloGlish → 今すぐ更新」
 *
 * データ元（収集のたびに自動更新される）:
 *   https://raw.githubusercontent.com/sonishimaru/HoloGlish/hologlish-data/coverage.json
 *   （公開 Pages 版 https://sonishimaru.github.io/HoloGlish/coverage.json でも可）
 */

const COVERAGE_URL =
  'https://raw.githubusercontent.com/sonishimaru/HoloGlish/hologlish-data/coverage.json';

const STATUS_LABEL = {
  done: '✅ 完了',
  pending: '⏳ 未収集',
  no_subs: '— 字幕なし',
  error: '⚠ エラー',
};
const STATUS_COLOR = {
  '完了': '#e6f4ea',
  '未収集': '#fff7e0',
  '字幕なし': '#f1f3f4',
  'エラー': '#fce8e6',
};

/** メインの更新処理。トリガーとメニューから呼ばれる。 */
function updateCoverage() {
  const resp = UrlFetchApp.fetch(COVERAGE_URL, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('coverage.json の取得に失敗: HTTP ' + resp.getResponseCode());
  }
  const data = JSON.parse(resp.getContentText());
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  writeSummary_(ss, data);

  const keep = { '📊 サマリー': true };
  (data.members || []).forEach(function (m) {
    const name = writeMemberSheet_(ss, m);
    keep[name] = true;
  });

  // データに無くなったライバー別シートは削除（サマリーは残す）
  ss.getSheets().forEach(function (sh) {
    if (!keep[sh.getName()]) ss.deleteSheet(sh);
  });

  ss.toast('更新しました: ' + new Date().toLocaleString());
}

/** サマリー（全体＋ライバー別の集計と進捗）。 */
function writeSummary_(ss, data) {
  const name = '📊 サマリー';
  const sh = ss.getSheetByName(name) || ss.insertSheet(name, 0);
  ss.setActiveSheet(sh);
  ss.moveActiveSheet(1);
  sh.clear();

  const header = ['ライバー', '完了', '未収集', '字幕なし', 'エラー', '合計', '進捗'];
  const rows = (data.members || []).map(function (m) {
    const c = m.counts;
    return [
      m.member_ja || m.member, c.done, c.pending, c.no_subs, c.error, c.total,
      c.total ? c.done / c.total : 0,
    ];
  });
  const s = data.summary || { done: 0, pending: 0, no_subs: 0, error: 0, total: 0 };
  const values = [
    ['最終更新', new Date(), '', '', '', '', ''],
    header,
  ].concat(rows).concat([
    ['合計', s.done, s.pending, s.no_subs, s.error, s.total, s.total ? s.done / s.total : 0],
  ]);

  sh.getRange(1, 1, values.length, header.length).setValues(values);
  sh.getRange(2, 1, 1, header.length).setFontWeight('bold');
  sh.getRange(3, 7, rows.length + 1, 1).setNumberFormat('0%');
  sh.setFrozenRows(2);
  sh.autoResizeColumns(1, header.length);
}

/** ライバー1人分のタブ。動画一覧と状態を書き、状態で色分けする。 */
function writeMemberSheet_(ss, m) {
  const name = (m.member_ja || m.member || '(unknown)').substring(0, 90);
  const sh = ss.getSheetByName(name) || ss.insertSheet(name);
  sh.clear();
  sh.clearConditionalFormatRules();

  const c = m.counts;
  const title =
    (m.member_ja || m.member) +
    ' — 完了 ' + c.done + '/' + c.total +
    '（未収集 ' + c.pending + '・字幕なし ' + c.no_subs + '・エラー ' + c.error + '）';
  const header = ['状態', 'タイトル', 'リンク', 'video_id'];
  const body = (m.videos || []).map(function (v) {
    return [STATUS_LABEL[v.status] || v.status, v.title, v.url, v.video_id];
  });
  const values = [[title, '', '', ''], header].concat(body.length ? body : [['(動画なし)', '', '', '']]);

  sh.getRange(1, 1, values.length, header.length).setValues(values);
  sh.getRange(2, 1, 1, header.length).setFontWeight('bold');
  sh.setFrozenRows(2);
  sh.setColumnWidth(2, 420);

  const n = Math.max(body.length, 1);
  const range = sh.getRange(3, 1, n, header.length);
  const rules = [];
  Object.keys(STATUS_COLOR).forEach(function (key) {
    rules.push(
      SpreadsheetApp.newConditionalFormatRule()
        .whenFormulaSatisfied('=REGEXMATCH($A3,"' + key + '")')
        .setBackground(STATUS_COLOR[key])
        .setRanges([range])
        .build()
    );
  });
  sh.setConditionalFormatRules(rules);
  return name;
}

/** スプレッドシートを開いたときのメニュー。 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('HoloGlish')
    .addItem('今すぐ更新', 'updateCoverage')
    .addItem('1時間ごとの自動更新を有効化', 'installHourlyTrigger')
    .addToMenu();
}

/** 1時間ごとの自動更新トリガーを設置（初回に一度だけ実行）。 */
function installHourlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'updateCoverage') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateCoverage').timeBased().everyHours(1).create();
  updateCoverage();
}
