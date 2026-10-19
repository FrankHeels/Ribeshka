const section = document.querySelector("[data-contact-map]");
const keyElement = document.getElementById("yandex-maps-api-key");
const locationElement = document.getElementById("contact-map-location");

if (section && keyElement && locationElement) {
  const canvas = section.querySelector("#contact-map");
  const fallback = section.querySelector("[data-map-fallback]");
  const status = section.querySelector("[data-map-status]");
  const controls = section.querySelector("[data-map-controls]");
  const location = JSON.parse(locationElement.textContent);
  let map;
  let failed = false;
  let loaded = false;
  let timer;

  function fail() {
    failed = true;
    clearTimeout(timer);
    map?.destroy();
    map = null;
    canvas.hidden = true;
    controls.hidden = true;
    fallback.hidden = false;
    section.setAttribute("aria-busy", "false");
    section.dataset.mapState = "error";
    status.textContent = "Не удалось загрузить карту. Адрес и ссылка на Яндекс Карты доступны ниже.";
  }

  async function init() {
    section.setAttribute("aria-busy", "true");
    section.dataset.mapState = "loading";
    status.textContent = "Загружаем карту адреса…";
    timer = setTimeout(fail, 20000);
    const script = document.createElement("script");
    const url = new URL("https://api-maps.yandex.ru/v3/");
    url.searchParams.set("apikey", JSON.parse(keyElement.textContent));
    url.searchParams.set("lang", "ru_RU");
    script.src = url.href;
    script.async = true;
    try {
      await new Promise((resolve, reject) => {
        script.onload = resolve;
        script.onerror = reject;
        document.head.append(script);
      });
      if (failed) return;
      await window.ymaps3.ready;
      if (failed) return;
      const { YMap, YMapDefaultSchemeLayer, YMapDefaultFeaturesLayer, YMapMarker, YMapListener } = window.ymaps3;
      canvas.hidden = false;
      map = new YMap(canvas, {
        location: { center: location.coordinates, zoom: location.zoom },
        mode: "raster",
        theme: "dark",
        behaviors: ["drag", "pinchZoom", "dblClick"],
        zoomRange: { min: 10, max: 19 },
        copyrights: true,
        distributionPosition: "top left",
      });
      map.addChild(new YMapListener({
        onStateChanged: (state) => {
          if (failed || loaded) return;
          const tiles = state.getLayerState(`${YMapDefaultSchemeLayer.defaultProps.source}:ground`, "tile");
          if (tiles?.tilesLoaded > 0) {
            loaded = true;
            clearTimeout(timer);
            fallback.hidden = true;
            controls.hidden = false;
            section.setAttribute("aria-busy", "false");
            section.dataset.mapState = "ready";
          }
        },
      }));
      map.addChild(new YMapDefaultSchemeLayer());
      map.addChild(new YMapDefaultFeaturesLayer());
      const marker = document.createElement("a");
      marker.className = "contact-map-marker";
      marker.href = location.map_url;
      marker.target = "_blank";
      marker.rel = "noopener noreferrer";
      marker.title = location.address;
      marker.setAttribute("aria-label", `${location.address}. Открыть в Яндекс Картах`);
      marker.innerHTML = '<svg width="40" height="48" viewBox="0 0 40 48" aria-hidden="true"><path d="M20 2C10.1 2 2 9.8 2 19.5 2 33 20 46 20 46s18-13 18-26.5C38 9.8 29.9 2 20 2Z"/><circle cx="20" cy="19" r="6"/></svg>';
      map.addChild(new YMapMarker({ coordinates: location.coordinates }, marker));

      for (const button of controls.querySelectorAll("[data-map-zoom]")) {
        button.addEventListener("click", () => {
          map?.setLocation({ zoom: Math.max(10, Math.min(19, map.zoom + Number(button.dataset.mapZoom))) });
        });
      }
      controls.querySelector("[data-map-reset]").addEventListener("click", () => {
        map?.setLocation({ center: location.coordinates, zoom: location.zoom });
      });
    } catch {
      // Provider errors may contain the browser key or full request URL.
      fail();
    }
  }

  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        init();
      }
    }, { rootMargin: "240px" });
    observer.observe(section);
  } else {
    init();
  }
}
