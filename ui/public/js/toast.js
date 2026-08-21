/* Toast notifications + activity log. */
import { $, el } from "./dom.js";

export function toast(title, body = "", kind = "info", ttl = 4200) {
  const node = el("div", { class: `toast ${kind}` },
    el("div", { class: "t-title" }, title),
    body ? el("div", { class: "t-body" }, body) : null
  );
  $("toasts").append(node);
  setTimeout(() => {
    node.classList.add("out");
    node.addEventListener("animationend", () => node.remove());
  }, ttl);
}

export function logLine(msg, obj) {
  const t = new Date().toLocaleTimeString();
  let line = `[${t}] ${msg}`;
  if (obj !== undefined) line += "\n" + (typeof obj === "string" ? obj : JSON.stringify(obj, null, 2));
  const log = $("log");
  log.textContent = line + "\n\n" + log.textContent;
}
