// Локальный режим страницы. Если страница открыта на GitHub Pages или не с этого компьютера,
// detectLocal() возвращает false, и этот файл ничего на странице не меняет.
// Здесь только действия: построить профиль, найти закупки, запустить анализ, следить за задачей.
// Показ данных остаётся в app.js. Всё, что тратит квоту, скачивает файлы или обращается к модели,
// сначала называет цену и ждёт подтверждения кнопкой.
"use strict";

const LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"];
const JOB_POLL_MS = 2000;
const COST = {
  profile: "Построить профиль: до 3 запросов к DaMIA (метод contracts). Страницы, которые уже сохранены на диске, квоту не тратят.",
  search: "Поиск закупок: 1 запрос к DaMIA (метод zsearch). Такой же поиск, уже выполненный раньше, берётся с диска бесплатно.",
  analyze: "Анализ закупки: до 7 запросов к DaMIA (zakupka 1, contracts 1, contract до 5), скачивание до 6 файлов с zakupki.gov.ru " +
    "и чтение документации моделью: для крупной закупки около 10 минут и 60–80 тысяч токенов. " +
    "Если закупка уже разобрана, повтор берёт всё с диска и занимает около минуты.",
};

let HEALTH = null;
let jobTimer = null;
let watchedJob = null;
let jobNote = ""; // пояснение рядом с ходом задачи, например «уже идёт другой анализ»

async function detectLocal() {
  if (!LOCAL_HOSTS.includes(location.hostname)) return false;
  try {
    const response = await fetch("api/health", { cache: "no-store" });
    if (!response.ok) return false;
    const body = await response.json();
    if (body.status !== "ok") return false;
    HEALTH = body;
    return true;
  } catch (error) {
    return false;
  }
}

async function api(path, options = {}) {
  const response = await fetch(`api/${path}`, {
    cache: "no-store", ...options, headers: { "Content-Type": "application/json" } });
  let body = null;
  try { body = await response.json(); } catch (error) { body = null; }
  if (!response.ok) {
    const detail = body && body.detail;
    const text = typeof detail === "string" ? detail
      : (Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `ответ сервера ${response.status}`);
    throw new Error(text);
  }
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data) });

// --- подтверждение и сообщения ---

/** Вместо кнопки показывает цену действия и две кнопки. Запуск только по «Подтвердить». */
function confirmIn(host, message, label, run) {
  const before = [...host.childNodes];
  const restore = () => host.replaceChildren(...before);
  const yes = button("btn btn-accent btn-sm", () => { restore(); run(); }, label);
  host.replaceChildren(el("div", "confirm",
    el("p", "confirm-text", message),
    el("div", "confirm-actions", yes, button("btn btn-line btn-sm", restore, "Отмена"))));
  yes.focus();
}

function say(host, text, kind) {
  host.replaceChildren(el("p", kind === "bad" ? "alert" : "notice", text));
}

// --- ход задачи ---

function elapsed(startedAt) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(startedAt).getTime()) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function showJob(job, extra) {
  const box = document.getElementById("job-status");
  if (!box) return;
  const running = job.status === "running";
  const share = job.total ? Math.min(1, job.done / job.total) : 0;
  box.replaceChildren(el("div", `job job-${job.status}`,
    el("div", "job-head",
      el("strong", "", running ? `Идёт анализ закупки № ${job.regn}` : (job.status === "done" ? `Анализ закупки № ${job.regn} завершён` : `Анализ закупки № ${job.regn} не удался`)),
      el("span", "small num", `прошло ${elapsed(job.started_at)}`)),
    running ? el("div", "job-bar", sized(el("i", ""), "--w", share)) : null,
    el("p", "small", running ? `${job.step}${job.total > 1 ? `: ${job.done} из ${job.total}` : ""}. ` +
      "Страницу можно не закрывать, ход обновляется сам. Если страницу закрыть, анализ продолжится на сервере." : ""),
    job.error ? el("p", "alert", job.error) : null,
    jobNote ? el("p", "notice", jobNote) : null,
    extra || null));
}

function stopWatching() {
  if (jobTimer) clearTimeout(jobTimer);
  jobTimer = null;
}

