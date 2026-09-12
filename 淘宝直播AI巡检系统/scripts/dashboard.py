#!/usr/bin/env python3
"""淘宝直播巡检 Web 看板：总览指标 + 每日趋势 + 场次列表 + 高光话术。

启动：.venv/bin/python scripts/dashboard.py [--port 8787] [--host 127.0.0.1]
数据：只读连接 data/inspection.db，不写任何数据，不影响 watcher。
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import sys
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config, now_shanghai, resolve  # noqa: E402

log = logging.getLogger("dashboard")

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>淘宝直播巡检看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --line:#e5e7eb; --text:#1f2329; --muted:#6b7280;
          --accent:#2563eb; --good:#16a34a; --warn:#d97706; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,
         "PingFang SC","Microsoft YaHei",sans-serif; padding:24px; }
  .wrap { max-width:1100px; margin:0 auto; }
  h1 { font-size:22px; font-weight:700; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .card .label { font-size:12px; color:var(--muted); margin-bottom:6px; }
  .card .value { font-size:26px; font-weight:700; }
  .card .value small { font-size:14px; color:var(--muted); font-weight:400; }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:20px; }
  .panel h2 { font-size:15px; font-weight:600; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  tr.clickable { cursor:pointer; }
  tr.clickable:hover td { background:#f0f4ff; }
  .hl-box { display:none; background:#f8fafc; border:1px dashed var(--line); border-radius:8px;
            padding:10px 14px; margin:6px 0 10px; }
  .hl-box.open { display:block; }
  .hl-item { margin-bottom:8px; font-size:13px; line-height:1.6; }
  .hl-item .t { color:var(--accent); font-weight:600; margin-right:6px; }
  .hl-item .q { color:#374151; }
  .num { font-variant-numeric:tabular-nums; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; color:#fff; }
  .badge.ok { background:var(--good); } .badge.warn { background:var(--warn); }
  .empty { color:var(--muted); text-align:center; padding:24px 0; }
  footer { color:var(--muted); font-size:12px; text-align:center; margin-top:8px; }
  @media (max-width:640px){ body{padding:12px;} th,td{padding:6px;} }
</style>
</head>
<body>
<div class="wrap">
  <h1>📺 淘宝直播巡检看板</h1>
  <div class="sub" id="subtitle">加载中…</div>

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>近 24 小时智能分析运行质量</h2>
    <div class="cards" id="intelligence-cards" style="margin-bottom:0"></div>
  </div>

  <div class="panel">
    <h2>近 7 天成交趋势（每日经营数据）</h2>
    <canvas id="trend" height="90"></canvas>
  </div>

  <div class="panel">
    <h2>场次明细（近 7 天，点击展开高光话术）</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr>
          <th>场次</th><th>主播</th><th>开始</th><th>时长</th>
          <th>成交金额</th><th>成交人数</th><th>最高在线</th><th>观看</th><th></th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div id="empty" class="empty" style="display:none;">近 7 天暂无场次数据</div>
  </div>

  <footer>数据来源：data/inspection.db（只读）· 千牛冻结/实时数据口径见单场报告</footer>
</div>

<script>
const days = 7;
async function load() {
  const [ov, streams] = await Promise.all([
    fetch('/api/overview?days=' + days).then(r => r.json()),
    fetch('/api/streams?days=' + days).then(r => r.json()),
  ]);
  document.getElementById('subtitle').textContent =
    '更新于 ' + new Date().toLocaleString('zh-CN') + ' · 近 ' + days + ' 天 ' + ov.total_streams + ' 场';
  renderCards(ov);
  renderIntelligence(ov.intelligence);
  renderTrend(ov);
  renderRows(streams);
}
function fmt(v, digits = 0) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
}
function renderCards(ov) {
  const items = [
    ['近7天场次', fmt(ov.total_streams) + ' <small>场</small>', ''],
    ['近7天成交', '¥' + fmt(ov.total_gmv, 0), ''],
    ['今日场次', fmt(ov.today_streams) + ' <small>场</small>', ''],
    ['今日成交', '¥' + fmt(ov.today_gmv, 0), ''],
  ];
  document.getElementById('cards').innerHTML = items.map(([label, value]) =>
    `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
}
function renderIntelligence(metrics) {
  const target = document.getElementById('intelligence-cards');
  if (!metrics || metrics.total_terminal === 0) {
    target.innerHTML = '<div class="empty">近 24 小时暂无智能分析终态</div>';
    return;
  }
  const pct = value => value === null || value === undefined ? '—' : fmt(value * 100, 1) + '%';
  const codes = (metrics.top_rejection_codes || []).map(item => item.code + ' (' + item.count + ')').join('、') || '—';
  const items = [
    ['成功率', pct(metrics.success_rate)],
    ['校验接受率', pct(metrics.acceptance_rate)],
    ['P50 / P95 耗时', fmt(metrics.p50_latency_ms) + ' / ' + fmt(metrics.p95_latency_ms) + ' ms'],
    ['结构修复', fmt(metrics.repair_count) + ' 次'],
    ['主要拒绝码', codes],
  ];
  target.innerHTML = items.map(([label, value]) =>
    `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
}
function renderTrend(ov) {
  const ctx = document.getElementById('trend');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: ov.daily.map(d => d.date.slice(5)),
      datasets: [{
        label: '成交金额(元)',
        data: ov.daily.map(d => d.gmv),
        borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.12)',
        fill: true, tension: .3, pointRadius: 3,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { callback: v => '¥' + v } } },
    },
  });
}
function renderRows(streams) {
  const tbody = document.getElementById('rows');
  if (!streams.length) { document.getElementById('empty').style.display = 'block'; return; }
  tbody.innerHTML = streams.map((s, i) => {
    const hl = s.highlights.map(h => `<div class="hl-item"><span class="t">${h.reasons}</span>
        <span class="q">“${h.text}”</span></div>`).join('');
    return `<tr class="clickable" onclick="toggle(${i})">
      <td class="num">#${s.id}</td>
      <td>${s.anchor}</td>
      <td>${s.started_at.slice(5,16)}</td>
      <td class="num">${fmt(s.duration_min)} 分</td>
      <td class="num"><b>¥${fmt(s.pay_amt)}</b></td>
      <td class="num">${fmt(s.buyer_cnt)}</td>
      <td class="num">${fmt(s.max_online_uv)}</td>
      <td class="num">${fmt(s.viewer_uv)}</td>
      <td><span class="badge ${s.hl_count ? 'ok' : 'warn'}">${s.hl_count} 高光</span></td>
    </tr>
    <tr><td colspan="9" style="padding:0;border:0;">
      <div class="hl-box" id="hl-${i}">${hl || '<div class="empty">本场暂无高光话术</div>'}
      <div style="margin-top:8px;font-size:12px;color:var(--muted);">完整报告：${s.report || '—'}</div></div>
    </td></tr>`;
  }).join('');
}
function toggle(i) {
  const box = document.getElementById('hl-' + i);
  box.classList.toggle('open');
}
load();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "dashboard/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            if self.path.split("?", 1)[0] == "/api/overview":
                self._send(200, json.dumps(overview(self.server.db, self.server.days),
                                           ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif self.path.split("?", 1)[0] == "/api/streams":
                self._send(200, json.dumps(streams(self.server.db, self.server.days),
                                           ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif self.path.split("?", 1)[0] == "/":
                self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # 任何异常都返回 JSON，避免页面白屏
            log.exception("请求处理失败")
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _since(days: int) -> str:
    return (now_shanghai() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def overview(conn: sqlite3.Connection, days: int) -> dict:
    since = _since(days)
    streams = conn.execute(
        """SELECT s.id, s.started_at, m.pay_amt FROM streams s
           LEFT JOIN stream_metrics m ON m.stream_id = s.id
           WHERE s.status = 'reported' AND s.started_at >= ?""", (since,)).fetchall()

    def _known_sum(rows) -> float | None:
        values = [float(row["pay_amt"]) for row in rows if row["pay_amt"] is not None]
        return round(sum(values), 2) if values else None

    total_gmv = _known_sum(streams)
    today = now_shanghai().strftime("%Y-%m-%d")
    today_rows = [r for r in streams if (r["started_at"] or "").startswith(today)]
    today_gmv = _known_sum(today_rows)

    daily_columns = {row["name"] for row in conn.execute(
        "PRAGMA table_info(daily_metrics)").fetchall()}
    pay_expression = (
        "CASE WHEN data_state='complete' THEN pay_amt END AS pay_amt"
        if "data_state" in daily_columns else "pay_amt"
    )
    daily_rows = conn.execute(
        f"""SELECT date, {pay_expression} FROM daily_metrics
            WHERE date >= ? ORDER BY date""",
        ((now_shanghai() - timedelta(days=days)).strftime("%Y%m%d"),)).fetchall()
    by_date = {r["date"]: (float(r["pay_amt"]) if r["pay_amt"] is not None else None)
               for r in daily_rows}
    daily = []
    for offset in range(days - 1, -1, -1):
        d = now_shanghai() - timedelta(days=offset)
        key = d.strftime("%Y%m%d")
        label = d.strftime("%Y-%m-%d")
        value = by_date.get(key)
        # 今日 daily_metrics 尚未 T+1 结算时，仅在缺失时用场次数据补充；
        # 真实 0 不得被当成缺失。
        if label == today and value is None and today_gmv is not None:
            value = today_gmv
        daily.append({"date": label, "gmv": round(value, 2) if value is not None else None})
    return {
        "total_streams": len(streams),
        "total_gmv": total_gmv,
        "today_streams": len(today_rows),
        "today_gmv": today_gmv,
        "daily": daily,
        "intelligence": intelligence_observability(conn),
    }


def _safe_json(value: object, default: object) -> object:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    lower, upper = int(offset), min(len(ordered) - 1, int(offset) + 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (offset - lower))


def intelligence_observability(conn: sqlite3.Connection) -> dict:
    """Return aggregate-only intelligence health for the last 24 hours.

    Persisted artifacts are untrusted: malformed JSON contributes neither
    accepted content nor error details.  This endpoint deliberately returns no
    transcript, model response, prompt, credential, or raw artifact field.
    """
    empty = {
        "total_terminal": 0,
        "success_rate": None,
        "acceptance_rate": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "repair_count": 0,
        "top_rejection_codes": [],
    }
    try:
        rows = conn.execute(
            """SELECT j.status,a.validated_result_json,a.rejected_json,a.latency_ms
                 FROM intelligence_jobs j
                 LEFT JOIN intelligence_artifacts a ON a.job_key=j.job_key
                 WHERE j.updated_at >= datetime('now','localtime','-1 day')
                   AND j.status IN ('ready','blocked')""",
        ).fetchall()
    except sqlite3.DatabaseError:
        return empty
    if not rows:
        return empty

    terminal = len(rows)
    success = sum(str(row["status"] or "") == "ready" for row in rows)
    accepted = rejected_count = repair_count = 0
    latency: list[int] = []
    rejection_codes: dict[str, int] = {}
    for row in rows:
        result = _safe_json(row["validated_result_json"], None)
        if isinstance(result, dict):
            for field in ("observations", "reusable_talktracks", "action_experiments"):
                values = result.get(field)
                if isinstance(values, list):
                    accepted += len(values)
            try:
                value = int(row["latency_ms"] or 0)
            except (TypeError, ValueError):
                value = 0
            if value >= 0:
                latency.append(value)
        rejected = _safe_json(row["rejected_json"], None)
        if not isinstance(rejected, list):
            continue
        for item in rejected:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                rejected_count += 1
                rejection_codes[code] = rejection_codes.get(code, 0) + 1
            if "structure_repair_attempted" in str(item.get("detail") or ""):
                repair_count += 1
    denominator = accepted + rejected_count
    top_codes = [
        {"code": code, "count": count}
        for code, count in sorted(rejection_codes.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return {
        "total_terminal": terminal,
        "success_rate": round(success / terminal, 4),
        "acceptance_rate": (round(accepted / denominator, 4) if denominator else None),
        "p50_latency_ms": _percentile(latency, 0.5),
        "p95_latency_ms": _percentile(latency, 0.95),
        "repair_count": repair_count,
        "top_rejection_codes": top_codes,
    }


def streams(conn: sqlite3.Connection, days: int) -> list[dict]:
    since = _since(days)
    rows = conn.execute(
        """SELECT s.id, a.name AS anchor, s.started_at, s.ended_at, s.duration_sec,
                  m.pay_amt, m.buyer_cnt, m.max_online_uv, m.viewer_uv,
                  s.file_path
           FROM streams s
           LEFT JOIN anchors a ON a.id = s.anchor_id
           LEFT JOIN stream_metrics m ON m.stream_id = s.id
           WHERE s.status = 'reported' AND s.started_at >= ?
           ORDER BY s.started_at DESC LIMIT 60""", (since,)).fetchall()
    out = []
    from app.highlight.peak import has_data_peak_reason
    for r in rows:
        hls = conn.execute(
            """SELECT reasons, transcript FROM highlights
               WHERE stream_id = ? AND transcript != ''
               ORDER BY score DESC LIMIT 6""", (r["id"],)).fetchall()
        highlights = []
        for h in hls:
            try:
                reasons = json.loads(h["reasons"])
                reasons = "、".join(reasons) if isinstance(reasons, list) else str(reasons)
            except ValueError:
                reasons = str(h["reasons"])
            # 展示只保留数据驱动峰值（成交/点击/在线），声学峰值是内部信号。
            if not has_data_peak_reason(reasons):
                continue
            highlights.append({
                "reasons": html.escape(reasons),
                "text": html.escape(h["transcript"]).replace("\n", "<br>"),
            })
        out.append({
            "id": r["id"],
            "anchor": html.escape(r["anchor"] or "未知"),
            "started_at": r["started_at"] or "",
            "duration_min": round((r["duration_sec"] or 0) / 60, 1),
            "pay_amt": r["pay_amt"],
            "buyer_cnt": r["buyer_cnt"],
            "max_online_uv": r["max_online_uv"],
            "viewer_uv": r["viewer_uv"],
            "hl_count": len(highlights),
            "highlights": highlights,
            "report": html.escape(Path(r["file_path"] or "").name or ""),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="淘宝直播巡检 Web 看板")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()
    db_path = resolve(cfg["paths"]["db"])

    class Server(ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            self.db = _connect(db_path)
            self.days = args.days
            super().__init__(*a, **kw)

    server = Server((args.host, args.port), DashboardHandler)
    log.info("看板已启动：http://%s:%s/（数据：%s）", args.host, args.port, db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
