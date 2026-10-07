(() => {
  const control = document.querySelector("[data-product-weight]");
  if (!control) return;

  const input = control.querySelector("input");
  const buttons = [...control.querySelectorAll("[data-product-weight-step]")];
  const error = document.getElementById("product-weight-error");
  const min = Number(input.min);
  const max = Number(input.max);

  function syncButtons() {
    const value = input.value === "" ? Number.NaN : Number(input.value);
    const valid = Number.isInteger(value) && value >= min && value <= max;
    buttons.forEach((button) => {
      const step = Number(button.dataset.productWeightStep);
      button.disabled = input.matches(":disabled")
        || (valid && step < 0 && value <= min)
        || (valid && step > 0 && value >= max);
    });
  }

  buttons.forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", () => {
      const current = input.value === "" ? Number.NaN : Number(input.value);
      if (input.value !== "" && (!Number.isInteger(current) || current < min || current > max)) {
        error.textContent = "Укажите целое количество килограммов от 1 до 99 999.";
        error.hidden = false;
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", error.id);
        input.focus();
        return;
      }
      const base = Number.isFinite(current) ? current : min - 1;
      input.value = String(Math.min(max, Math.max(min, base + Number(button.dataset.productWeightStep))));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });

  input.addEventListener("input", () => {
    error.hidden = true;
    error.textContent = "";
    input.removeAttribute("aria-invalid");
    input.removeAttribute("aria-describedby");
    syncButtons();
  });
  syncButtons();
})();