async function watchJob(id) {
  stopWatching();
  watchedJob = id;
  let failures = 0;
  const tick = async () => {
    if (watchedJob !== id) return;
    let job;
    try {
      job = await api(`jobs/${id}`);
      failures = 0;
    } catch (error) {
      failures += 1;
      if (failures >= 3) {
        say(document.getElementById("job-status"), `Связь с сервером потеряна: ${error.message}`, "bad");
        return;
      }
      jobTimer = setTimeout(tick, JOB_POLL_MS);
      return;
    }
    if (job.status === "running") {
      showJob(job);
      jobTimer = setTimeout(tick, JOB_POLL_MS);
      return;
    }
    if (job.status !== "running") jobNote = "";
    if (job.status === "done") {
      showJob(job, el("p", "small", "Карточка сохранена. Открываю её."));
      await main();
      await refreshQuota();
      if (location.hash === `#card=${job.regn}`) location.hash = "";
      location.hash = `card=${job.regn}`;
    } else {
      showJob(job);
      await refreshQuota();
    }
  };
  await tick();
}

async function startAnalysis(regn) {
  const status = document.getElementById("job-status");
  try {
    const job = await post(`tenders/${regn}/analyze`, { network: true, download: true, history_lookups: 5 });
    jobNote = "";
    showJob(job);
    watchJob(job.id);
  } catch (error) {
    say(status, error.message, "bad");
    // Уже идёт другая задача: покажем её ход вместе с пояснением.
    try {
      const running = (await api("jobs")).find((j) => j.status === "running");
      if (running) { jobNote = error.message; showJob(running); watchJob(running.id); }
    } catch (ignored) { /* сообщение об ошибке уже показано */ }
  }
  document.getElementById("job-box").scrollIntoView({ block: "center" });
}

/** Кнопка в строке списка закупок. Подтверждение показывается в блоке «Управление». */
function localRowAction(regn, hasCard) {
  return button("btn btn-line btn-sm", () => {
    const host = document.getElementById("job-confirm");
    confirmIn(host, `${COST.analyze} Закупка № ${regn}.`, "Подтвердить и запустить", () => startAnalysis(regn));
    document.getElementById("job-box").scrollIntoView({ block: "center" });
  }, hasCard ? "Обновить" : "Анализировать");
}

// --- панель «Управление» ---

async function refreshQuota() {
  const holder = document.getElementById("quota-box");
  if (!holder) return;
  try {
    const q = await api("quota");
    holder.replaceChildren(el("div", "table-wrap", el("table", "",
      el("caption", "", `Квота DaMIA на год: ${q.yearly_limit} запросов на метод, потолок приложения ${q.cap}`),
      el("thead", "", el("tr", "", el("th", "", "Метод"), el("th", "", "Использовано"), el("th", "r", "Осталось"))),
      el("tbody", "", q.methods.map((m) => el("tr", "", el("th", "", m.method),
        el("td", "", el("div", "meter", sized(el("i", ""), "--w", m.cap ? m.used / m.cap : 0),
          el("span", "num", `${m.used} / ${m.cap}`))),
        el("td", "r", number(m.left))))))));
  } catch (error) {
    say(holder, `Квота не прочитана: ${error.message}`, "bad");
  }
}

function profileCard() {
  const host = el("div", "action-host");
  const run = async () => {
    say(host, "Строю профиль…");
    try {
      const built = await post("profile/build", { network: true, max_pages: 3 });
      await main(); // панель перерисована: сообщение показываем в блоке хода, он переживает перерисовку
      await refreshQuota();
      jobNote = "";
      say(document.getElementById("job-status"),
        `Профиль построен: ${number(built.profile.contracts)} контрактов, ${day(built.profile.period_from)} – ${day(built.profile.period_to)}.`);
    } catch (error) {
      host.replaceChildren(...profileButtons(host));
      host.prepend(el("p", "alert", error.message));
    }
  };
  const profileButtons = (container) => [button("btn btn-ink btn-sm", () =>
    confirmIn(container, COST.profile, "Подтвердить и построить", run), "Построить профиль")];
  host.append(...profileButtons(host));
  return el("div", "panel panel-pad action",
    el("p", "label", "Профиль компании"),
    el("p", "", "Контракты компании из открытого реестра: коды ОКПД2, заказчики, размер, снижение цены. Профиль нужен для анализа любой закупки."),
    host);
}

