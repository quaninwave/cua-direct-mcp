// Meta Ad Library results page -> one record per ad card that has a <video>.
// Run:  cua.py js --file scripts/extract_meta_cards.js > cards.json
// (set CUA_MAX_OUTPUT_CHARS=8000000 for hundreds of cards). Needs a current tab on the results page.
// Works with Vietnamese ("ID thư viện") and English ("Library ID") interface labels.
const t = await getTab(A.tab_id);
const rows = await t.playwright.evaluate(() => {
  const ID = /(?:ID thư viện|Library ID):? (\d+)/;
  const ID_ALL = /(?:ID thư viện|Library ID):/g;
  const out = [];
  const seen = {};
  for (const v of document.querySelectorAll("video")) {
    let el = v;
    let card = null;
    // climb until the ancestor contains exactly one library id: that is the card
    for (let i = 0; i < 16 && el; i++) {
      el = el.parentElement;
      if (!el) break;
      const tx = el.innerText || "";
      if (ID.test(tx) && (tx.match(ID_ALL) || []).length === 1) { card = tx; break; }
    }
    if (!card) continue;
    const id = card.match(ID)[1];
    if (seen[id]) continue;
    seen[id] = 1;
    out.push({ id, text: card.replace(/\n+/g, " | ").slice(0, 700), src: v.getAttribute("src") || null });
  }
  return out;
});
out(rows);
