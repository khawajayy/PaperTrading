"use strict";
// All text from the data files goes through el()/textContent, never innerHTML,
// because headlines come from outside sources.

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "style" || k.includes("-")) node.setAttribute(k, v);
    else node[k] = v;
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) node.append(kid);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

const money = (v, d = 2) => "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const signedMoney = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v));
const pct = (v, d = 2) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%";
const dash = "–";
const num = (v, d = 1) => (v === null || v === undefined ? dash : Number(v).toFixed(d));
const toneOf = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

function price(v) {
  const n = Number(v);
  return n >= 100 ? n.toFixed(2) : n >= 1 ? n.toFixed(3) : n.toFixed(5);
}

const tz = { timeZoneName: "short" };
function when(value) {
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...tz });
}
function ago(ts) {
  const h = (Date.now() / 1000 - ts) / 3600;
  return h < 1 ? `${Math.max(1, Math.round(h * 60))}m ago` : h < 48 ? `${Math.round(h)}h ago` : `${Math.round(h / 24)}d ago`;
}

function table(columns, rows, emptyText) {
  if (!rows.length) return el("p", { class: "empty" }, emptyText);
  const head = el("tr", {}, ...columns.map((c) => el("th", { class: c.n ? "n" : "", scope: "col" }, c.label)));
  const body = rows.map((r) =>
    el("tr", {}, ...columns.map((c) => {
      const out = c.cell(r);
      const cls = [c.n ? "n" : "", c.cls || "", c.tone ? toneOf(c.tone(r)) : ""].join(" ").trim();
      return el("td", { class: cls }, out === null || out === undefined ? dash : out);
    }))
  );
  return el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

function tile(k, v, d, tone) {
  return el("div", { class: "tile" },
    el("div", { class: "k" }, k),
    el("div", { class: "v " + (tone || "") }, v),
    d ? el("div", { class: "d" }, d) : null);
}

function niceTicks(lo, hi, count = 4) {
  if (hi === lo) { hi += 1; lo -= 1; }
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.999; v += step) out.push(Number(v.toFixed(8)));
  return out;
}