function regnCard() {
  const input = el("input", "search");
  input.inputMode = "numeric";
  input.placeholder = "Номер закупки, например 0152200004726000906";
  input.setAttribute("aria-label", "Номер закупки");
  input.maxLength = 25;
  const host = el("div", "action-host");
  const form = el("div", "form-row", input, button("btn btn-ink btn-sm", () => {
    const regn = input.value.trim();
    if (!/^\d{8,25}$/.test(regn)) { say(host, "Номер закупки состоит из 8–25 цифр.", "bad"); input.focus(); return; }
    confirmIn(document.getElementById("job-confirm"), `${COST.analyze} Закупка № ${regn}.`,
      "Подтвердить и запустить", () => startAnalysis(regn));
    document.getElementById("job-box").scrollIntoView({ block: "center" });
  }, "Анализировать"));
  return el("div", "panel panel-pad action",
    el("p", "label", "Анализ закупки по номеру"),
    el("p", "", "Для закупки, которой нет в списке ниже. Номер можно скопировать со страницы закупки на zakupki.gov.ru."),
    form, host);
}

/** Блок подтверждения и хода задачи создаётся один раз и переживает перерисовку данных. */
function jobBox() {
  let box = document.getElementById("job-box");
  if (!box) {
    box = el("div", "job-box");
    box.id = "job-box";
    const confirm = el("div", "");
    confirm.id = "job-confirm";
    const status = el("div", "");
    status.id = "job-status";
    box.append(confirm, status);
  }
  return box;
}

function renderLocal(index, data) {
  document.getElementById("nav-local").hidden = false;
  document.getElementById("local").hidden = false;
  const kind = (ok) => (ok ? "good" : "bad");
  const quota = el("div", "");
  quota.id = "quota-box";
  fill(document.getElementById("local-body"),
    el("div", "chips status-row",
      tag(`Ключ DaMIA: ${HEALTH.damia_key_set ? "задан" : "не задан"}`, kind(HEALTH.damia_key_set)),
      tag(`Модель: ${HEALTH.model_set ? "задана" : "не задана"}`, kind(HEALTH.model_set)),
      tag(`ИНН компании ${HEALTH.company_inn}`),
      tag(`версия ${HEALTH.version}`)),
    !HEALTH.model_set ? el("p", "notice", "Языковая модель не настроена в .env: карточки будут собираться без чтения документации. " +
      "Нужны LLM_BASE_URL, LLM_API_KEY и LLM_MODEL.") : null,
    !HEALTH.damia_key_set ? el("p", "notice", "Ключ DaMIA не задан в .env: работают только ответы, уже сохранённые на диске.") : null,
    !data.profile ? el("p", "notice", "Профиль компании ещё не построен. Начните с него: без профиля анализ закупки не запускается.") : null,
    el("div", "cols", profileCard(), regnCard()),
    jobBox(), quota);
  refreshQuota();
}

/** Форма поиска над списком закупок. current: прежний результат поиска или null. */
function localSearchForm(current) {
  const query = el("input", "search");
  query.value = current ? current.query : "услуги связи";
  query.maxLength = 200;
  query.setAttribute("aria-label", "Запрос");
  const from = el("input", "search");
  from.type = "date";
  const weekAgo = new Date(Date.now() - 14 * 86400000);
  from.value = current ? current.from_date : weekAgo.toISOString().slice(0, 10);
  from.setAttribute("aria-label", "Опубликовано с");
  const host = el("div", "action-host");
  const go = () => {
    const text = query.value.trim();
    if (text.length < 2) { say(host, "Запрос короче двух знаков.", "bad"); query.focus(); return; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from.value)) { say(host, "Укажите дату начала поиска.", "bad"); return; }
    confirmIn(host, COST.search, "Подтвердить и искать", async () => {
      say(host, "Ищу закупки…");
      try {
        await post("tenders/search", { query: text, from_date: from.value, network: true });
        await main();
        await refreshQuota();
      } catch (error) {
        say(host, error.message, "bad");
      }
    });
  };
  return el("div", "panel panel-pad search-form",
    el("p", "label", "Новый поиск"),
    el("div", "form-row",
      el("label", "field", el("span", "small", "Запрос"), query),
      el("label", "field", el("span", "small", "Опубликовано с"), from),
      button("btn btn-ink btn-sm", go, "Найти закупки")),
    host);
}

/** Если после перезагрузки страницы на сервере идёт анализ, страница снова показывает его ход. */
async function resumeJob() {
  try {
    const running = (await api("jobs")).find((j) => j.status === "running");
    if (running && watchedJob !== running.id) { showJob(running); watchJob(running.id); }
  } catch (error) { /* список задач недоступен: ход не показываем, анализ идёт сам */ }
}
