import { installDom, test, run, assertEqual } from './harness.mjs';

installDom();
const prefs = await import('../js/core/prefs.js');
const ui = await import('../js/ui.js');

function storage() {
  const values = new Map();
  window.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
  return values;
}

test('preferences persist under their supported keys', () => {
  const values = storage();
  ui.persistColor('solarized-light');
  assertEqual(values.get('zicato.console.theme'), 'solarized-light');
  assertEqual(ui.readColor(), 'solarized-light');
});

test('retired preference keys leave the appearance defaults in effect', () => {
  const values = storage();
  values.set('zicato.T.theme', 'solarized-light');
  assertEqual(ui.readColor(), ui.DEFAULT_COLOR);
  assertEqual(prefs.readPrefRaw('missing', 'absent'), 'absent');
});

test('a stored theme the console no longer ships reads as the default', () => {
  const values = storage();
  for (const value of ['dracula', 'google-light', 'paper', 'ubuntu', '']) {
    values.set('zicato.console.theme', value);
    assertEqual(ui.readColor(), ui.DEFAULT_COLOR);
  }
});

test('a stored typeface preference is ignored without error', () => {
  const values = storage();
  values.set('zicato.console.typeface', 'literata');
  assertEqual(ui.readColor(), ui.DEFAULT_COLOR);
  assertEqual(ui.readType, undefined, 'the console holds no typeface preference');
  assertEqual(values.get('zicato.console.typeface'), 'literata', 'the stored key is left alone');
});

test('unavailable browser storage leaves defaults usable', () => {
  window.localStorage = {
    getItem() { throw new Error('storage unavailable'); },
    setItem() { throw new Error('storage unavailable'); },
  };
  ui.persistColor('solarized-light');
  assertEqual(ui.readColor(), ui.DEFAULT_COLOR);
});

await run();
