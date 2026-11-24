// Godfather README media recipe for screen-kit (https://github.com/maxvyshniakov/screen-kit).
// Regenerate dark and light stills + animations from the repository root:
//   screen-kit capture --config godfather/tools/screen-kit.config.mjs --out assets
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEMO = resolve(HERE, 'dashboard-demo.py');
const FIXTURES = resolve(HERE, '../tests/fixtures/procs');

const THEMES = [
  ['dashboard', 'seti', '#111213', '#cacecd'],
  ['dashboard-light', 'prolight', '#ffffff', '#191919'],
];

// Full tour: Overview navigation & DETAILS scroll -> unified event log switch -> Lanes page -> Spawns page -> Overview.
const STEPS = [
  { waitFor: ['SLOTS', '1 2 3 pages', 'slots held'], timeout: 15, startCapture: true },
  { hold: 1.2 },
  { key: 'j' },
  { hold: 1.0 },
  { key: 'j' },
  { hold: 0.9 },
  { wheel: { col: 149, row: 11, down: 4 } },
  { hold: 1.0 },
  { key: 'g' },
  { hold: 0.7 },
  { key: 'f' },
  { waitFor: 'LANES LOG', timeout: 5 },
  { hold: 1.2 },
  { key: '2' },
  { waitFor: 'LANE DETAILS', timeout: 5 },
  { hold: 1.4 },
  { key: 'j' },
  { hold: 1.2 },
  { key: '3' },
  { waitFor: 'SPAWN DETAILS', timeout: 5 },
  { hold: 1.4 },
  { key: 'j' },
  { hold: 1.2 },
  { key: '1' },
  { waitFor: 'SLOTS', timeout: 5 },
  { hold: 1.0 },
];

const recipes = {};
for (const [name, preset, bg, fg] of THEMES) {
  recipes[name] = {
    source: 'terminal',
    command: [
      'env',
      'TZ=Etc/GMT-2',
      `GODFATHER_FIXTURES=${FIXTURES}`,
      'GODFATHER_THEME=auto',
      `GODFATHER_TERMINAL_BG=${bg}`,
      `GODFATHER_TERMINAL_FG=${fg}`,
      'python3', DEMO, '--width', '156', '--height', '43',
    ],
    columns: 156,
    rows: 43,
    theme: preset,
    steps: STEPS,
    outputs: [
      { name, format: 'png', at: 0.5 },
      { name, format: 'webp', fps: 4 },
    ],
  };
}

export default { recipes };