// One y-axis, thin lines, crosshair and tooltip on hover or arrow keys.
function lineChart(host, { rows, series, format, height = 260, emptyText, area, label }) {
  host.replaceChildren();
  if (rows.length < 2) {
    host.append(el("p", { class: "empty" }, emptyText));
    return;
  }
  // Draw at the real width so axis text stays readable on a phone.
  const W = Math.max(host.clientWidth, 280), H = height;
  const m = { t: 12, r: W < 500 ? 62 : 80, b: 26, l: W < 500 ? 56 : 66 };
  const xTicks = W < 500 ? 3 : 5;
  const times = rows.map((r) => Date.parse(r.time));
  const all = series.flatMap((s) => rows.map((r) => Number(r[s.key])));
  const ticks = niceTicks(Math.min(...all), Math.max(...all));
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const x = (t) => m.l + ((t - times[0]) / (times[times.length - 1] - times[0] || 1)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0", "aria-label": label });

  for (const t of ticks) {
    root.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1 }));
    const txt = svg("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 12 });
    txt.textContent = format(t);
    root.append(txt);
  }
  const days = (times[times.length - 1] - times[0]) / 864e5;
  for (let i = 0; i < xTicks; i++) {
    const t = times[0] + ((times[times.length - 1] - times[0]) * i) / (xTicks - 1);
    const txt = svg("text", {
      x: x(t), y: H - 7, "text-anchor": i === 0 ? "start" : i === xTicks - 1 ? "end" : "middle", fill: "var(--muted)", "font-size": 12,
    });
    const d = new Date(t);
    txt.textContent = days < 2
      ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    root.append(txt);
  }
  for (const s of series) {
    const pts = rows.map((r, i) => `${x(times[i]).toFixed(1)},${y(Number(r[s.key])).toFixed(1)}`);
    if (area) {
      root.append(svg("polygon", {
        points: `${x(times[0])},${y(Math.min(0, hi))} ${pts.join(" ")} ${x(times[times.length - 1])},${y(Math.min(0, hi))}`,
        fill: s.color, opacity: 0.14,
      }));
    }
    root.append(svg("polyline", {
      points: pts.join(" "), fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }
  // Label each line's end directly, nudging labels apart when the lines finish close together.
  const ends = series.map((s) => ({ s, v: Number(rows[rows.length - 1][s.key]) }))
    .map((e) => ({ ...e, y: y(e.v) })).sort((a, b) => a.y - b.y);
  ends.forEach((e, i) => { if (i && e.y - ends[i - 1].y < 15) e.y = ends[i - 1].y + 15; });
  for (const e of ends) {
    const txt = svg("text", { x: W - m.r + 8, y: e.y + 4, fill: "var(--ink2)", "font-size": 12 });
    txt.textContent = format(e.v);
    root.append(txt);
  }

  const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
  root.append(cross);
  const dots = series.map((s) => {
    const c = svg("circle", { r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    root.append(c);
    return c;
  });
  host.append(root);
  const tip = el("div", { class: "tip", hidden: true });
  host.append(tip);

  let cursor = rows.length - 1;
  const show = (i) => {
    cursor = i;
    const px = x(times[i]);
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    cross.setAttribute("visibility", "visible");
    dots.forEach((c, k) => {
      c.setAttribute("cx", px);
      c.setAttribute("cy", y(Number(rows[i][series[k].key])));
      c.setAttribute("visibility", "visible");
    });
    tip.replaceChildren(
      el("div", {}, when(rows[i].time)),
      ...series.map((s) => el("div", {}, `${s.name}: ${format(Number(rows[i][s.key]))}`))
    );
    tip.hidden = false;
    const left = px;
    tip.style.top = "8px";
    tip.style.left = (left > host.clientWidth * 0.62 ? left - tip.offsetWidth - 12 : left + 12) + "px";
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    dots.forEach((c) => c.setAttribute("visibility", "hidden"));
    tip.hidden = true;
  };
  root.addEventListener("pointermove", (ev) => {
    const box = root.getBoundingClientRect();
    const t = times[0] + (((ev.clientX - box.left) / box.width * W - m.l) / (W - m.l - m.r)) * (times[times.length - 1] - times[0]);
    let best = 0;
    times.forEach((v, i) => { if (Math.abs(v - t) < Math.abs(times[best] - t)) best = i; });
    show(best);
  });
  root.addEventListener("pointerleave", hide);
  root.addEventListener("blur", hide);
  root.addEventListener("focus", () => show(cursor));
  root.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") show(Math.max(0, cursor - 1));
    else if (ev.key === "ArrowRight") show(Math.min(rows.length - 1, cursor + 1));
    else return;
    ev.preventDefault();
  });
}

function scoreBar(score, goal) {
  if (score === null || score === undefined) return dash;
  const size = Math.min(Math.abs(score), 100) / 2;
  const fill = score >= 0
    ? el("div", { class: "fill pos", style: `left:50%;width:${size}%` })
    : el("div", { class: "fill neg", style: `right:50%;width:${size}%` });
  const track = el("div", { class: "track" }, fill, el("div", { class: "zero" }),
    el("div", { class: "goal", style: `left:${50 + goal / 2}%`, title: `Buy threshold: ${goal}` }));
  return el("div", { class: "scorebar", title: `Score ${score}. Buy at ${goal} or higher.` }, track, el("b", {}, score.toFixed(0)));
}

const POINT_NAMES = {
  trend_1h: "1h EMA trend", trend_4h: "4h EMA trend", trend_1d: "Daily EMA trend", rsi: "RSI", macd_sign: "MACD side",
  macd_slope: "MACD slope", adx: "ADX", breakout: "20-hour range", volume: "Volume",
};

function statusOf(s, goal) {
  if (s.error) return s.error;
  if (s.action) return s.action;
  if (s.held) return "Holding";
  if (s.gates && s.gates.length) return s.gates.join("; ");
  return s.final >= goal ? "Ready" : "Waiting";
}

function render(d) {
  $("updated").textContent = "Last run: " + when(d.generated_at);
  const age = (Date.now() - Date.parse(d.generated_at)) / 36e5;
  const bad = Object.entries(d.health).filter(([, h]) => !h.ok).map(([k]) => k);
  const notes = [];
  if (age > 3) notes.push(`The bot last ran ${Math.round(age)} hours ago, so this page may be out of date.`);
  if (bad.length) notes.push(`Some sources had problems on the last run: ${bad.join(", ")}. See Data health.`);
  $("alert").hidden = !notes.length;
  $("alert").textContent = notes.join(" ");

  const st = d.stats, ts = d.trade_stats;
  const excess = st.return_pct - st.benchmark_return_pct;
  $("tiles").replaceChildren(
    tile("Account value", money(d.equity), `Started with ${money(d.starting_cash, 0)} on ${new Date(d.started_at).toLocaleDateString()}`),
    tile("Return", pct(st.return_pct), `${pct(excess)} against buy and hold`, toneOf(st.return_pct)),
    tile("Max drawdown", pct(st.max_drawdown_pct), st.sharpe === null ? "Sharpe appears after 30 days" : `Sharpe ${st.sharpe}`),
    tile("In the market", money(d.exposure, 0), `${money(d.cash, 0)} in cash, ${d.positions.length} open`),
    tile("Closed trades", String(ts.trades), ts.trades ? `${ts.win_rate_pct}% won` : "None yet"),
    tile("Profit factor", ts.profit_factor === null ? dash : String(ts.profit_factor), ts.avg_r === null ? "Needs winning and losing trades" : `Average ${ts.avg_r}R per trade`),
  );

  const curve = d.equity_curve;
  const css = (name) => `var(${name})`;
  $("equity-legend").replaceChildren(
    el("span", {}, el("i", { style: `background:${css("--series-1")}` }), "Bot"),
    el("span", {}, el("i", { style: `background:${css("--series-2")}` }), "Buy and hold, all ten"),
  );
  const whole = (v) => money(v, 0);
  const drawCharts = () => {
    lineChart($("equity-chart"), {
      rows: curve, label: `Account value over time, now ${money(d.equity)}`, format: whole,
      emptyText: "The chart starts after the second hourly run.",
      series: [
        { key: "equity", name: "Bot", color: css("--series-1") },
        { key: "benchmark", name: "Buy and hold", color: css("--series-2") },
      ],
    });
    lineChart($("dd-chart"), {
      rows: curve, height: 170, area: true, label: `Drawdown from peak, worst ${st.max_drawdown_pct}%`,
      format: (v) => v.toFixed(1) + "%", emptyText: "The chart starts after the second hourly run.",
      series: [{ key: "drawdown_pct", name: "Drawdown", color: css("--series-1") }],
    });
  };
  drawCharts();
  let lastWidth = $("equity-chart").clientWidth;
  window.addEventListener("resize", () => {
    const width = $("equity-chart").clientWidth;
    if (width !== lastWidth) {
      lastWidth = width;
      drawCharts();
    }
  });

  $("positions").replaceChildren(table([
    { label: "Token", cls: "sym", cell: (p) => p.symbol },
    { label: "Opened", cell: (p) => when(p.entry_time) },
    { label: "Quantity", n: 1, cell: (p) => String(p.qty) },
    { label: "Entry", n: 1, cell: (p) => price(p.entry_price) },
    { label: "Now", n: 1, cell: (p) => price(p.price) },
    { label: "Stop", n: 1, cell: (p) => price(p.stop) },
    { label: "Value", n: 1, cell: (p) => money(p.value) },
    { label: "P&L", n: 1, tone: (p) => p.pnl, cell: (p) => `${signedMoney(p.pnl)} (${pct(p.pnl_pct)})` },
    { label: "R", n: 1, tone: (p) => p.r, cell: (p) => num(p.r, 2) },
  ], d.positions, "No open positions. The bot is all in cash."));

  $("signal-hint").textContent =
    `Score = indicators, then up to ±15 for news and ±15 for macro. It buys at ${d.entry_score} or higher (black tick) if nothing is blocking the trade. Long only.`;
  $("signal-table").replaceChildren(table([
    {
      label: "Token", cls: "sym",
      cell: (s) => {
        const tag = s.leverage > 1 ? el("span", { class: "tag" }, `${s.group === "semis_short" ? "−" : ""}${s.leverage}x`) : null;
        return el("span", {}, s.symbol, tag, s.held ? el("span", { class: "tag" }, "Held") : null, el("small", {}, s.name));
      },
    },
    { label: "Price", n: 1, cell: (s) => price(s.price) },
    { label: "24h", n: 1, tone: (s) => s.change_24h_pct, cell: (s) => pct(s.change_24h_pct, 1) },
    { label: "Score", cell: (s) => scoreBar(s.final, d.entry_score) },
    { label: "Status", cls: "why", cell: (s) => statusOf(s, d.entry_score) },
    {
      label: "Tech", n: 1,
      cell: (s) => {
        const span = el("span", {}, num(s.tech, 0));
        if (s.points) span.title = Object.entries(s.points).map(([k, v]) => `${POINT_NAMES[k] || k}: ${v > 0 ? "+" : ""}${v}`).join("\n");
        return span;
      },
    },
    { label: "News", n: 1, cell: (s) => num(s.news, 1) },
    { label: "Macro", n: 1, cell: (s) => num(s.macro, 1) },
    { label: "RSI", n: 1, cell: (s) => num(s.rsi, 0) },
    { label: "ADX", n: 1, cell: (s) => num(s.adx, 0) },
    { label: "ATR", n: 1, cell: (s) => (s.atr_pct === null || s.atr_pct === undefined ? dash : s.atr_pct.toFixed(1) + "%") },
    { label: "Spread", n: 1, cell: (s) => s.spread_pct.toFixed(2) + "%" },
    { label: "Vs share", n: 1, cell: (s) => (s.premium_pct === null ? dash : pct(s.premium_pct, 1)) },
  ], d.signals, "No signals yet."));

  const macroNames = { vix: "VIX (fear index)", dxy: "US dollar index", us10y: "US 10-year yield", smh: "Chip stocks (SMH)" };
  $("macro").replaceChildren(table([
    { label: "Reading", cell: (r) => macroNames[r.key] || r.key },
    { label: "Last", n: 1, cell: (r) => r.last.toFixed(2) },
    { label: "20-day avg", n: 1, cell: (r) => r.ma20.toFixed(2) },
    { label: "5-day change", n: 1, cell: (r) => pct(r.chg_5d_pct, 1) },
  ], Object.entries(d.macro).map(([key, v]) => ({ key, ...v })), "No macro data on the last run."));

  const upcoming = [
    ...d.calendar.map((e) => ({ title: e.title, ts: e.ts, kind: "Macro, US" })),
    ...Object.entries(d.earnings).filter(([, ts]) => ts > Date.now() / 1000 - 86400)
      .map(([t, ts]) => ({ title: `${t} earnings`, ts, kind: "Earnings", dateOnly: true })),
  ].sort((a, b) => a.ts - b.ts);
  $("calendar").replaceChildren(table([
    { label: "Event", cell: (e) => e.title },
    { label: "Type", cell: (e) => e.kind },
    { label: "When", cell: (e) => (e.dateOnly ? new Date(e.ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }) : when(e.ts)) },
  ], upcoming, "No high-impact events found."));

  $("news").replaceChildren(...Object.entries(d.news).map(([topic, n]) => {
    const mood = n.veto ? "High-impact bad news" : `Sentiment ${n.sentiment > 0 ? "+" : ""}${n.sentiment.toFixed(2)}`;
    return el("div", {},
      el("h3", {}, topic, el("span", {}, `${mood} · ${n.count} stories`)),
      n.top.length ? el("ul", {}, ...n.top.map((x) => el("li", {},
        el("a", { href: x.link, target: "_blank", rel: "noopener noreferrer" }, x.title),
        el("small", {}, `${x.source} · ${ago(x.ts)}${x.score ? ` · ${x.score > 0 ? "+" : ""}${x.score}` : ""}`)))) : el("p", { class: "empty" }, "No recent stories."));
  }));

  const stat = (k, v) => el("span", {}, k + " ", el("b", {}, v === null || v === undefined ? dash : String(v)));
  $("trade-stats").replaceChildren(
    stat("Net P&L", signedMoney(ts.total_pnl)), stat("Fees paid", money(ts.total_fees)),
    stat("Average win", ts.avg_win === null ? null : money(ts.avg_win)),
    stat("Average loss", ts.avg_loss === null ? null : signedMoney(ts.avg_loss)),
    stat("Average hold", ts.avg_hold_hours === null ? null : ts.avg_hold_hours + "h"),
  );
  $("per-symbol").replaceChildren(d.per_symbol.length ? table([
    { label: "Token", cls: "sym", cell: (x) => x.symbol },
    { label: "Trades", n: 1, cell: (x) => String(x.trades) },
    { label: "Won", n: 1, cell: (x) => String(x.wins) },
    { label: "P&L", n: 1, tone: (x) => x.pnl, cell: (x) => signedMoney(x.pnl) },
  ], d.per_symbol, "") : "");
  $("trade-table").replaceChildren(table([
    { label: "ID", cell: (t) => t.trade_id },
    { label: "Token", cls: "sym", cell: (t) => t.symbol },
    { label: "Opened", cell: (t) => when(t.entry_time) },
    { label: "Closed", cell: (t) => when(t.exit_time) },
    { label: "Entry", n: 1, cell: (t) => price(t.entry_price) },
    { label: "Exit", n: 1, cell: (t) => price(t.exit_price) },
    { label: "P&L", n: 1, tone: (t) => Number(t.pnl), cell: (t) => signedMoney(Number(t.pnl)) },
    { label: "R", n: 1, tone: (t) => Number(t.r_multiple), cell: (t) => Number(t.r_multiple).toFixed(2) },
    { label: "Reason", cls: "why", cell: (t) => t.exit_reason },
  ], d.trades, "No closed trades yet."));

  $("ledger-hint").textContent =
    `${d.ledger_rows} entries in total, newest first. Every change to cash is one row, and the bot rebuilds its balance from these rows on each run.`;
  $("ledger-table").replaceChildren(table([
    { label: "#", n: 1, cell: (r) => r.seq },
    { label: "Time", cell: (r) => when(r.time) },
    { label: "Type", cell: (r) => r.type },
    { label: "Token", cell: (r) => r.symbol || dash },
    { label: "Quantity", n: 1, cell: (r) => (Number(r.qty) ? r.qty : dash) },
    { label: "Price", n: 1, cell: (r) => (Number(r.price) ? price(r.price) : dash) },
    { label: "Amount", n: 1, tone: (r) => Number(r.amount), cell: (r) => signedMoney(Number(r.amount)) },
    { label: "Cash after", n: 1, cell: (r) => money(r.cash_after) },
    { label: "Trade", cell: (r) => r.trade_id || dash },
    { label: "Note", cls: "why", cell: (r) => r.note || dash },
  ], d.ledger, "The ledger is empty."));

  $("health-list").replaceChildren(...Object.entries(d.health).map(([k, h]) =>
    el("div", { class: "status" },
      el("span", { class: "ico " + (h.ok ? "ok" : "bad"), "aria-hidden": "true" }, h.ok ? "✓" : "!"),
      el("b", {}, k),
      el("span", {}, `${h.ok ? "OK" : "Problem"}: ${h.detail}`))));
}

fetch("data/dashboard.json", { cache: "no-store" })
  .then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then(render)
  .catch((e) => {
    $("updated").textContent = "";
    $("alert").hidden = false;
    $("alert").textContent = `Couldn't load the trading record (${e.message}). It appears after the bot's first run.`;
  });
