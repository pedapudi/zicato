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
  ui.persistType('literata');
  ui.persistColor('dracula');
  assertEqual(values.get('zicato.console.typeface'), 'literata');
  assertEqual(ui.readType(), 'literata');
  assertEqual(ui.readColor(), 'dracula');
});

test('retired preference keys leave the appearance defaults in effect', () => {
  const values = storage();
  values.set('zicato.T.theme', 'dracula');
  values.set('zicato.T.typeface', 'literata');
  assertEqual(ui.readColor(), ui.DEFAULT_COLOR);
  assertEqual(ui.readType(), ui.DEFAULT_TYPE);
  assertEqual(prefs.readPrefRaw('missing', 'absent'), 'absent');
});

test('retired typeface values use the default without translation', () => {
  const values = storage();
  for (const value of ['editorial', 'display', 'E8', 'D14']) {
    values.set('zicato.console.typeface', value);
    assertEqual(ui.readType(), ui.DEFAULT_TYPE);
  }
});

test('unavailable browser storage leaves defaults usable', () => {
  window.localStorage = {
    getItem() { throw new Error('storage unavailable'); },
    setItem() { throw new Error('storage unavailable'); },
  };
  ui.persistType('literata');
  assertEqual(ui.readType(), ui.DEFAULT_TYPE);
});

await run();
