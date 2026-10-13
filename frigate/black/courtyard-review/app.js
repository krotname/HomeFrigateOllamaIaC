"use strict";

const CAMERAS = ["common_camera01", "common_camera02", "common_camera03"];
const CAMERA_NAMES = {
  common_camera01: "Камера 01 · навес",
  common_camera02: "Камера 02 · двор",
  common_camera03: "Камера 03 · двор сверху",
};
const MAX_GAP_SECONDS = 15;
const MAX_GROUP_SECONDS = 180;

function groupReviewItems(items) {
  const allowed = new Set(CAMERAS);
  const sorted = items.filter((item) =>
    allowed.has(item.camera) && Number.isFinite(item.start_time) &&
    ["alert", "detection"].includes(item.severity)
  ).sort((a, b) => a.start_time - b.start_time);
  const groups = [];

  for (const item of sorted) {
    const itemLabels = new Set(item.data?.objects ?? []);
    const itemEnd = Number.isFinite(item.end_time) ? item.end_time : item.start_time;
    const group = [...groups].reverse().find((candidate) =>
      candidate.severity === item.severity &&
      !candidate.items.some((previous) => previous.camera === item.camera) &&
      item.start_time <= candidate.end + MAX_GAP_SECONDS &&
      item.start_time - candidate.start <= MAX_GROUP_SECONDS &&
      [...itemLabels].some((label) => candidate.labels.has(label))
    );
    if (group) {
      group.items.push(item);
      group.end = Math.max(group.end, itemEnd);
      for (const label of itemLabels) group.labels.add(label);
    } else {
      groups.push({ start: item.start_time, end: itemEnd, severity: item.severity,
        labels: itemLabels, items: [item] });
    }
  }
  return groups.sort((a, b) => b.start - a.start);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { groupReviewItems };
}

if (typeof document !== "undefined") {
  const list = document.getElementById("incident-list");
  const status = document.getElementById("status");
  const refreshButton = document.getElementById("refresh");
  let allGroups = [];
  let activeFilter = "all";
  let loading = false;

  const time = (seconds, options = {}) => new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit", minute: "2-digit", ...options,
  }).format(new Date(seconds * 1000));

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  function thumbUrl(path) {
    if (typeof path !== "string") return null;
    const publicPath = path.replace(/^\/media\/frigate/, "");
    return /^\/clips\/review\/thumb-[a-zA-Z0-9_.-]+\.webp$/.test(publicPath) ? publicPath : null;
  }

  function renderGroups() {
    list.replaceChildren();
    const visible = allGroups.filter((group) => activeFilter === "all" || group.severity === activeFilter);
    if (!visible.length) {
      status.textContent = "За выбранный период событий нет.";
      return;
    }
    status.textContent = `Показано ${visible.length} временных групп. Совпадение по времени не доказывает, что на разных камерах один объект.`;
    for (const group of visible) {
      const cameras = new Set(group.items.map((item) => item.camera));
      const card = element("article", "incident" + (group.items.every((item) => item.has_been_reviewed) ? " reviewed" : ""));
      const head = element("div", "incident-header");
      const heading = element("div");
      const date = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(group.start * 1000));
      heading.append(element("h3", "", `${date}, ${time(group.start)}–${time(group.end)}`));
      const meta = element("div", "incident-meta");
      meta.append(element("span", `pill ${group.severity}`, group.severity === "alert" ? "Тревога" : "Обнаружение"));
      meta.append(element("span", "", `${cameras.size} ${cameras.size === 1 ? "камера" : "камеры"} · ${group.items.length} записей`));
      if (cameras.size > 1) meta.append(element("span", "pill", "Совпали по времени"));
      if (card.classList.contains("reviewed")) meta.append(element("span", "", "Просмотрено"));
      heading.append(meta);
      head.append(heading);
      card.append(head);
      const views = element("div", "views");
      for (const item of [...group.items].sort((a, b) => CAMERAS.indexOf(a.camera) - CAMERAS.indexOf(b.camera))) {
        const view = element("div", "view");
        const url = thumbUrl(item.thumb_path);
        if (url) {
          const img = element("img");
          img.src = url;
          img.alt = `${CAMERA_NAMES[item.camera]}, ${time(item.start_time)}`;
          img.loading = "lazy";
          view.append(img);
        } else {
          view.append(element("div", "thumb-placeholder", "Кадр недоступен"));
        }
        const info = element("div", "view-info");
        info.append(element("span", "", `${CAMERA_NAMES[item.camera]} · ${time(item.start_time)}`));
        const clip = element("a", "", "Клип ↗");
        clip.href = `/api/review/${encodeURIComponent(item.id)}/clip.mp4`;
        clip.target = "_blank";
        clip.rel = "noopener";
        info.append(clip);
        view.append(info);
        views.append(view);
      }
      card.append(views);
      list.append(card);
    }
  }

  function renderMotion(points) {
    const chart = document.getElementById("motion-chart");
    chart.replaceChildren();
    if (!points.length) {
      chart.textContent = "Нет данных движения за последний час.";
      return;
    }
    const max = Math.max(...points.map((point) => Number(point.motion) || 0), 1);
    for (const point of points) {
      const bar = element("div", "motion-bar");
      bar.style.height = `${Math.max(2, Math.round(((Number(point.motion) || 0) / max) * 100))}%`;
      bar.title = `${time(point.start_time)} · движение ${Math.round(Number(point.motion) || 0)} · ${point.camera || "нет камер"}`;
      chart.append(bar);
    }
    document.getElementById("motion-from").textContent = time(points[0].start_time);
    document.getElementById("motion-to").textContent = time(points.at(-1).start_time + 30);
  }

  async function getJson(path) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    refreshButton.disabled = true;
    const since = Math.floor(Date.now() / 1000) - 86400;
    const query = new URLSearchParams({ cameras: CAMERAS.join(","), after: String(since), limit: "500" });
    try {
      const [reviews, motion] = await Promise.all([
        getJson(`/api/review?${query}`),
        getJson(`/api/review/activity/motion?cameras=${CAMERAS.join(",")}`),
      ]);
      if (!Array.isArray(reviews) || !Array.isArray(motion)) throw new Error("Неожиданный ответ API");
      allGroups = groupReviewItems(reviews);
      document.getElementById("group-count").textContent = String(allGroups.length);
      document.getElementById("source-count").textContent = String(allGroups.reduce((n, group) => n + group.items.length, 0));
      document.getElementById("updated-at").textContent = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date());
      renderMotion(motion);
      renderGroups();
      if (reviews.length === 500) status.textContent += " Достигнут предел 500 записей; ранние события суток могут отсутствовать.";
      status.classList.remove("error");
    } catch (error) {
      status.textContent = `Не удалось получить события Frigate: ${error.message}`;
      status.classList.add("error");
    } finally {
      loading = false;
      refreshButton.disabled = false;
    }
  }

  document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((other) => {
      other.classList.toggle("selected", other === button);
      other.setAttribute("aria-pressed", String(other === button));
    });
    renderGroups();
  }));
  refreshButton.addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  refresh();
}
