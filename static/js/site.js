(() => {
  const menu = document.querySelector(".mobile-menu");
  if (!menu) return;
  const summary = menu.querySelector("summary");
  const navigation = menu.querySelector("nav");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let animation;
  let closing = false;

  function closeMenu({ restoreFocus = false, immediate = false } = {}) {
    if (restoreFocus) summary.focus();
    if (!menu.open || closing) return;
    animation?.cancel();
    if (immediate || reducedMotion.matches || !navigation.animate) {
      menu.open = false;
      return;
    }
    closing = true;
    navigation.inert = true;
    animation = navigation.animate(
      [{ opacity: 1, transform: "translateY(0)" }, { opacity: 0, transform: "translateY(-6px)" }],
      { duration: 160, easing: "ease-in" },
    );
    animation.finished.then(() => {
      menu.open = false;
      navigation.inert = false;
      closing = false;
    }).catch(() => {});
  }

  summary.addEventListener("click", (event) => {
    if (menu.open && !closing) {
      event.preventDefault();
      closeMenu();
    } else if (closing) {
      event.preventDefault();
      animation?.cancel();
      closing = false;
      navigation.inert = false;
    }
  });
  menu.addEventListener("toggle", () => {
    if (!menu.open || closing || reducedMotion.matches || !navigation.animate) return;
    animation?.cancel();
    animation = navigation.animate(
      [{ opacity: 0, transform: "translateY(-8px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 260, easing: "cubic-bezier(.22, 1, .36, 1)" },
    );
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) {
      closeMenu({ restoreFocus: true });
    }
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) closeMenu();
  });
  menu.addEventListener("focusout", (event) => {
    if (event.relatedTarget && !menu.contains(event.relatedTarget)) closeMenu();
  });
  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) closeMenu({ immediate: true });
  });
  window.matchMedia("(min-width: 901px)").addEventListener("change", (event) => {
    if (event.matches) {
      animation?.cancel();
      closing = false;
      navigation.inert = false;
      closeMenu({ immediate: true });
    }
  });
  reducedMotion.addEventListener("change", () => {
    animation?.cancel();
    if (closing) {
      menu.open = false;
      closing = false;
      navigation.inert = false;
    }
  });
})();

(() => {
  const menus = [...document.querySelectorAll(".assortment-menu")];
  if (!menus.length) return;
  const desktopHover = window.matchMedia("(min-width: 901px) and (hover: hover) and (pointer: fine)");

  function closeMenus({ restoreFocus = false } = {}) {
    menus.forEach((menu) => {
      if (!menu.open) return;
      menu.open = false;
      if (restoreFocus && !menu.closest(".mobile-menu")) menu.querySelector("summary").focus();
    });
  }

  menus.forEach((menu) => {
    const summary = menu.querySelector("summary");
    let closeTimer;

    menu.addEventListener("mouseenter", () => {
      if (!desktopHover.matches || menu.closest(".mobile-menu")) return;
      window.clearTimeout(closeTimer);
      menu.open = true;
    });
    menu.addEventListener("mouseleave", () => {
      if (!desktopHover.matches || menu.closest(".mobile-menu")) return;
      window.clearTimeout(closeTimer);
      closeTimer = window.setTimeout(() => {
        if (!menu.matches(":hover") && !menu.querySelector(":focus-visible")) menu.open = false;
      }, 200);
    });
    summary.addEventListener("click", (event) => {
      if (!desktopHover.matches || menu.closest(".mobile-menu") || event.detail === 0) return;
      event.preventDefault();
      menu.open = true;
    });

    menu.addEventListener("toggle", () => {
      if (menu.open) {
        menus.forEach((other) => {
          if (other !== menu) other.open = false;
        });
      }
    });
    menu.addEventListener("focusout", (event) => {
      if (event.relatedTarget && !menu.contains(event.relatedTarget)) menu.open = false;
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenus({ restoreFocus: true });
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".assortment-menu")) closeMenus();
  });
})();
