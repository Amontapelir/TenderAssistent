// Веб-версия TenderAssistant: читает готовые файлы из папки data и строит страницу.
// Любой текст из файлов вставляется только как текст, не как разметка:
// в карточках есть цитаты из чужих документов.
"use strict";

const DATA = "data/";
const PAGE = 20; // сколько строк списка показывается за один раз

// Данные берутся либо из готовых файлов (GitHub Pages), либо прямо с локального сервера.
// Режим выбирает local.js: detectLocal() сообщает, что страница открыта с этого компьютера.
let LIVE = false;
let booted = false; // после первой отрисовки числа не пересчитываются заново
let currentOpen = null; // открыть карточку по номеру; ставит renderCards
const LIVE_URL = { "index.json": "api/index", "profile.json": "api/profile",
  "tenders.json": "api/tenders", "backtest.json": "api/backtest", "shortlist.json": "api/shortlist" };

const LEVEL = { high: "высокий", medium: "средний", low: "низкий", unknown: "не определён" };
const AREA = { high: "совпадает", medium: "близка", low: "смежная", none: "не совпадает", unknown: "не определена" };
const IMPORTANCE = { high: "важное", medium: "существенное", low: "рутинное" };
const CONFIDENCE = { high: "высокая", medium: "средняя", low: "низкая" };
const STATUS = { met: "выполнено", not_met: "не выполнено", unknown: "неизвестно, проверить внутри компании" };
const CATEGORY = {
  license: "лицензии и допуски", experience: "опыт", staff: "персонал", equipment: "оборудование",
  security: "обеспечение", deadline: "сроки", documents: "документы", restriction: "ограничения",
  other: "прочее",
};
const VERDICT = { works: ["работает", "good"], inverted: ["наоборот", "bad"],
  indistinguishable: ["не различает", "warn"], too_few: ["мало данных", ""] };
const SOURCE_KIND = { tender: "закупка", profile: "профиль", contract: "контракты", document: "документ" };

// --- построение разметки ---

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function link(href, text, className) {
  // Ссылка из файла данных открывается, только если это обычный адрес https.
  if (typeof href !== "string" || !href.startsWith("https://")) return el("span", className, text);
  const a = el("a", className, text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function button(className, onClick, ...children) {
  const node = el("button", className, ...children);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

/** Ширина полосы задаётся переменной из скрипта: встроенных стилей в разметке нет. */
function sized(node, name, share) {
  node.style.setProperty(name, `${Math.max(0, Math.min(100, share * 100)).toFixed(2)}%`);
  return node;
}

/** Вставляет детей в узел, пропуская пустые значения: replaceChildren напечатал бы «null». */
const fill = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
const tag = (text, kind) => el("span", `tag${kind ? " tag-" + kind : ""}`, text);
const stat = (value, label, sub) => el("div", "stat", el("b", "", value), el("span", "", label), sub ? el("small", "", sub) : null);
const fact = (label, value) => el("div", "fact", el("dt", "", label), el("dd", "", value));
const bullets = (items) => el("ul", "bullets", items.map((text) => el("li", "", text)));
const limits = (title, items) => el("div", "limits", el("p", "label", title), bullets(items));

/** Список порциями: кнопка «Показать ещё» и счётчик «показано N из M». */
function paged(items, render, className, noun) {
  const box = el("div", className);
  const more = el("div", "more");
  let shown = 0;
  function step() {
    for (const item of items.slice(shown, shown + PAGE)) box.append(render(item));
    shown = Math.min(items.length, shown + PAGE);
    more.replaceChildren();
    if (shown < items.length) {
      more.append(el("span", "", `Показано ${shown} из ${items.length} ${noun}`),
        button("btn btn-line", step, `Показать ещё ${Math.min(PAGE, items.length - shown)}`));
    } else if (items.length > PAGE) {
      more.append(el("span", "", `Показаны все ${items.length} ${noun}`));
    }
  }
  step();
  return el("div", "", box, more);
}

/** Вкладки: содержимое строится при первом открытии. */
function tabs(defs, label) {
  const bar = el("div", "tabs");
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", label);
  const pane = el("div", "pane");
  pane.setAttribute("role", "tabpanel");
  const built = new Map();
  const buttons = defs.map((def, i) => {
    const b = button("tab", () => select(i), def.title,
      def.count === undefined ? null : el("span", "count", def.count));
    b.setAttribute("role", "tab");
    return b;
  });
  function select(i) {
    buttons.forEach((b, n) => {
      b.setAttribute("aria-selected", String(n === i));
      b.tabIndex = n === i ? 0 : -1;
    });
    if (!built.has(i)) built.set(i, el("div", "pane-in", defs[i].build()));
    pane.replaceChildren(...built.get(i).childNodes.length ? [built.get(i)] : []);
  }
  bar.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const next = (buttons.findIndex((b) => b.getAttribute("aria-selected") === "true") + step + buttons.length) % buttons.length;
    select(next);
    buttons[next].focus();
  });
  bar.append(...buttons);
  select(0);
  return [bar, pane];
}

// --- движение ---

const CALM = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Число дорастает до значения за 0,9 с. При просьбе уменьшить движение стоит сразу.
 * Настоящее число уже стоит в разметке, а ноль подставляется только по первому кадру анимации:
 * если кадров нет (фоновая вкладка, снимок страницы, превью ссылки), остаётся настоящее число. */
function countUp(node, target, format) {
  if (CALM || booted || typeof target !== "number") return;
  const whole = Number.isInteger(target);
  let started = null;
  const tick = (now) => {
    if (started === null) started = now;
    const t = Math.min(1, (now - started) / 900);
    const value = target * (1 - Math.pow(1 - t, 3));
    node.textContent = format(t === 1 ? target : (whole ? Math.round(value) : value));
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/** Блоки появляются при прокрутке; шаги с небольшой задержкой друг за другом. */
function reveal() {
  if (CALM || !("IntersectionObserver" in window)) return;
  const nodes = document.querySelectorAll(
    ".block-head, .steps, .step, .bench, #shortlist-body > *, #check-body > *, #tenders-body > *, #profile-body > *");
  let observed = false;
  const seen = new IntersectionObserver((entries) => {
    observed = true;
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("in");
      seen.unobserve(entry.target);
    }
  }, { rootMargin: "0px 0px -6% 0px", threshold: 0.02 });
  nodes.forEach((node) => {
    node.classList.add("reveal");
    if (node.classList.contains("step")) {
      node.style.setProperty("--d", `${[...node.parentNode.children].indexOf(node) * 70}ms`);
    }
    seen.observe(node);
  });
  // Если наблюдатель так и не сработал (страницу не рисуют: снимок, превью), блоки не остаются скрытыми.
  setTimeout(() => { if (!observed) nodes.forEach((node) => node.classList.add("in")); }, 2000);
}

/** Полоса прокрутки под шапкой и подсветка текущего раздела в меню. */
function chrome() {
  const bar = document.getElementById("progress");
  const links = [...document.querySelectorAll(".nav a")];
  const sections = links.map((a) => document.querySelector(a.getAttribute("href")));
  let queued = false;
  const update = () => {
    queued = false;
    const max = document.documentElement.scrollHeight - innerHeight;
    bar.style.setProperty("--p", max > 0 ? (scrollY / max).toFixed(4) : "0");
    let current = -1;
    sections.forEach((s, i) => { if (s && s.getClientRects().length && s.getBoundingClientRect().top < innerHeight * 0.35) current = i; });
    links.forEach((a, i) => (i === current ? a.setAttribute("aria-current", "true") : a.removeAttribute("aria-current")));
  };
  addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(update); } }, { passive: true });
  addEventListener("resize", update);
  update();
}

