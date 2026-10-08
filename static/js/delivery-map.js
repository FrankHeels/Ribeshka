import { DELIVERY_ZONES } from "./delivery-zones.js?v=2026-10-08-1";

const section = document.querySelector("[data-delivery-map]");
const keyElement = document.getElementById("yandex-maps-api-key");

if (section && keyElement) {
  const canvas = section.querySelector("#delivery-map");
  const fallback = section.querySelector("[data-map-fallback]");
  const status = section.querySelector("[data-map-status]");
  const controls = section.querySelector("[data-map-controls]");
  const points = DELIVERY_ZONES.flatMap((zone) => zone.geometry.coordinates.flat());
  const bounds = [
    [Math.min(...points.map(([lng]) => lng)), Math.min(...points.map(([, lat]) => lat))],
    [Math.max(...points.map(([lng]) => lng)), Math.max(...points.map(([, lat]) => lat))],
  ];
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
    status.textContent = "Не удалось загрузить карту. Воспользуйтесь ссылкой «Открыть в Картах» или уточните доставку при подтверждении заявки.";
  }

  async function init() {
    section.setAttribute("aria-busy", "true");
    section.dataset.mapState = "loading";
    status.textContent = "Загружаем карту зон доставки…";
    // Bound both API readiness and the first map tiles, including a rejected key.
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
      const { YMap, YMapDefaultSchemeLayer, YMapDefaultFeaturesLayer, YMapFeature, YMapListener } = window.ymaps3;
      canvas.hidden = false;
      map = new YMap(canvas, {
        location: { bounds },
        margin: [32, 32, 48, 32],
        mode: "raster",
        behaviors: ["drag", "pinchZoom", "dblClick"],
        zoomRange: { min: 8, max: 18 },
        // Keep Yandex attribution and links visible, including on small screens.
        copyrights: true,
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
      for (const zone of DELIVERY_ZONES) {
        map.addChild(new YMapFeature({
          id: zone.id,
          geometry: zone.geometry,
          style: zone.style,
          properties: { name: zone.name },
        }));
      }
      for (const button of controls.querySelectorAll("[data-map-zoom]")) {
        button.addEventListener("click", () => {
          map?.setLocation({ zoom: Math.max(8, Math.min(18, map.zoom + Number(button.dataset.mapZoom))) });
        });
      }
      controls.querySelector("[data-map-reset]").addEventListener("click", () => {
        map?.setLocation({ bounds });
      });
    } catch {
      // Do not log the provider error: it may contain the API key/request URL.
      fail();
    }
  }

  // Load the third-party API only when the delivery block approaches the viewport.
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
