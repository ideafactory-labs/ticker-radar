// Browser checks from the idea's "How we'll know it works", in headless Chromium.
//   make e2e                        -> builds site/data from fixtures, serves site/ locally
//   BASE_URL=https://... node tests/e2e.mjs   -> checks the deployed page (make verify)
// Screenshots go to evidence/ (CI uploads them).
import { chromium, devices } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { serve } from './serve.mjs';

const OUT = process.env.EVIDENCE_DIR || 'evidence';
fs.mkdirSync(OUT, { recursive: true });
const LIVE = !!process.env.BASE_URL;
let server = null;
let base = process.env.BASE_URL;
if (!base) { server = await serve(0); base = `http://127.0.0.1:${server.address().port}/`; }
if (!base.endsWith('/')) base += '/';
// Fixture data is dated 2026-11-24; pin the page's "today" so the checks never age.
const Q = LIVE ? '' : '?today=2026-11-25';

const browser = await chromium.launch();
const results = [];
let failed = 0;
async function check(name, fn) {
  const t0 = Date.now();
  try { await fn(); results.push(`ok   ${name} (${Date.now() - t0} ms)`); }
  catch (e) { failed++; results.push(`FAIL ${name}: ${e.message}`); }
  console.log(results[results.length - 1]);
}
async function open(ctxOpts = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, ...ctxOpts });
  // Fonts are cosmetic and come from a third party; keep tests hermetic.
  await context.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  const page = await context.newPage();
  const errors = [], requests = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/fonts\.g|ERR_FAILED/.test(m.text())) errors.push(m.text()); });
  page.on('request', (r) => requests.push(r.url()));
  await page.goto(base + Q, { waitUntil: 'load' });
  await page.waitForSelector('body[data-ready="1"]', { timeout: 20000 });
  return { context, page, errors, requests };
}

