(() => {
  const sections = [...document.querySelectorAll(".home-page .home-screen, .home-page .site-footer")];
  if (!sections.length) return;

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let previousY = window.scrollY;
  let direction = 0;
  let touching = false;
  let settling = false;
  let idleTimer;
  let settlingTimer;

  function cancelSettling() {
    clearTimeout(idleTimer);
    clearTimeout(settlingTimer);
    settling = false;
    direction = 0;
    previousY = window.scrollY;
  }

  function settle() {
    if (touching || settling || reducedMotion.matches || !direction) return;
    const currentY = window.scrollY;
    const headerHeight = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    const maximumY = document.documentElement.scrollHeight - window.innerHeight;
    const threshold = Math.min(160, Math.max(64, (window.innerHeight - headerHeight) * .18));
    const targets = sections.map((section) => Math.max(0, Math.min(maximumY, section.getBoundingClientRect().top + currentY - headerHeight)));
    const target = targets
      .filter((value) => (value - currentY) * direction > 2 && Math.abs(value - currentY) <= threshold)
      .sort((a, b) => Math.abs(a - currentY) - Math.abs(b - currentY))[0];
    if (target === undefined) return;
    settling = true;
    window.scrollTo({ top: target, behavior: "smooth" });
    settlingTimer = setTimeout(() => { settling = false; direction = 0; }, 1200);
  }

  function scheduleSettling() {
    clearTimeout(idleTimer);
    if (!touching && !settling && !reducedMotion.matches) idleTimer = setTimeout(settle, 200);
  }

  window.addEventListener("scroll", () => {
    const currentY = window.scrollY;
    const delta = currentY - previousY;
    previousY = currentY;
    if (settling || Math.abs(delta) < 1) return;
    direction = Math.sign(delta);
    scheduleSettling();
  }, { passive: true });
  window.addEventListener("scrollend", () => {
    if (settling) cancelSettling();
    else scheduleSettling();
  }, { passive: true });
  window.addEventListener("wheel", cancelSettling, { passive: true });
  window.addEventListener("keydown", cancelSettling);
  window.addEventListener("touchstart", () => { touching = true; cancelSettling(); }, { passive: true });
  const finishTouch = () => { touching = false; scheduleSettling(); };
  window.addEventListener("touchend", finishTouch, { passive: true });
  window.addEventListener("touchcancel", finishTouch, { passive: true });
  window.addEventListener("resize", cancelSettling, { passive: true });
  reducedMotion.addEventListener("change", cancelSettling);
})();