// --- форматы ---

const money = (value) => {
  if (typeof value !== "number" || !isFinite(value)) return "нет данных";
  if (value >= 1e9) return (value / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " млрд ₽";
  if (value >= 1e6) return (value / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн ₽";
  return value.toLocaleString("ru-RU", { maximumFractionDigits: 0 }) + " ₽";
};
const percent = (value, digits = 0) => (typeof value === "number" && isFinite(value)
  ? (value * 100).toLocaleString("ru-RU", { maximumFractionDigits: digits }) + "%" : "нет данных");
const day = (value) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return m ? `${m[3]}.${m[2]}.${m[1]}` : "не указано";
};
const number = (value) => (typeof value === "number" ? value.toLocaleString("ru-RU") : "нет данных");
const range = (low, high) => (low === null || low === undefined ? "" : `${low}–${high} из 100`);

function dataUrl(name) {
  if (!LIVE) return DATA + name;
  const card = /^cards\/(\d+)\.json$/.exec(name);
  return card ? `api/cards/${card[1]}` : LIVE_URL[name];
}

async function load(name) {
  const response = await fetch(dataUrl(name), { cache: "no-cache" });
  if (!response.ok) throw new Error(`${name}: ответ ${response.status}`);
  return response.json();
}

// --- вступление ---

function renderIntro(index, profile, backtest) {
  const kpi = (label, value, sub, target, format) => {
    const figure = el("span", "", value);
    countUp(figure, target, format);
    return el("div", "kpi", el("dt", "", label), el("dd", "", figure, el("small", "", sub)));
  };
  const items = [];
  if (profile) {
    const p = profile.profile;
    items.push(kpi("Контрактов в профиле", number(p.contracts), `ПАО «ВымпелКом», ${day(p.period_from)} – ${day(p.period_to)}`, p.contracts, number));
  }
  if (backtest) {
    const r = backtest.report;
    items.push(kpi("Закупок в проверке", number(r.won.total + r.other.total), `${number(r.won.total)} выигранных и ${number(r.other.total)} чужих`, r.won.total + r.other.total, number));
    const w = r.separation_comparable;
    items.push(kpi("Выигранная оценена выше чужой, среди похожих закупок", percent(w.value),
      `в парах, интервал ${percent(w.low)}–${percent(w.high)}; 50% значило бы «не различает»`, w.value, percent));
  }
  items.push(kpi("Карточек закупок", number(index.cards.length), "с требованиями и цитатами из документации", index.cards.length, number));
  document.getElementById("kpis").replaceChildren(...items);
  document.getElementById("bar-note").textContent = LIVE ? "локальный режим" :
    `демонстрация · данные от ${day(index.generated_at)}`;
  const note = document.getElementById("demo-note");
  note.textContent = LIVE ? "Локальный режим: данные читаются с вашего компьютера, новые результаты появляются сразу после действий в разделе «Управление»." :
    `Это демонстрация на готовых файлах: данные собраны ${day(index.generated_at)} и не обновляются. ` +
    "Поиск и новый анализ здесь недоступны, для них программу нужно запустить у себя.";
  note.hidden = false;
}

// --- карточка закупки ---

// --- шкала оценки и факторы ---

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(name, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  node.append(...children);
  return node;
}

/** Точка на полукруге: 0 слева, 100 справа. */
function arcPoint(value) {
  const angle = Math.PI * (1 - value / 100);
  return [100 + 80 * Math.cos(angle), 100 - 80 * Math.sin(angle)];
}

function arc(from, to) {
  const [x1, y1] = arcPoint(from);
  const [x2, y2] = arcPoint(to);
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A 80 80 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/** Полукруг с зонами «низкий / средний / высокий» и диапазоном оценки поверх. */
function gauge(ch) {
  const picture = svg("svg", { viewBox: "0 0 200 108", class: "gauge-svg", "aria-hidden": "true" },
    svg("path", { d: arc(0.6, 34.4), class: "g-zone g-low" }),
    svg("path", { d: arc(35.6, 59.4), class: "g-zone g-medium" }),
    svg("path", { d: arc(60.6, 99.4), class: "g-zone g-high" }));
  if (ch.range_low !== null) {
    picture.append(svg("path", {
      d: arc(Math.max(1, ch.range_low), Math.min(99, ch.range_high)), class: `g-range g-range-${ch.level}`, pathLength: 1 }));
  }
  const box = el("div", "gauge", picture,
    el("div", "gauge-text", el("strong", `level-${ch.level}`, LEVEL[ch.level] || ch.level),
      el("span", "", ch.range_low === null ? "нет данных" : `${ch.range_low}–${ch.range_high} из 100`)),
    el("div", "gauge-ends", el("span", "", "0"), el("span", "", "35"), el("span", "", "60"), el("span", "", "100")));
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", ch.range_low === null ? "Оценка не рассчитана"
    : `Оценка ${LEVEL[ch.level] || ch.level}, диапазон ${ch.range_low}–${ch.range_high} из 100`);
  return box;
}

/** Факторы полосами от нуля: вправо прибавляют, влево отнимают. */
function factorRows(ch) {
  const max = Math.max(25, ...ch.factors.map((f) => Math.abs(f.points)));
  return el("div", "factors", ch.factors.map((f) => el("div", "factor",
    el("span", "factor-text", f.explanation),
    el("span", `fbar ${f.points < 0 ? "neg" : "pos"}`, sized(el("i", ""), "--w", Math.abs(f.points) / max / 2)),
    el("b", f.points > 0 ? "plus" : (f.points < 0 ? "minus" : "zero"), (f.points > 0 ? "+" : "") + f.points))));
}

function verdict(card) {
  const ch = card.chance;
  const total = ch.factors.reduce((sum, f) => sum + f.points, 0);
  return el("div", "verdict",
    el("div", "verdict-gauge",
      el("p", "label", "Оценка шансов"),
      gauge(ch),
      el("p", "small", ch.range_low === null ? "Данных для оценки нет."
        : `Уверенность ${CONFIDENCE[ch.confidence] || ch.confidence}. Это сумма факторов, а не вероятность победы.`)),
    el("div", "verdict-factors",
      el("div", "factors-head", el("p", "label", "Из чего сложилась оценка"),
        el("span", `factors-total ${total > 0 ? "plus" : (total < 0 ? "minus" : "zero")}`,
          `сумма ${total > 0 ? "+" : ""}${total}`)),
      factorRows(ch)));
}

const WARNING_ICON = () => svg("svg", { viewBox: "0 0 24 24", class: "ico", "aria-hidden": "true" },
  svg("path", { d: "m10.3 3.9-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3.1l-8-14a2 2 0 0 0-3.4 0Z" }),
  svg("path", { d: "M12 9v4M12 17h.01" }));

function paneSummary(card) {
  const pr = card.price;
  return [
    el("div", "cols",
      el("div", "box",
        el("p", "label", "Область закупки"),
        el("div", "chips", card.tender.okpd_codes.map((c) => el("span", "tag code", c)),
          tag(`по кодам: ${AREA[card.area_match.level] || card.area_match.level}`,
            { high: "good", medium: "warn", low: "warn", none: "bad" }[card.area_match.level])),
        el("p", "", card.area_match.explanation),
        card.area_comment ? el("blockquote", "quote model", card.area_comment) : null,
        card.area_comment ? el("p", "small", `По смыслу модель считает область: ${AREA[card.area_level_by_meaning] || "не оценила"}. Эта оценка неустойчива и при повторном запуске может измениться.`) : null),
      el("div", "box",
        el("p", "label", "Ожидаемая цена контракта"),
        el("p", "big", pr.expected_price === null ? "не названа" : money(pr.expected_price)),
        pr.expected_price === null ? null : el("p", "", `Начальная цена ${money(pr.initial_price)}. Диапазон ${money(pr.low_price)} – ${money(pr.high_price)}, ` +
          `обычное снижение ${percent(pr.expected_reduction, 1)} по ${number(pr.observations)} закупкам.`),
        el("p", "small", pr.note))),
    card.winning_hypotheses.length ? el("div", "",
      el("p", "label", "Гипотезы модели о прошлых закупках заказчика"),
      el("div", "items", card.winning_hypotheses.map((x) => el("div", "req",
        el("div", "req-top", tag(`уверенность ${CONFIDENCE[x.confidence] || x.confidence}`)),
        el("p", "", x.text),
        el("p", "small", "Основания: " + x.based_on.map((id) => sourceTitle(card, id)).join("; ")))))) : null,
    card.limitations.length ? limits("Что проверить не удалось", card.limitations.concat(card.chance.limitations)) : null,
  ];
}

function sourceTitle(card, id) {
  const source = card.sources.find((s) => s.id === id);
  return source ? source.title : id;
}

function paneRequirements(card) {
  const order = { high: 0, medium: 1, low: 2 };
  const all = [...card.requirements].sort((a, b) => order[a.importance] - order[b.importance]);
  if (!all.length) return [el("p", "empty", "Требования не извлечены: документация не прочитана.")];
  const holder = el("div", "");
  const filters = [["all", "Все"], ["high", "Важные"], ["medium", "Существенные"], ["low", "Рутинные"]];
  const chips = filters.map(([key, title]) => {
    const n = key === "all" ? all.length : all.filter((r) => r.importance === key).length;
    return button("chip", () => show(key), title, el("span", "num", n));
  });
  function show(key) {
    chips.forEach((chip, i) => chip.setAttribute("aria-pressed", String(filters[i][0] === key)));
    const rows = key === "all" ? all : all.filter((r) => r.importance === key);
    holder.replaceChildren(rows.length ? paged(rows, (r) => {
      const src = card.sources.find((x) => x.id === r.source_id) || {};
      return el("article", `req req-${r.importance}`,
        el("div", "req-main",
          el("div", "req-top", el("span", "req-n", String(all.indexOf(r) + 1).padStart(2, "0")),
            tag(IMPORTANCE[r.importance] || r.importance, r.importance === "high" ? "ink" : ""),
            tag(CATEGORY[r.category] || r.category)),
          el("h4", "", r.requirement),
          el("dl", "req-facts",
            el("div", "", el("dt", "", "Статус компании"), el("dd", "", STATUS[r.company_status] || r.company_status)),
            el("div", "", el("dt", "", "Что нужно"), el("dd", "", r.gap || "не указано"))),
          el("p", "req-src", "Источник: ", link(src.url, src.title || r.source_id))),
        el("blockquote", "quote", r.quote));
    }, "items", "требований") : el("p", "empty", "Таких требований нет."));
  }
  show("high");
  return [
    el("div", "chipbar", chips),
    el("p", "small", "Соответствие компании требованиям по открытым данным не проверить: статус показывает, что нужно выяснить внутри."),
    holder,
    card.dropped_findings.length ? el("details", "dropped",
      el("summary", "", `Не прошли проверку цитат и в карточку не попали: ${card.dropped_findings.length}`),
      bullets(card.dropped_findings.map((d) => `${d.text} Причина: ${d.reason}.`))) : null,
  ];
}

function paneCustomer(card) {
  const h = card.customer_history;
  return [
    el("div", "stats",
      stat(number(h.contracts), "контрактов заказчика", h.period_from ? `${day(h.period_from)} – ${day(h.period_to)}` : ""),
      stat(number(h.relevant_contracts), "похожих на закупку", `поставщик известен у ${number(h.suppliers_known_for)}`),
      stat(number(h.company_wins), "контрактов компании", "с этим заказчиком"),
      stat(percent(h.reductions.no_reduction_share), "без снижения цены", `по ${number(h.reductions.observations)} закупкам`),
      stat(percent(h.reductions.median, 1), "обычное снижение", "медиана")),
    h.winners.length ? el("div", "table-wrap", el("table", "",
      el("caption", "", "Кто выигрывал похожие закупки"),
      el("thead", "", el("tr", "", el("th", "", "Поставщик"), el("th", "r", "Контрактов"), el("th", "r", "На сумму"))),
      el("tbody", "", h.winners.map((w) => el("tr", "", el("th", "", w.name || `ИНН ${w.inn}`),
        el("td", "r", number(w.contracts)), el("td", "r", money(w.total)))))))
      : el("p", "empty", "Победители похожих закупок не выяснены."),
    h.examples.length ? el("div", "table-wrap", el("table", "",
      el("caption", "", "Похожие прошлые контракты"),
      el("thead", "", el("tr", "", el("th", "", "Дата"), el("th", "", "Контракт"), el("th", "r", "Цена"),
        el("th", "r", "Снижение"), el("th", "", "Поставщик"))),
      el("tbody", "", h.examples.map((e) => el("tr", "", el("td", "r", day(e.published)),
        el("td", "t-title", e.title, el("small", "", e.placing_way || "способ не указан")),
        el("td", "r", money(e.price)), el("td", "r", percent(e.reduction, 1)),
        el("td", "", e.supplier_names.length ? e.supplier_names.join(", ") : "не выяснен")))))) : null,
    h.limitations.length ? limits("Ограничения истории", h.limitations) : null,
  ];
}

function paneAttention(card) {
  if (!card.attention.length) return [el("p", "empty", "Замечаний нет.")];
  return [paged(card.attention, (a) => el("article", "att",
    el("span", "att-ico", WARNING_ICON()),
    el("div", "att-body", el("h4", "", a.text),
      a.quote ? el("blockquote", "quote", a.quote) : null,
      el("p", "small", sourceTitle(card, a.source_id)))), "items", "пунктов")];
}

function paneSources(card) {
  const s = card.stats;
  return [
    el("div", "table-wrap", el("table", "",
      el("thead", "", el("tr", "", el("th", "", "Источник"), el("th", "", "Вид"), el("th", "", "Сведения"))),
      el("tbody", "", card.sources.map((m) => el("tr", "",
        el("th", "t-title", link(m.url, m.title)), el("td", "", SOURCE_KIND[m.kind] || m.kind), el("td", "", m.comment || "")))))),
    el("p", "small", `Карточка собрана ${day(card.generated_at)}. Модель: ${card.model || "не использовалась"}. ` +
      `Прочитано частей документации: ${s.parts_read} из ${s.parts_total}. Выводов модели ${s.findings_total}, подтверждено цитатой ${s.findings_kept}.`),
  ];
}

function renderCard(card) {
  const t = card.tender;
  const s = card.stats;
  const won = card.limitations.some((x) => x.includes("уже выиграла"));
  const high = card.requirements.filter((r) => r.importance === "high").length;
  return [
    el("header", "card-head",
      el("div", "card-tags", tag(`№ ${t.regn}`, "code"), tag(`${t.law}-ФЗ`), tag(t.status || "статус не указан"),
        won ? tag("компания выиграла эту закупку", "accent") : null),
      el("h3", "", t.title),
      el("dl", "facts",
        fact("Заказчик", t.customer_name),
        fact("Начальная цена", el("span", "num", money(t.initial_price))),
        fact("Приём заявок до", el("span", "num", day(t.deadline))),
        fact("Способ", t.placing_way || "не указан")),
      el("div", "card-actions",
        link(t.url, "Закупка на zakupki.gov.ru", "btn btn-ghost btn-sm"),
        el("span", "small", `Прочитано частей документации: ${s.parts_read} из ${s.parts_total}. ` +
          `Подтверждено цитатой: ${s.findings_kept} из ${s.findings_total} выводов модели.`))),
    verdict(card),
    ...tabs([
      { title: "Сводка", build: () => paneSummary(card) },
      { title: "Требования", count: `${high} / ${card.requirements.length}`, build: () => paneRequirements(card) },
      { title: "Заказчик", count: card.customer_history.relevant_contracts, build: () => paneCustomer(card) },
      { title: "Внимание", count: card.attention.length, build: () => paneAttention(card) },
      { title: "Источники", count: card.sources.length, build: () => paneSources(card) },
    ], "Разделы карточки"),
  ];
}

/** Строка в списке закупок слева. card появляется, когда файл карточки загружен. */
function fillPick(node, entry, card) {
  const won = card && card.limitations.some((x) => x.includes("уже выиграла"));
  const mini = el("span", "mini-scale");
  if (entry.range_low !== null && entry.range_low !== undefined) {
    const mark = el("span", `mini-range mini-${entry.chance_level}`);
    sized(mark, "--from", entry.range_low / 100);
    sized(mark, "--span", (entry.range_high - entry.range_low) / 100);
    mini.append(mark);
  }
  fill(node,
    el("span", "pick-row",
      el("span", "pick-level", el("span", "pick-k", "шанс"),
        el("strong", `level-${entry.chance_level}`, LEVEL[entry.chance_level] || entry.chance_level)),
      el("span", "pick-range", entry.range_low === null ? "" : `${entry.range_low}–${entry.range_high}`)),
    mini,
    el("span", "pick-title", entry.title),
    el("span", "pick-foot", el("span", "num", money(entry.initial_price)), el("span", "", `требований ${entry.requirements}`)),
    won ? el("span", "pick-tags", tag("выиграна компанией", "accent"))
      : (card ? el("span", "pick-tags", tag(card.tender.status || "статус не указан")) : null));
}

function renderCards(index) {
  const picker = document.getElementById("picker");
  const body = document.getElementById("card");
  if (!index.cards.length) {
    picker.replaceChildren();
    body.replaceChildren(el("p", "empty", LIVE ? "Карточек пока нет. Запустите анализ закупки в разделе «Управление» или из списка закупок ниже."
      : "Готовых карточек в пакете нет."));
    currentOpen = null;
    return;
  }
  const cards = new Map();
  const picks = index.cards.map((entry) => {
    const node = button("pick", () => { location.hash = `card=${entry.regn}`; });
    node.setAttribute("role", "tab");
    node.dataset.regn = entry.regn;
    fillPick(node, entry, null);
    return node;
  });
  picker.replaceChildren(el("p", "picker-title", `Закупок разобрано: ${index.cards.length}`), ...picks);

  // Карточки загружаются сразу все: из них список берёт исход и состояние закупки.
  const ready = Promise.all(index.cards.map((entry, i) => load(entry.file).then(
    (card) => { cards.set(entry.regn, card); fillPick(picks[i], entry, card); },
    (error) => { cards.set(entry.regn, error); })));

  async function open(regn, scroll) {
    const entry = index.cards.find((c) => c.regn === regn) || index.cards[0];
    picks.forEach((node) => node.setAttribute("aria-selected", String(node.dataset.regn === entry.regn)));
    if (!cards.has(entry.regn)) {
      body.replaceChildren(el("p", "empty", "Загружаю карточку…"));
      await ready;
    }
    const card = cards.get(entry.regn);
    body.replaceChildren(...(card instanceof Error
      ? [el("p", "alert", `Карточка не загрузилась: ${card.message}`)] : renderCard(card)));
    if (scroll) document.getElementById("cards").scrollIntoView();
  }
  const fromHash = () => (/^#card=(\d+)$/.exec(location.hash) || [])[1];
  currentOpen = open;
  open(fromHash(), Boolean(fromHash()));
}

// --- проверка на прошлых закупках ---

function renderCheck(backtest) {
  const r = backtest.report;
  const row = (title, sub, g) => el("div", "dist-row",
    el("div", "dist-name", el("b", "", title), el("span", "small", `${sub}: ${number(g.total)}`)),
    el("div", "dist-bar", ["high", "medium", "low"].map((key) => (g[key]
      ? sized(el("div", `seg seg-${key}`, number(g[key])), "--w", g[key] / g.total) : null))));
  const rowLabel = (title, g) => `${title}: высокий ${g.high}, средний ${g.medium}, низкий ${g.low}`;
  const dist = el("div", "panel panel-pad dist",
    el("p", "label", "Какую оценку получили закупки"),
    row("Выигранные компанией", "все", r.won),
    row("Выигранные у заказчиков из сравнения", "закупок", r.won_at_customers),
    row("Чужие у тех же заказчиков", "закупок", r.other),
    el("div", "legend", el("span", "", sized(el("i", "seg-high"), "--w", 0), "высокий"),
      el("span", "", sized(el("i", "seg-medium"), "--w", 0), "средний"),
      el("span", "", sized(el("i", "seg-low"), "--w", 0), "низкий")));
  dist.setAttribute("role", "img");
  dist.setAttribute("aria-label", [rowLabel("Выигранные компанией", r.won),
    rowLabel("Выигранные у заказчиков из сравнения", r.won_at_customers), rowLabel("Чужие", r.other)].join(". "));

  const factors = el("div", "table-wrap", el("table", "",
    el("caption", "", "Доля выигранных закупок при каждом значении фактора (у заказчиков из сравнения)"),
    el("thead", "", el("tr", "", el("th", "", "Фактор"), el("th", "", "Значение"), el("th", "r", "Выиграно"),
      el("th", "r", "Чужих"), el("th", "", "Доля выигранных"))),
    el("tbody", "", r.factors.map((f) => el("tr", "", el("th", "", f.factor),
      el("td", "", AREA[f.value] || LEVEL[f.value] || f.value), el("td", "r", number(f.won)), el("td", "r", number(f.other)),
      el("td", "", el("div", "meter", sized(el("i", ""), "--w", f.won_share || 0), el("span", "num", percent(f.won_share, 1)))))))));

  const sep = (v) => (v.value === null ? "нет данных" : `${percent(v.value)} (${percent(v.low)}–${percent(v.high)})`);
  const verdictTag = (key) => { const [text, kind] = VERDICT[key] || [key, ""]; return tag(text, kind); };
  const checks = el("div", "table-wrap", el("table", "",
    el("caption", "", "Различает ли каждый фактор выигранные и чужие закупки"),
    el("thead", "", el("tr", "", el("th", "", "Фактор"), el("th", "r", "Все закупки"), el("th", "", "Вердикт"),
      el("th", "r", "Среди похожих"), el("th", "", "Вердикт"))),
    el("tbody", "", r.factor_checks.map((c) => el("tr", "", el("th", "", c.factor),
      el("td", "r", sep(c.overall)), el("td", "", verdictTag(c.verdict_overall)),
      el("td", "r", sep(c.comparable)), el("td", "", verdictTag(c.verdict_comparable)))))));
  const p = r.profile;
  const comparableCount = r.separation_comparable;

  fill(document.getElementById("check-body"),
    el("div", "verdict-note",
      el("p", "label", "Главный вывод"),
      el("p", "", `Среди закупок, похожих на дело компании, оценка различает выигранные и чужие закупки в ${percent(comparableCount.value)} пар ` +
        `(интервал ${percent(comparableCount.low)}–${percent(comparableCount.high)}, ${number(comparableCount.positives)} побед и ${number(comparableCount.negatives)} чужих). ` +
        "50% значило бы «не различает». Сегодняшние правила на этих данных проверку не проходят, и это честный результат, а не ошибка отображения."),
      el("p", "small", "Число по всем закупкам выше, потому что чужие закупки заказчиков в основном вообще не из области компании: оно отвечает на вопрос «похоже ли это на дело компании», а не «выиграет ли она».")),
    el("div", "block-gap"),
    el("div", "stats",
      stat(sep(r.separation_comparable), "среди похожих закупок", `${number(comparableCount.positives)} побед и ${number(comparableCount.negatives)} чужих`),
      stat(sep(r.separation), "по всем закупкам", `${number(r.separation.positives)} побед и ${number(r.separation.negatives)} чужих`),
      stat(percent(r.won_rated_low_share), "побед с оценкой «низкий»", `${number(r.won.low)} из ${number(r.won.total)}: их правила посоветовали бы пропустить`),
      stat(number(r.won.total + r.other.total), "закупок оценено", `${day(r.period_from)} – ${day(r.period_to)}, заказчиков ${r.customers.length}`),
      stat(number(r.skipped_early + r.skipped_no_tender), "не оценено", `${number(r.skipped_early)}: мало истории до закупки; ${number(r.skipped_no_tender)}: контракт без извещения`)),
    el("div", "block-gap"), checks,
    el("p", "small check-foot", "«Наоборот» значит, что фактор в этой выборке указывает в сторону, обратную правилу. Это в первую очередь следствие состава выборки " +
      "(заказчики выбраны по числу побед компании, победы идут сериями у немногих заказчиков), а не доказательство, что фактор неверен: проверить и откалибровать его по таким данным нельзя."),
    el("div", "block-gap"), dist, el("div", "block-gap"), factors, el("div", "block-gap"),
    el("p", "small check-foot", `Профиль, по которому считалось: ${number(p.contracts)} контрактов, ${day(p.period_from)} – ${day(p.period_to)}, отпечаток ${p.fingerprint}. ` +
      `Правила: «${r.rules.name}». Когда в профиль добавят закрытые сведения о компании, проверку нужно запустить заново, а числа сравнивать с оговоркой о другом профиле.`),
    limits("Как читать эти числа", backtest.limitations));
}

// --- подборка ---

function renderShortlistEmpty() {
  document.getElementById("shortlist-sub").textContent = "Подборка появится после первого запуска.";
  fill(document.getElementById("shortlist-body"), localShortlistForm(null),
    el("div", "block-gap"), el("p", "panel empty", "Подборка ещё не строилась."));
}

function renderShortlist(list, index) {
  const analyzed = new Set(index.cards.map((c) => c.regn));
  document.getElementById("shortlist-sub").textContent =
    `Поиск по ${list.queries.length} словам из профиля компании, закупки, опубликованные с ${day(list.from_date)}: ` +
    `найдено ${number(list.found_total)}, приём заявок идёт по ${number(list.open_total)}. Показаны лучшие ${list.items.length}. ` +
    "Балл говорит, насколько закупка похожа на то, что компания уже делает. Это порядок просмотра, не прогноз победы.";

  const q = list.quality;
  const sep = q ? q.separation : null;
  const verdict = q ? (VERDICT[q.verdict] || [q.verdict, ""]) : null;
  const quality = q ? el("div", "verdict-note",
    el("p", "label", "Как проверен этот балл"),
    el("p", "", `Тот же балл посчитан для ${number(q.won + q.other)} закупок прошлого: ${number(q.won)} выиграла компания, ${number(q.other)} чужие у тех же заказчиков. ` +
      `Балл ставит выигранную закупку выше чужой в ${percent(sep.value)} пар (интервал ${percent(sep.low)}–${percent(sep.high)}; 50% значило бы «не различает»). ` +
      `Среди ${number(q.top_n)} закупок с самым высоким баллом выигранных компанией ${percent(q.top_won_share)}, в среднем по всем ${percent(q.base_won_share)}.`),
    el("p", "small", "Это проверка «похоже ли на дело компании», а не «выиграет ли она»: чужие закупки этих заказчиков в основном не из области компании, " +
      "и балл отличает их по названию. Победу он не предсказывает. Точную оценку даёт карточка закупки после разбора документации."),
    el("p", "small", verdict ? ["Вердикт проверки: ", tag(verdict[0], verdict[1])] : null)) : null;

  const words = el("div", "chips", list.queries.map((w) => tag(`${w.word}: ${number(w.found)}${w.more_pages ? "+" : ""}`)));

  const item = (p, rank) => {
    const c = p.candidate;
    const card = analyzed.has(c.regn);
    const node = el("li", "sl-item");
    const title = card ? el("a", "", c.title) : el("span", "", c.title);
    if (card) title.href = `#card=${c.regn}`;
    let more = null;
    if (c.title.length > 150) {
      more = button("sl-more", () => {
        const open = node.classList.toggle("is-open");
        more.setAttribute("aria-expanded", String(open));
        more.textContent = open ? "свернуть" : "название полностью";
      }, "название полностью");
      more.setAttribute("aria-expanded", "false");
    }
    const kind = p.score >= 55 ? "hi" : p.score >= 40 ? "mid" : "lo";
    const score = el("div", `sl-score sl-score-${kind}`, el("b", "", String(p.score)), el("span", "", "балл"), el("small", "", `№ ${rank}`));
    score.setAttribute("aria-label", `Место ${rank}, балл ${p.score} из 100`);
    fill(node,
      score,
      el("div", "sl-main",
        el("h3", "sl-title", title), more,
        el("p", "sl-customer", c.customer_name || "заказчик не указан"),
        el("div", "sl-meta",
          el("span", "sl-price", c.price ? money(c.price) : "цена не указана"),
          el("span", "sl-date", `заявки до ${day(c.deadline)}`), deadlineTag(c.deadline),
          tag(`${c.law}-ФЗ`), p.larger_than_usual ? tag("крупнее обычного", "warn") : null,
          card ? tag("есть карточка", "accent") : null),
        el("ul", "sl-why", p.reasons.map((r) => el("li", "", r))),
        el("p", "sl-foot", `№ ${c.regn} · `,
          link(`https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=${encodeURIComponent(c.regn)}`, "найти на zakupki.gov.ru"))),
      LIVE ? el("div", "sl-act", localRowAction(c.regn, card)) : null);
    return node;
  };

  // Фильтр по закону: у закупок по разным законам разные правила участия.
  const laws = [...new Set(list.items.map((p) => p.candidate.law))].sort((a, b) => Number(a) - Number(b));
  let law = "";
  const holder = el("ol", "sl-list");
  const count = el("span", "small", "");
  const chips = [["", "Все законы"], ...laws.map((l) => [l, `${l}-ФЗ`])].map(([value, label]) => {
    const b = button("chip", () => { law = value; draw(); }, label,
      el("span", "num", String(value ? list.items.filter((p) => p.candidate.law === value).length : list.items.length)));
    b.dataset.law = value;
    return b;
  });
  function draw() {
    chips.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.law === law)));
    const shown = list.items.map((p, i) => [p, i + 1]).filter(([p]) => !law || p.candidate.law === law);
    count.textContent = `Показано ${shown.length} из ${list.items.length}`;
    fill(holder, shown.map(([p, rank]) => item(p, rank)));
    if (holder.isConnected) hideNeedlessMore();
  }
  // Кнопка «название полностью» нужна, только если название действительно обрезано.
  function hideNeedlessMore() {
    for (const node of holder.querySelectorAll(".sl-item:not(.is-open)")) {
      const title = node.querySelector(".sl-title");
      const more = node.querySelector(".sl-more");
      if (more && title.scrollHeight <= title.clientHeight + 1) more.hidden = true;
    }
  }
  draw();

  fill(document.getElementById("shortlist-body"),
    LIVE ? localShortlistForm(list) : null, LIVE ? el("div", "block-gap") : null,
    laws.length > 1 ? el("div", "toolbar sl-tools", chips, count) : null,
    holder,
    el("div", "block-gap"), quality, quality ? el("div", "block-gap") : null,
    el("div", "sl-words", el("p", "label", "Как искали"),
      el("p", "small", `Слова из названий контрактов компании и сколько закупок вернул каждый поиск. Знак «+»: есть ещё страницы, прочитана первая.`),
      words),
    el("div", "block-gap"),
    limits("Ограничения подборки", list.limitations));
  hideNeedlessMore();
}