await check('every section renders with values, sources and as-of dates; no console errors', async () => {
  const { context, page, errors } = await open();
  await page.waitForSelector('.card .px');
  assert.ok(await page.locator('.card .px').count() >= 1, 'watchlist card with a price');
  assert.ok(await page.locator('#bills tbody tr').count() >= 5, 'bill rows');
  assert.ok(await page.locator('#curve circle').count() >= 5, 'yield curve points');
  assert.ok(await page.locator('#auctions tbody tr').count() >= 1, 'auction rows');
  assert.ok(await page.locator('.ipo').count() >= 1, 'IPO entries');
  assert.ok(await page.locator('.ind .val').count() >= 1, 'indicator values');
  assert.ok(await page.locator('.cal-row').count() >= 1, 'calendar rows');
  for (const id of ['src-rates', 'src-ipos', 'src-ind', 'src-prices']) {
    const t = await page.locator('#' + id).textContent();
    assert.match(t, /as of/, `${id} shows an as-of date: ${t}`);
  }
  assert.match(await page.locator('#src-ipos').textContent(), /unofficial/i);
  await page.screenshot({ path: `${OUT}/1-desktop.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await context.close();
});

await check('search by company name adds a card; it survives a reload and ✕ removes it', async () => {
  const { context, page, errors } = await open();
  await page.fill('#q', 'bank of america');
  await page.waitForSelector('#results button');
  const first = await page.locator('#results button').first().textContent();
  assert.match(first, /^BAC/, `first result is BAC: ${first}`);
  await page.keyboard.press('Enter');
  await page.waitForSelector('.card[data-tk="BAC"] .px');
  await page.reload();
  await page.waitForSelector('body[data-ready="1"]');
  await page.waitForSelector('.card[data-tk="BAC"] .px');
  await page.locator('.card[data-tk="BAC"] .x').click();
  assert.equal(await page.locator('.card[data-tk="BAC"]').count(), 0);
  await page.reload();
  await page.waitForSelector('body[data-ready="1"]');
  assert.equal(await page.locator('.card[data-tk="BAC"]').count(), 0, 'removal persisted');
  assert.deepEqual(errors, []);
  await context.close();
});

await check('cards show period changes, one headline signal and more behind a tap; glance strip renders', async () => {
  const { context, page, errors } = await open();
  const card = page.locator('.card[data-tk="NVDA"]');
  await card.locator('.periods').waitFor();
  assert.match(await card.locator('.periods').textContent(), /1w [+-][\d.]+%.*1m [+-][\d.]+%.*YTD/);
  assert.equal(await card.locator('.signal').count(), 1, 'exactly one headline signal');
  if (!LIVE) assert.match(await card.locator('.signal').textContent(), /Unusual week \(\+7\.9%\)/);
  assert.equal(await card.locator('details.more .row').first().isVisible(), false, 'details start closed');
  await card.locator('details.more summary').click();
  assert.match(await card.locator('details.more').textContent(), /vs S&P 500, 3 months.*From 52-week high.*vs 200-day average/s);
  assert.ok(await page.locator('#glance tbody tr').count() >= (LIVE ? 5 : 4), 'glance rows');
  assert.match(await page.locator('#glance').textContent(), /US stocks.*Gold/s);
  await card.screenshot({ path: `${OUT}/3-card-signals.png` });
  assert.deepEqual(errors, []);
  await context.close();
});

await check('the gist: one sentence, six dials, and a tap opens the numbers, history and rule', async () => {
  const { context, page, errors } = await open();
  await page.locator('#dials .dial').first().waitFor();
  assert.match(await page.locator('#gist-summary').textContent(), /^In the US, .+\.$/);
  assert.equal(await page.locator('#dials .dial').count(), 6);
  for (const d of await page.locator('#dials .dial').all()) assert.match(await d.textContent(), /OK|Watch|Alert/);
  assert.equal(await page.locator('#dial-detail').isVisible(), false, 'detail starts closed');
  await page.locator('.dial[data-dial="recession"]').click();
  const det = page.locator('#dial-detail');
  await det.waitFor();
  const text = await det.textContent();
  assert.match(text, /yield curve/i);
  assert.match(text, /Sahm rule/);
  assert.match(text, /How this is judged/);
  assert.match(text, /What this is\./);
  assert.match(text, /Why it matters\./);
  assert.match(text, /Direction/);
  // every dial tile shows which way it moved and that it can be opened
  for (const d of await page.locator('#dials .dial').all()) assert.match(await d.textContent(), /[▲▼■].*(Explain|Close)/s);
  // a "?" explains a row in plain words, and closes again
  const sahm = det.locator('.part', { hasText: 'Sahm rule' });
  assert.equal(await sahm.locator('.helpbox').isVisible(), false);
  await sahm.locator('button.q').click();
  assert.match(await sahm.locator('.helpbox').textContent(), /Claudia Sahm/);
  await sahm.locator('button.q').click();
  assert.equal(await sahm.locator('.helpbox').isVisible(), false);
  if (!LIVE) assert.match(text, /inversions came before 3 of 3 recessions, with 1 false alarm/);
  assert.ok(await det.locator('.hist svg rect').count() >= 1, 'recession shading on the history chart');
  await page.locator('#gist-panel').screenshot({ path: `${OUT}/4-gist.png` });
  await page.locator('.dial[data-dial="recession"]').click();
  assert.equal(await det.isVisible(), false, 'second tap closes it');
  assert.deepEqual(errors, []);
  await context.close();
});

await check('sections explain themselves; rates are summarised with the tables behind a tap; glance shows prices', async () => {
  const { context, page, errors } = await open();
  await page.locator('#rates-summary .stat').first().waitFor();
  const stats = await page.locator('#rates-summary').textContent();
  assert.match(stats, /3-month T-bill\s*[\d.]+%/);
  assert.match(stats, /10-year Treasury\s*[\d.]+%/);
  assert.match(stats, /Yield curve\s*(Normal|Inverted)/);
  assert.equal(await page.locator('#bills').isVisible(), false, 'full table starts collapsed');
  await page.locator('#rates-more summary').click();
  assert.equal(await page.locator('#bills').isVisible(), true);
  for (const id of ['help-gist', 'help-glance', 'help-watch', 'help-rates', 'help-cal']) {
    assert.equal(await page.locator('#' + id).isVisible(), false, id + ' starts hidden');
    await page.locator(`button.q[data-q="${id}"]`).click();
    assert.ok((await page.locator('#' + id).textContent()).length > 80, id + ' has an explanation');
  }
  assert.match(await page.locator('#help-glance').textContent(), /VEA holds large companies in developed countries outside the US/);
  assert.match(await page.locator('#glance thead').textContent(), /Fund price/);
  assert.match(await page.locator('#glance tbody tr').first().textContent(), /\$[\d,.]+/);
  assert.deepEqual(errors, []);
  await context.close();
});

if (!LIVE) await check('a stale cached script heals itself: one reload, then everything works, never a loop', async () => {
  // A deploy stamps index.html and app.js with one build id. Simulate a browser that has the
  // new page but an older stamped script cached: the first app.js it gets is from build OLD.
  const html = fs.readFileSync('site/index.html', 'utf8')
    .replace('<head>\n', '<head>\n<meta name="build" content="NEW">\n').replace('src="app.js"', 'src="app.js?v=NEW"');
  const js = fs.readFileSync('site/app.js', 'utf8');
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  let served = 0;
  await context.route(/\/(\?.*)?$/, (r) => r.fulfill({ body: html, contentType: 'text/html' }));
  await context.route(/\/app\.js/, (r) => { served++; r.fulfill({ body: js.replace('__BUILD__', served === 1 ? 'OLD' : 'NEW'), contentType: 'text/javascript' }); });
  const page = await context.newPage();
  let navs = 0; page.on('framenavigated', (f) => { if (f === page.mainFrame()) navs++; });
  await page.goto(base + Q);
  await page.waitForSelector('body[data-ready="1"]');
  await page.waitForSelector('#dials .dial-more');
  assert.equal(served, 2, 'script fetched again after the mismatch');
  assert.equal(navs, 2, 'exactly one automatic reload');
  await page.locator('button.q[data-q="help-rates"]').click();
  assert.equal(await page.locator('#help-rates').isVisible(), true, '? works after healing');
  assert.equal(await page.locator('#rates-summary .stat').count(), 4);
  // A mismatch that persists (e.g. a broken CDN) reloads once and then stops.
  served = -100;
  const p2 = await context.newPage(); let navs2 = 0; p2.on('framenavigated', (f) => { if (f === p2.mainFrame()) navs2++; });
  await context.unroute(/\/app\.js/);
  await context.route(/\/app\.js/, (r) => r.fulfill({ body: js.replace('__BUILD__', 'OLD'), contentType: 'text/javascript' }));
  await p2.evaluate(() => sessionStorage.clear()).catch(() => {});
  await p2.goto(base + Q);
  await p2.waitForTimeout(1500);
  assert.ok(navs2 <= 2, `navigated ${navs2} times`);
  assert.match(await p2.locator('#gist-summary').textContent(), /^In the US/, 'still renders after giving up')
  await context.close();
});

await check('a ticker without price data says so instead of showing a number', async () => {
  const { context, page } = await open();
  await page.fill('#q', 'microsoft');
  await page.waitForSelector('#results button');
  assert.match(await page.locator('#results button').first().textContent(), /no price data/);
  await page.keyboard.press('Enter');
  await page.waitForSelector('.card[data-tk="MSFT"]');
  assert.match(await page.locator('.card[data-tk="MSFT"]').textContent(), /No price data/);
  assert.equal(await page.locator('.card[data-tk="MSFT"] .px').count(), 0);
  await context.close();
});

if (!LIVE) await check('"since your last visit" shows a move past the alert level', async () => {
  const { context, page } = await open();
  const chips = await page.locator('#alerts .chip').allTextContents();
  assert.ok(chips.some((c) => /NVDA \+6\.8%/.test(c)), `chips: ${chips}`);
  await context.close();
});

await check('the watchlist never leaves the device: no request names a watched ticker', async () => {
  const { context, page, requests } = await open();
  await page.fill('#q', 'intel');
  await page.waitForSelector('#results button');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.card[data-tk="INTC"]');
  await page.waitForTimeout(500);
  const origin = new URL(base).origin;
  const ours = requests.filter((u) => u.startsWith(origin)).map((u) => new URL(u).pathname + new URL(u).search);
  const leaks = ours.filter((u) => /NVDA|AAPL|INTC|SGOV/i.test(u));
  assert.deepEqual(leaks, [], `requests naming tickers: ${leaks}`);
  const foreign = requests.filter((u) => !u.startsWith(origin) && !/fonts\.(googleapis|gstatic)\.com/.test(u) && !u.startsWith('data:'));
  assert.deepEqual(foreign, [], 'no third-party requests');
  await context.close();
});

await check('watchlist code moves the list to another browser', async () => {
  const a = await open();
  await a.page.fill('#q', 'intel'); await a.page.waitForSelector('#results button'); await a.page.keyboard.press('Enter');
  await a.page.waitForSelector('.card[data-tk="INTC"]');
  const code = await a.page.locator('#code').inputValue();
  assert.match(code, /^TR1\./);
  await a.context.close();
  const b = await open();
  assert.equal(await b.page.locator('.card[data-tk="INTC"]').count(), 0, 'fresh browser starts without INTC');
  await b.page.fill('#paste', code); await b.page.click('#import');
  await b.page.waitForSelector('.card[data-tk="INTC"]');
  assert.match(await b.page.locator('#move-msg').textContent(), /Loaded \d+ tickers/);
  await b.context.close();
});

await check('calendar feed is a valid iCalendar file', async () => {
  const r = await fetch(base + 'data/calendar.ics');
  assert.equal(r.status, 200);
  const t = await r.text();
  assert.match(t, /^BEGIN:VCALENDAR/);
  assert.match(t, /BEGIN:VEVENT[\s\S]*DTSTART;VALUE=DATE:\d{8}[\s\S]*END:VEVENT/);
});

await check('phone width: no horizontal scroll, search works', async () => {
  const { context, page, errors } = await open({ ...devices['Pixel 7'] });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 1, `page scrolls sideways by ${overflow}px`);
  await page.fill('#q', 'spy'); await page.waitForSelector('#results button');
  await page.screenshot({ path: `${OUT}/2-phone.png`, fullPage: false });
  assert.deepEqual(errors, []);
  await context.close();
});

await browser.close();
if (server) server.close();
console.log(`\n${results.length - failed}/${results.length} browser checks passed against ${base}`);
fs.writeFileSync(`${OUT}/e2e-results.txt`, results.join('\n') + '\n');
process.exit(failed ? 1 : 0);
