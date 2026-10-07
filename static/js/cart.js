(() => {
  const cart = document.querySelector("[data-cart]");
  if (!cart) return;

  const makeLine = (row) => ({
    row,
    input: row.querySelector(".cart-weight"),
    form: row.querySelector("form"),
    revision: 0,
    dirty: false,
    removing: false,
    queued: false,
    error: "",
    removeError: "",
  });
  const lines = new Map([...cart.querySelectorAll("[data-cart-line]")].map((row) => [row.dataset.cartLine, makeLine(row)]));
  const queue = [];
  let busy = false;
  let hasReconciled = false;

  function setFieldError(line, message, invalid = false) {
    const target = line.row.querySelector("[data-cart-weight-error]");
    target.textContent = message;
    target.hidden = !message;
    if (message) {
      line.input.setAttribute("aria-describedby", target.id);
      if (invalid) line.input.setAttribute("aria-invalid", "true");
      else line.input.removeAttribute("aria-invalid");
    } else {
      line.input.removeAttribute("aria-invalid");
      line.input.removeAttribute("aria-describedby");
    }
  }

  function updateStepperState(line) {
    const min = Number(line.input.min) || 1;
    const max = Number(line.input.max) || 99999;
    const value = line.input.value === "" ? Number.NaN : Number(line.input.value);
    const valid = Number.isInteger(value) && value >= min && value <= max;
    line.row.querySelectorAll("[data-cart-step]").forEach((button) => {
      const step = Number(button.dataset.cartStep);
      button.disabled = line.input.disabled
        || (valid && step < 0 && value <= min)
        || (valid && step > 0 && value >= max);
    });
  }

  function syncTotals() {
    const states = [...lines.values()];
    const total = cart.querySelector("[data-cart-total]");
    const totalValue = cart.querySelector("[data-cart-total-value]");
    const pending = states.some((line) => (line.dirty && !line.error) || line.removing);
    const invalid = states.some((line) => line.input.hasAttribute("aria-invalid"));
    const failed = states.some((line) => line.error || line.removing);
    const summaryUnavailable = total?.dataset.cartTotalAvailable !== "true" || failed || invalid;
    if (total && totalValue) {
      total.classList.toggle("is-unavailable", summaryUnavailable);
      if (summaryUnavailable) {
        totalValue.setAttribute("aria-hidden", "true");
      } else {
        totalValue.removeAttribute("aria-hidden");
      }
      if (pending) total.setAttribute("aria-busy", "true");
      else total.removeAttribute("aria-busy");
      const totalStatus = total.querySelector("[data-cart-total-status]");
      if (totalStatus) {
        totalStatus.textContent = pending
          ? "Пересчитываем предварительную сумму."
          : summaryUnavailable
            ? "Предварительная сумма недоступна. Проверьте вес позиций."
            : hasReconciled ? "Предварительная сумма обновлена." : "";
      }
    }

    states.forEach((line) => {
      const value = line.row.querySelector("[data-cart-line-total-value]");
      const unavailable = value.dataset.cartLineTotalAvailable !== "true"
        || line.error || line.removing || line.input.hasAttribute("aria-invalid");
      value.classList.toggle("is-unavailable", unavailable);
      value.setAttribute("aria-hidden", String(unavailable));
      const removeError = line.row.querySelector("[data-cart-remove-error]");
      removeError.textContent = line.removeError;
      removeError.hidden = !line.removeError;
      updateStepperState(line);
    });
  }

  function reconcile(page, submitted, revision) {
    const next = page.querySelector("[data-cart]");
    if (!next) throw new Error("Unexpected cart response");
    const nextLines = new Map([...next.querySelectorAll("[data-cart-line]")].map((row) => [row.dataset.cartLine, row]));
    const stale = submitted.revision !== revision;
    if (!stale && submitted.removing && nextLines.has(submitted.row.dataset.cartLine)) {
      submitted.removing = false;
      submitted.input.disabled = false;
      submitted.row.querySelector('[value="remove"]').disabled = false;
    }
    if (!stale && !submitted.removing) {
      submitted.dirty = false;
      submitted.error = "";
    }

    for (const [key, line] of lines) {
      const row = nextLines.get(key);
      if (!row) {
        line.row.remove();
        lines.delete(key);
        continue;
      }

      line.row.querySelector("[data-cart-price]").innerHTML = row.querySelector("[data-cart-price]").innerHTML;
      const lineTotal = line.row.querySelector("[data-cart-line-total-value]");
      const nextLineTotal = row.querySelector("[data-cart-line-total-value]");
      if (!line.dirty && !line.removing) {
        lineTotal.innerHTML = nextLineTotal.innerHTML;
        lineTotal.dataset.cartLineTotalAvailable = nextLineTotal.dataset.cartLineTotalAvailable;
      }
      const input = row.querySelector(".cart-weight");
      line.input.min = input.min;
      line.input.max = input.max;
      line.input.step = input.step;

      // Another line's response contains stored weights, not this field's rejected input.
      if (line.dirty || line.removing || (line !== submitted && line.input.hasAttribute("aria-invalid"))) continue;
      if (line !== submitted) line.input.value = input.value;
      const error = row.querySelector("[data-cart-weight-error]");
      if (input.hasAttribute("aria-invalid")) setFieldError(line, error.textContent, true);
      else setFieldError(line, "");
    }

    const currentTotal = cart.querySelector("[data-cart-total-value]");
    const nextTotal = next.querySelector("[data-cart-total-value]");
    const currentTotalRow = cart.querySelector("[data-cart-total]");
    const nextTotalRow = next.querySelector("[data-cart-total]");
    // Keep the last confirmed total until the response matches every edited line.
    const hasPendingWeights = [...lines.values()].some((line) => line.dirty || line.removing);
    if (!hasPendingWeights && currentTotal && nextTotal && currentTotalRow && nextTotalRow) {
      currentTotal.innerHTML = nextTotal.innerHTML;
      currentTotalRow.dataset.cartTotalAvailable = nextTotalRow.dataset.cartTotalAvailable;
    }

    const currentRequestForm = cart.querySelector("[data-retail-request-form]");
    const nextRequestForm = next.querySelector("[data-retail-request-form]");
    if (currentRequestForm && nextRequestForm) {
      for (const name of ["submission_key", "submission_token"]) {
        const currentField = currentRequestForm.elements.namedItem(name);
        const nextField = nextRequestForm.elements.namedItem(name);
        if (currentField && nextField) currentField.value = nextField.value;
      }
      const currentConfirmation = currentRequestForm.closest("[data-cart]").querySelector("[data-cart-confirmation]");
      const nextConfirmation = nextRequestForm.closest("[data-cart]").querySelector("[data-cart-confirmation]");
      if (currentConfirmation && nextConfirmation) {
        currentConfirmation.innerHTML = nextConfirmation.innerHTML;
      }
      for (const form of cart.querySelectorAll('form[id^="cart-item-"]')) {
        const preserve = form.elements.namedItem("preserve_confirmation");
        if (preserve) preserve.value = nextConfirmation?.querySelector("#id_confirm_updated_cart") ? "1" : "0";
      }
    }

    if (!lines.size) {
      cart.querySelector("[data-cart-body]").innerHTML = next.querySelector("[data-cart-body]").innerHTML;
    } else {
      const tbody = cart.querySelector("tbody");
      for (const [key, row] of nextLines) {
        if (!lines.has(key)) tbody.insertAdjacentHTML("beforeend", row.outerHTML);
      }
    }

    for (const row of cart.querySelectorAll("[data-cart-line]")) {
      if (!lines.has(row.dataset.cartLine)) {
        const line = makeLine(row);
        lines.set(row.dataset.cartLine, line);
        row.querySelectorAll("[data-cart-step]").forEach((button) => { button.hidden = false; });
      }
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
        hasReconciled = true;
      } catch {
        if (line.revision === revision && line.removing === removing) {
          if (removing) {
            line.removeError = "Не удалось удалить позицию. Проверьте соединение и нажмите «Удалить» ещё раз.";
            line.removing = false;
            line.input.disabled = false;
            line.row.querySelector('[value="remove"]').disabled = false;
          } else {
            line.error = "Не удалось сохранить изменения. Проверьте соединение и повторите ввод веса или нажмите Enter.";
            setFieldError(line, line.error);
          }
        }
      }
      syncTotals();
    }
    busy = false;
    syncTotals();
  }

  function enqueue(line) {
    if (!line.queued) {
      line.queued = true;
      queue.push(line);
    }
    void processQueue();
  }

  cart.querySelectorAll("[data-cart-step]").forEach((button) => { button.hidden = false; });
  lines.forEach(updateStepperState);
  syncTotals();

  cart.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cart-step]");
    if (!button || !cart.contains(button) || button.disabled) return;
    const row = button.closest("[data-cart-line]");
    const line = lines.get(row.dataset.cartLine);
    const min = Number(line.input.min) || 1;
    const max = Number(line.input.max) || 99999;
    const current = line.input.value === "" ? Number.NaN : Number(line.input.value);
    if (line.input.value !== "" && (!Number.isInteger(current) || current < min || current > max)) {
      setFieldError(line, "Укажите целое количество килограммов от 1 до 99 999.", true);
      line.input.focus();
      syncTotals();
      return;
    }
    const base = Number.isFinite(current) ? current : min - 1;
    const next = Math.min(max, Math.max(min, base + Number(button.dataset.cartStep)));
    if (String(next) === line.input.value) return;
    line.input.value = String(next);
    line.input.dispatchEvent(new Event("input", { bubbles: true }));
  });

  cart.addEventListener("input", (event) => {
    if (!event.target.matches(".cart-weight")) return;
    const row = event.target.closest("[data-cart-line]");
    const line = lines.get(row.dataset.cartLine);
    line.revision += 1;
    line.dirty = true;
    line.error = "";
    setFieldError(line, "");
    syncTotals();
    enqueue(line);
  });

  cart.addEventListener("change", (event) => {
    if (!event.target.matches(".cart-weight")) return;
    const row = event.target.closest("[data-cart-line]");
    const line = lines.get(row.dataset.cartLine);
    if (line.dirty) enqueue(line);
  });

  cart.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches(".cart-weight")) return;
    event.preventDefault();
    const row = event.target.closest("[data-cart-line]");
    const line = lines.get(row.dataset.cartLine);
    if (line.error) {
      line.error = "";
      setFieldError(line, "");
    }
    if (!line.dirty) {
      line.revision += 1;
      line.dirty = true;
    }
    syncTotals();
    enqueue(line);
  });

  cart.addEventListener("submit", (event) => {
    const line = [...lines.values()].find((entry) => entry.form === event.target);
    if (!line) {
      if (event.target.matches("[data-retail-request-form]")) {
        const pending = busy || queue.length > 0 || [...lines.values()].some((entry) =>
          entry.dirty || entry.removing || entry.error || entry.input.hasAttribute("aria-invalid")
          || !entry.input.checkValidity()
        );
        if (pending) {
          event.preventDefault();
          const invalid = [...lines.values()].find((entry) =>
            entry.input.hasAttribute("aria-invalid") || !entry.input.checkValidity()
          );
          invalid?.input.focus();
        }
      }
      return;
    }
    event.preventDefault();
    line.error = "";
    if (event.submitter?.value === "remove") {
      setFieldError(line, "");
      line.removeError = "";
      line.removing = true;
      line.input.disabled = true;
      event.submitter.disabled = true;
    }
    enqueue(line);
    syncTotals();
  });
})();