/** Сколько дней до окончания приёма заявок, считая от сегодняшнего дня у читателя. */
function daysLeft(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  if (!m) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) - today) / 86400000);
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b > 1 && b < 5) return few;
  return many;
}

function deadlineTag(value) {
  const n = daysLeft(value);
  if (n === null) return tag("срок не указан");
  if (n < 0) return tag("приём закончился", "bad");
  if (n === 0) return tag("последний день", "bad");
  const text = `осталось ${n} ${plural(n, "день", "дня", "дней")}`;
  return tag(text, n <= 3 ? "warn" : "");
}

// --- список закупок ---

function renderTendersEmpty() {
  document.getElementById("tenders-sub").textContent = "Список появится после поиска.";
  document.getElementById("tenders-body").replaceChildren(localSearchForm(null),
    el("div", "block-gap"), el("p", "panel empty", "Поиск ещё не выполнялся."));
}

function renderTenders(tenders, index) {
  const analyzed = new Set(index.cards.map((c) => c.regn));
  document.getElementById("tenders-sub").textContent =
    `Запрос «${tenders.query}», опубликовано с ${day(tenders.from_date)}: найдено ${tenders.total}, приём заявок на ${day(tenders.generated_at)} шёл по ${tenders.open_total}. ` +
    "Порядок задаёт сходство названия с контрактами компании. Это порядок просмотра, не прогноз победы.";
  const holder = el("div", "");
  const count = el("span", "small", "");
  const input = el("input", "search");
  input.type = "search";
  input.placeholder = "Название, заказчик или номер";
  input.setAttribute("aria-label", "Фильтр списка закупок");
  let onlyOpen = false;
  const toggle = button("chip", () => { onlyOpen = !onlyOpen; draw(); }, "Только с открытым приёмом заявок");

  function draw() {
    toggle.setAttribute("aria-pressed", String(onlyOpen));
    const q = input.value.trim().toLowerCase();
    const rows = tenders.items.filter((c) => (!onlyOpen || c.is_open) &&
      (!q || `${c.title} ${c.customer_name} ${c.regn}`.toLowerCase().includes(q)));
    count.textContent = `Подходит ${rows.length} из ${tenders.items.length}`;
    if (!rows.length) {
      holder.replaceChildren(el("p", "panel empty", "Под фильтр не подходит ни одна закупка."));
      return;
    }
    const tbody = el("tbody", "");
    const more = el("div", "more");
    let shown = 0;
    const step = () => {
      for (const c of rows.slice(shown, shown + PAGE)) {
        const title = analyzed.has(c.regn) ? el("a", "", c.title) : el("span", "", c.title);
        if (analyzed.has(c.regn)) title.href = `#card=${c.regn}`;
        tbody.append(el("tr", "",
          el("td", "t-title", title, el("small", "", `${c.customer_name || "заказчик не указан"} · № ${c.regn}`)),
          el("td", "r", money(c.price)),
          el("td", "r", day(c.deadline)),
          el("td", "", tag(c.is_open ? "идёт приём" : "закончен", c.is_open ? "good" : ""), " ", tag(`${c.law}-ФЗ`),
            analyzed.has(c.regn) ? [" ", tag("есть карточка", "accent")] : null),
          el("td", "", el("div", "meter", sized(el("i", ""), "--w", c.text_match || 0), el("span", "num", percent(c.text_match)))),
          LIVE ? el("td", "r", localRowAction(c.regn, analyzed.has(c.regn))) : null));
      }
      shown = Math.min(rows.length, shown + PAGE);
      more.replaceChildren();
      if (shown < rows.length) {
        more.append(el("span", "", `Показано ${shown} из ${rows.length} закупок`),
          button("btn btn-line", step, `Показать ещё ${Math.min(PAGE, rows.length - shown)}`));
      } else if (rows.length > PAGE) more.append(el("span", "", `Показаны все ${rows.length} закупок`));
    };
    step();
    holder.replaceChildren(el("div", "table-wrap", el("table", "",
      el("thead", "", el("tr", "", el("th", "", "Закупка"), el("th", "r", "Начальная цена"), el("th", "r", "Заявки до"),
        el("th", "", "Состояние"), el("th", "", "Сходство с профилем"), LIVE ? el("th", "r", "Действие") : null)), tbody)), more);
  }
  input.addEventListener("input", draw);
  draw();
  fill(document.getElementById("tenders-body"),
    LIVE ? localSearchForm(tenders) : null, LIVE ? el("div", "block-gap") : null,
    el("div", "toolbar", input, toggle, count), holder,
    tenders.limitations.length ? el("div", "block-gap") : null,
    tenders.limitations.length ? limits("Ограничения списка", tenders.limitations) : null);
}

