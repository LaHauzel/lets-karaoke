'use strict';
const assert = require('node:assert/strict');
const LyricsFormat = require('../src/webui_assets/lyrics_format.js');

const tests = {
  enhanced_lyrics_strip_both_marker_formats_and_metadata() {
    const value = '[ti:demo]\n[offset:+200]\n[00:03.00]<00:03.00~00:04.00>Cloud <00:04.00~00:05.00>test\n[00:01.00]<00:01.00>Hello <00:02.00>world<00:02.50>';
    assert.deepEqual(LyricsFormat.previewLines(value), ['Hello world', 'Cloud test']);
    assert.equal(LyricsFormat.hasWordTimes(value), true);
  },
  repeated_line_clocks_and_long_minutes_match_lrc_parser() {
    assert.deepEqual(LyricsFormat.previewLines('[100:02:34][101:03.00]<100:02:34>再见'), ['再见', '再见']);
  },
  partial_word_timing_is_not_directly_renderable() {
    for (const value of ['', '[00:01]plain', '[00:01]<00:01>timed\n[00:02]plain', '<00:01>no line clock']) {
      assert.equal(LyricsFormat.hasWordTimes(value), false, value);
    }
  },
  srt_preview_preserves_text_and_skips_cue_numbers_and_clocks() {
    const value = '1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\nworld\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nCloud test';
    assert.deepEqual(LyricsFormat.previewLines(value), ['Hello\nworld', 'Cloud test']);
    assert.equal(LyricsFormat.hasWordTimes(value), false);
  },
  plain_text_and_line_timed_lrc_previews_omit_section_markers() {
    assert.deepEqual(LyricsFormat.previewLines('[Chorus]\nHello world\nCloud test'), ['Hello world', 'Cloud test']);
    assert.deepEqual(LyricsFormat.previewLines('[00:01]Hello\n[00:02][Chorus]\n[00:03]world'), ['Hello', 'world']);
  },
};
for (const [name, test] of Object.entries(tests)) { test(); process.stdout.write(`PASS ${name}\n`); }
