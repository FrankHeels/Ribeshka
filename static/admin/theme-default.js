try {
  const storedTheme = window.localStorage.getItem("adminTheme");
  if (storedTheme === null) {
    window.localStorage.setItem("adminTheme", JSON.stringify("light"));
  } else {
    try {
      if (typeof JSON.parse(storedTheme) !== "string") {
        window.localStorage.setItem("adminTheme", JSON.stringify("light"));
      }
    } catch {
      // Older builds stored unquoted values; migrate those before Alpine Persist
      // attempts to parse them. Preserve an explicit previous dark selection.
      window.localStorage.setItem(
        "adminTheme",
        JSON.stringify(storedTheme === "dark" ? "dark" : "light"),
      );
    }
  }
} catch {
  // The admin remains usable with Unfold's own default if storage is blocked.
}