// --- профиль ---

function renderProfile(profile) {
  const p = profile.profile;
  const price = p.contract_price || {};
  const r = p.reductions || {};
  const top = p.okpd_codes[0] ? p.okpd_codes[0].contracts : 1;
  const topTerm = p.title_terms[0] ? p.title_terms[0].contracts : 1;
  const bars = (items, key, max) => el("div", "hbars", items.map((item) => el("div", "hbar",
    el("span", key === "code" ? "code" : "", item[key]), sized(el("i", ""), "--w", item.contracts / max),
    el("span", "num small", item.contracts))));
  document.getElementById("profile-body").replaceChildren(
    el("div", "stats",
      stat(number(p.contracts), "контрактов", `${day(p.period_from)} – ${day(p.period_to)}`),
      stat(number(p.customers_total), "заказчиков", "по ИНН"),
      stat(money(price.median), "медиана цены контракта", `от ${money(price.min)} до ${money(price.max)}`),
      stat(percent(r.no_reduction_share), "закупок без снижения цены", `по ${number(r.observations)} закупкам`),
      stat(percent(r.deep_share), "со снижением больше чем вдвое", "торг до символической цены")),
    el("div", "block-gap"),
    el("div", "cols",
      el("div", "panel panel-pad", el("p", "label", `Коды ОКПД2: ${p.okpd_codes.length} самых частых из ${p.okpd_codes_total}`),
        el("div", "block-gap-s"), bars(p.okpd_codes, "code", top)),
      el("div", "panel panel-pad", el("p", "label", "Частые слова в названиях контрактов"),
        el("div", "block-gap-s"), bars(p.title_terms.slice(0, 15), "stem", topTerm))),
    el("div", "block-gap"),
    limits("Чего в профиле нет", p.limitations));
}

