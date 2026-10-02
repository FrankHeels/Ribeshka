(() => {
  const cart = document.querySelector("[data-cart]");
  if (!cart) return;
  const status = cart.querySelector("[data-cart-status]");
  const makeLine = (row) => ({
    row, input: row.querySelector(".cart-weight"), form: row.querySelector("form"),
    total: row.querySelector("[data-cart-line-total]").innerHTML,
    revision: 0, dirty: false, removing: false, queued: false, error: "", timer: null,
  });
  const lines = new Map([...cart.querySelectorAll("[data-cart-line]")].map((row) => [row.dataset.cartLine, makeLine(row)]));
  const queue = [];
  let busy = false;

  function clearWeightError(line) {
    line.input.removeAttribute("aria-invalid");
    line.input.removeAttribute("aria-describedby");
    line.row.querySelector("[data-cart-weight-error]").hidden = true;
  }

  function showStatus() {
    const states = [...lines.values()];
    const pending = states.some((line) => line.removing || (line.dirty && !line.error));
    const invalid = states.some((line) => line.input.hasAttribute("aria-invalid"));
    const error = states.find((line) => line.error)?.error;
    const total = cart.querySelector("[data-cart-total]");
    if (total) total.style.display = states.some((line) => line.dirty || line.removing) || invalid ? "none" : "";
    status.textContent = busy || pending ? "Пересчитываем сумму…" : error || (invalid ? "Проверьте вес выделенных позиций." : "Сумма пересчитана.");
    status.hidden = false;
    states.forEach((line) => {
      line.row.querySelector("[data-cart-line-total]").innerHTML = line.dirty || line.removing || line.input.hasAttribute("aria-invalid")
        ? '<span aria-label="Сумма недоступна">—</span>' : line.total;
    });
  }

  function reconcile(page, submitted, revision) {
    const next = page.querySelector("[data-cart]");
    if (!next) throw new Error("Unexpected cart response");
    const nextLines = new Map([...next.querySelectorAll("[data-cart-line]")].map((row) => [row.dataset.cartLine, row]));
    const stale = submitted.revision !== revision;
    if (!stale && !submitted.removing) {
      submitted.dirty = false;
      submitted.error = "";
    }
    for (const [key, line] of lines) {
      const row = nextLines.get(key);
      if (!row) {
        window.clearTimeout(line.timer);
        line.row.remove();
        lines.delete(key);
        continue;
      }
      line.row.querySelector("[data-cart-price]").innerHTML = row.querySelector("[data-cart-price]").innerHTML;
      const input = row.querySelector(".cart-weight");
      line.total = row.querySelector("[data-cart-line-total]").innerHTML;
      line.input.min = input.min;
      line.input.step = input.step;
      // Another line's response contains stored weights, not this field's rejected input.
      if (line.dirty || line.removing || (line !== submitted && line.input.hasAttribute("aria-invalid"))) continue;
      if (line !== submitted) line.input.value = input.value;
      const error = row.querySelector("[data-cart-weight-error]");
      const target = line.row.querySelector("[data-cart-weight-error]");
      target.textContent = error.textContent;
      target.hidden = error.hidden;
      if (input.hasAttribute("aria-invalid")) {
        line.input.setAttribute("aria-invalid", "true");
        line.input.setAttribute("aria-describedby", target.id);
      } else {
        clearWeightError(line);
      }
    }
    if (!lines.size) {
      cart.querySelector("[data-cart-body]").innerHTML = next.querySelector("[data-cart-body]").innerHTML;
    } else {
      cart.querySelector(".cart-summary").innerHTML = next.querySelector(".cart-summary").innerHTML;
      for (const [key, row] of nextLines) {
        if (!lines.has(key)) cart.querySelector("tbody").insertAdjacentHTML("beforeend", row.outerHTML);
      }
    }
    for (const row of cart.querySelectorAll("[data-cart-line]")) {
      if (!lines.has(row.dataset.cartLine)) lines.set(row.dataset.cartLine, makeLine(row));
    }
    if (stale) next.querySelector("[data-cart-form-error]")?.remove();
    cart.querySelector("[data-cart-notices]").innerHTML = next.querySelector("[data-cart-notices]").innerHTML;
    const messages = document.querySelector(".message-list");
    const nextMessages = page.querySelector(".message-list");
    if (messages) {
      if (nextMessages) messages.replaceWith(nextMessages);
      else messages.remove();
    } else if (nextMessages) {
      cart.before(nextMessages);
    }
    const header = document.querySelector(".header-cart");
    const nextHeader = page.querySelector(".header-cart");
    header.innerHTML = nextHeader.innerHTML;
    header.setAttribute("aria-label", nextHeader.getAttribute("aria-label"));
  }

  async function processQueue() {
    if (busy) return;
    busy = true;
    while (queue.length) {
      const line = queue.shift();
      line.queued = false;
      if (!line.row.isConnected) continue;
      const revision = line.revision;
      const removing = line.removing;
      // The name="action" buttons shadow the form.action property.
      const url = line.form.getAttribute("action");
      const data = new FormData(line.form);
      data.set("action", removing ? "remove" : "update");
      if (!removing) data.set("weight", line.input.value);
      try {
        let response = await fetch(url, {
          method: "POST", body: data, mode: "same-origin", credentials: "same-origin",
        });
        if (response.status === 404) {
          response = await fetch(url, { mode: "same-origin", credentials: "same-origin" });
        }
        if (response.status !== 200 && response.status !== 400) throw new Error("Cart request failed");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        reconcile(page, line, revision);
      } catch {
        if (line.revision === revision && line.removing === removing) {
          line.error = removing
            ? "Не удалось удалить позицию. Проверьте соединение и повторите удаление."
            : "Не удалось сохранить изменения. Проверьте соединение и повторите ввод веса или нажмите Enter.";
          line.removing = false;
          line.input.disabled = false;
          line.row.querySelector('[value="remove"]').disabled = false;
        }
      }
      showStatus();
    }
    busy = false;
    showStatus();
  }

  function enqueue(line) {
    window.clearTimeout(line.timer);
    if (!line.queued) {
      line.queued = true;
      queue.push(line);
    }
    void processQueue();
  }

  cart.addEventListener("input", (event) => {
    if (!event.target.matches(".cart-weight")) return;
    const line = lines.get(event.target.closest("[data-cart-line]").dataset.cartLine);
    line.revision += 1;
    line.dirty = true;
    line.error = "";
    clearWeightError(line);
    showStatus();
    window.clearTimeout(line.timer);
    line.timer = window.setTimeout(() => enqueue(line), 350);
  });
  cart.addEventListener("change", (event) => {
    if (!event.target.matches(".cart-weight")) return;
    const line = lines.get(event.target.closest("[data-cart-line]").dataset.cartLine);
    if (line.dirty) enqueue(line);
  });
  cart.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches(".cart-weight")) return;
    event.preventDefault();
    const line = lines.get(event.target.closest("[data-cart-line]").dataset.cartLine);
    line.error = "";
    enqueue(line);
  });
  cart.addEventListener("submit", (event) => {
    const line = [...lines.values()].find((entry) => entry.form === event.target);
    if (!line) return;
    event.preventDefault();
    line.error = "";
    if (event.submitter?.value === "remove") {
      line.removing = true;
      line.input.disabled = true;
      event.submitter.disabled = true;
    }
    enqueue(line);
    showStatus();
  });
})();
