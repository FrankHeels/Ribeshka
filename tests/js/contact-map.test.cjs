// Run: node --test tests/js/contact-map.test.cjs
// The SDK is simulated; real map tiles are checked separately in the browser.
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const source = readFileSync(resolve(__dirname, '../../static/js/contact-map.js'), 'utf8');
const point = { coordinates: [20.608287, 54.764501], zoom: 18, address: 'Гурьевск, Каштановая, 1Б', map_url: 'https://yandex.ru/maps/' };

function harness({ key = true } = {}) {
  const canvas = { hidden: true };
  const fallback = { hidden: false };
  const status = { textContent: '' };
  const makeButton = (zoom) => ({ dataset: { mapZoom: zoom }, addEventListener(_, handler) { this.click = handler; } });
  const buttons = [makeButton('1'), makeButton('-1')];
  const reset = makeButton();
  const controls = { hidden: true, querySelectorAll: () => buttons, querySelector: () => reset };
  const section = {
    dataset: {},
    setAttribute(name, value) { this[name] = value; },
    querySelector: (selector) => ({ '#contact-map': canvas, '[data-map-fallback]': fallback, '[data-map-status]': status, '[data-map-controls]': controls })[selector],
  };
  const scripts = [];
  const timers = new Map();
  const maps = [];
  class FakeMap {
    constructor(element, options) { this.element = element; this.options = options; this.zoom = options.location.zoom; this.children = []; maps.push(this); }
    addChild(child) { this.children.push(child); return this; }
    setLocation(value) { this.lastLocation = value; if (value.zoom !== undefined) this.zoom = value.zoom; }
    destroy() { this.destroyed = true; }
  }
  class Listener { constructor(props) { this.props = props; } }
  class Marker { constructor(props, element) { this.props = props; this.element = element; } }
  class Scheme {}
  Scheme.defaultProps = { source: 'scheme' };
  const document = {
    querySelector: () => section,
    getElementById: (id) => id === 'contact-map-location' ? { textContent: JSON.stringify(point) } : key ? { textContent: JSON.stringify('synthetic-key') } : null,
    createElement: () => ({ setAttribute(name, value) { this[name] = value; } }),
    head: { append: (script) => scripts.push(script) },
  };
  vm.runInNewContext(source, {
    document, URL,
    window: { ymaps3: { ready: Promise.resolve(), YMap: FakeMap, YMapListener: Listener, YMapMarker: Marker, YMapDefaultSchemeLayer: Scheme, YMapDefaultFeaturesLayer: class {} } },
    setTimeout: (fn) => { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout: (id) => timers.delete(id),
  });
  return { section, canvas, fallback, controls, scripts, timers, maps, buttons, reset, Listener, Marker };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('missing key never requests the SDK and leaves fallback available', () => {
  const h = harness({ key: false });
  assert.equal(h.scripts.length, 0);
  assert.equal(h.fallback.hidden, false);
  assert.equal(h.canvas.hidden, true);
});

test('real readiness requires loaded tiles; marker, dark theme and controls use the requested point', async () => {
  const h = harness();
  h.scripts[0].onload();
  await settle();
  const map = h.maps[0];
  assert.equal(map.options.theme, 'dark');
  assert.equal(map.options.copyrights, true);
  assert.equal(map.options.distributionPosition, 'top left');
  assert.equal(map.options.behaviors.includes('scrollZoom'), false);
  assert.equal(h.fallback.hidden, false);
  const listener = map.children.find((child) => child instanceof h.Listener);
  listener.props.onStateChanged({ getLayerState: () => ({ tilesLoaded: 0 }) });
  assert.equal(h.section.dataset.mapState, 'loading');
  listener.props.onStateChanged({ getLayerState: () => ({ tilesLoaded: 1 }) });
  assert.equal(h.section.dataset.mapState, 'ready');
  assert.equal(h.fallback.hidden, true);
  assert.equal(h.controls.hidden, false);
  assert.equal(h.timers.size, 0);
  const marker = map.children.find((child) => child instanceof h.Marker);
  assert.equal(JSON.stringify(marker.props.coordinates), JSON.stringify(point.coordinates));
  assert.equal(marker.element.href, point.map_url);
  h.buttons[0].click();
  assert.equal(map.zoom, 19);
  h.buttons[0].click();
  assert.equal(map.zoom, 19);
  h.buttons[1].click();
  assert.equal(map.zoom, 18);
  h.reset.click();
  assert.equal(JSON.stringify(map.lastLocation), JSON.stringify({ center: point.coordinates, zoom: point.zoom }));
});

test('SDK failure leaves the readable fallback and clears its timer', async () => {
  const h = harness();
  h.scripts[0].onerror(new Error('network'));
  await settle();
  assert.equal(h.section.dataset.mapState, 'error');
  assert.equal(h.canvas.hidden, true);
  assert.equal(h.controls.hidden, true);
  assert.equal(h.fallback.hidden, false);
  assert.equal(h.timers.size, 0);
});

test('late SDK response cannot resurrect a timed-out map', async () => {
  const h = harness();
  [...h.timers.values()][0]();
  h.scripts[0].onload();
  await settle();
  assert.equal(h.maps.length, 0);
  assert.equal(h.section.dataset.mapState, 'error');
  assert.equal(h.fallback.hidden, false);
});

test('tile timeout destroys an initialized map and ignores late tile readiness', async () => {
  const h = harness();
  h.scripts[0].onload();
  await settle();
  const map = h.maps[0];
  [...h.timers.values()][0]();
  assert.equal(map.destroyed, true);
  map.children.find((child) => child instanceof h.Listener).props.onStateChanged({ getLayerState: () => ({ tilesLoaded: 1 }) });
  assert.equal(h.section.dataset.mapState, 'error');
  assert.equal(h.fallback.hidden, false);
  assert.equal(h.canvas.hidden, true);
});