async function main() {
  try {
    if (!booted && typeof detectLocal === "function") LIVE = await detectLocal();
    const index = await load("index.json");
    const need = (name) => (index.files.includes(name) ? load(name) : null);
    const [profile, tenders, backtest, shortlist] = await Promise.all([
      need("profile.json"), need("tenders.json"), need("backtest.json"), need("shortlist.json")]);
    const missing = (id, text) => document.getElementById(id).replaceChildren(el("p", "panel empty", text));
    const none = (what) => (LIVE ? `${what} ещё нет: выполните действие в разделе «Управление».` : `${what} в пакете нет.`);
    renderIntro(index, profile, backtest);
    renderCards(index);
    if (shortlist) renderShortlist(shortlist, index);
    else if (LIVE) renderShortlistEmpty();
    else missing("shortlist-body", none("Подборки"));
    if (backtest) renderCheck(backtest); else missing("check-body", none("Проверки на прошлых закупках"));
    if (tenders) renderTenders(tenders, index);
    else if (LIVE) renderTendersEmpty();
    else missing("tenders-body", none("Списка закупок"));
    if (profile) renderProfile(profile); else missing("profile-body", none("Профиля"));
    if (LIVE) renderLocal(index, { profile, tenders, backtest, shortlist });
    reveal();
    if (LIVE && !booted) resumeJob();
    booted = true;
  } catch (error) {
    const box = document.getElementById("load-error");
    box.textContent = `Данные не загрузились: ${error.message}. ` +
      (LIVE ? "Проверьте, что сервер запущен." : "Страницу нужно открывать с сервера (GitHub Pages или локальный сервер), а не как файл с диска.");
    box.hidden = false;
  }
}

// Скрипты подключены с defer: к этому событию загружены и app.js, и local.js.
addEventListener("hashchange", () => {
  const regn = (/^#card=(\d+)$/.exec(location.hash) || [])[1];
  if (regn && currentOpen) currentOpen(regn, true);
});
document.addEventListener("DOMContentLoaded", () => { chrome(); main(); });
