/* Display text and word-timed input checks shared by lyric preview and submission. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LyricsFormat = api;
})(typeof window === 'object' ? window : globalThis, function () {
  const LINE_TIME = /\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]/g;
  const WORD_TIME = /<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?(?:~\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?)?>/g;
  const SRT_TIME = /\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}/;
  const META = /^\s*\[(?:ti|ar|al|by|offset|re|ve|length|kana)\s*:/i;
  const SECTION = /^\s*[\[(【（].{1,24}?[\])】）]\s*$/;
  const display = text => SECTION.test(text) ? '' : text.trim();

  function rows(value) {
    const raw = String(value || '').replace(/\r\n?/g, '\n');
    if (SRT_TIME.test(raw)) {
      return raw.trim().split(/\n\s*\n/).flatMap(block => {
        const lines = block.split('\n').filter(line => line.trim());
        const clock = lines.findIndex(line => SRT_TIME.test(line));
        const text = clock < 0 ? '' : display(lines.slice(clock + 1).join('\n'));
        return text ? [{text, wordTimed: false}] : [];
      });
    }
    if ([...raw.matchAll(LINE_TIME)].length) {
      return raw.split('\n').flatMap(line => {
        if (META.test(line)) return [];
        const clocks = [...line.matchAll(LINE_TIME)];
        if (!clocks.length) return [];
        const body = line.replace(LINE_TIME, '').trim();
        const markers = [...body.matchAll(WORD_TIME)];
        const text = display(body.replace(WORD_TIME, ''));
        if (!text) return [];
        return clocks.map(clock => ({text, wordTimed: markers.length > 0,
          start: Number(clock[1]) * 60 + Number(clock[2].replace(':', '.'))}));
      }).sort((a, b) => a.start - b.start);
    }
    return raw.split('\n').map(display).filter(Boolean).map(text => ({text, wordTimed: false}));
  }
  function previewLines(value) { return rows(value).map(row => row.text); }
  function hasWordTimes(value) {
    const parsed = rows(value);
    return parsed.length > 0 && parsed.every(row => row.wordTimed);
  }
  return {previewLines, hasWordTimes};
});
