"""Realign lyric ranges bounded by human-confirmed lines."""
import math
import tempfile
from pathlib import Path
from process_runner import ProcessCancelled, run_process


def realign_suffix(job, lines, anchor_row, progress, cancel=None):
    """Re-align the lyric ranges after one or more fixed anchor lines.

    ``anchor_row`` accepts the old single integer form and a sorted list of
    rows for multi-anchor editing. Rows before the first anchor and all anchor
    rows keep their exact timings. Unanchored rows between anchors are aligned
    inside the audio interval between those anchors; the final suffix is
    aligned through the end of the source audio.
    """
    from whisper_align import align_words, build_lines, DEFAULT_RULES
    from alignment_policy import resolve_rules, project_timeline
    from pipeline import KaraokeLine, KaraokeToken

    callback = progress
    def progress(frac, message):
        if cancel and cancel():
            raise ProcessCancelled('用户已取消')
        callback(frac, message)
    config = job.get('alignment_config') or {}

    if isinstance(anchor_row, bool):
        raise ValueError('锚点行号无效')
    if isinstance(anchor_row, int):
        anchors = [anchor_row]
    elif isinstance(anchor_row, (list, tuple)):
        anchors = list(anchor_row)
    else:
        raise ValueError('锚点行号无效')
    if not anchors or any(isinstance(row, bool) or not isinstance(row, int) for row in anchors):
        raise ValueError('请选择有效的人工锚点')
    if anchors != sorted(set(anchors)):
        raise ValueError('锚点必须按歌词顺序选择，且不能重复')
    if anchors[0] < 0 or anchors[-1] >= len(lines) or anchors[0] >= len(lines)-1:
        raise ValueError('至少选择末句之前的一句作为锚点')

    duration = float(job['media_info']['duration'])
    for row in anchors:
        line = lines[row]
        if not all(math.isfinite(v) for v in (line.start, line.end)) or not 0 <= line.start < line.end < duration:
            raise ValueError(f'第 {row+1} 句锚点时间必须位于音频范围内')

    # Each range starts after an anchor and ends before the next anchor. The
    # final range continues from the last anchor to the end of the recording.
    ranges = []
    for i, row in enumerate(anchors):
        next_row = anchors[i + 1] if i + 1 < len(anchors) else len(lines)
        first_row = row + 1
        if first_row < next_row:
            range_end = lines[next_row].start if next_row < len(lines) else duration
            ranges.append((first_row, next_row, lines[row].end, range_end))
    if not ranges:
        raise ValueError('所选锚点之间没有待重新对齐的歌词')

    source = Path(job.get('align_source') or '')
    if not source.is_file():
        source = Path(job['media'])
    if not source.is_file():
        raise ValueError('原始音频已移动或丢失，无法重新对齐')

    for first_row, stop_row, start, end in ranges:
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
            raise ValueError('锚点之间必须留有有效音频区间，请检查锚点起止时间')

    result = list(lines)
    rules = resolve_rules(DEFAULT_RULES, profile='automatic')
    progress(0.08, f'已锁定 {len(anchors)} 句锚点，分 {len(ranges)} 段重新分析其间歌词')
    with tempfile.TemporaryDirectory(prefix='karaoke_anchor_') as tmp:
        for range_index, (first_row, stop_row, start, end) in enumerate(ranges):
            rows = [line.raw for line in lines[first_row:stop_row]]
            label = f'第 {first_row+1}–{stop_row} 句'
            base_progress = 0.08 + 0.14 * range_index / len(ranges)
            range_progress = 0.14 / len(ranges)
            progress(base_progress, f'重新对齐{label}（{start:.2f}–{end:.2f} 秒）')

            def align_clip(clip_end, text_rows, pass_name):
                clip = Path(tmp) / f'range_{range_index}_{pass_name}.wav'
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(start), '-i', str(source),
                                '-t', str(clip_end - start), '-vn', '-ar', '16000', '-ac', '1', str(clip)],
                            check=True, cancel=cancel, timeout=1800)
                words = align_words(
                    clip, '\n'.join(text_rows),
                    language={'ja': 'Japanese', 'zh': 'Chinese', 'en': 'English'}.get(
                        job.get('lang'), job.get('lang') or 'Japanese'),
                    model_size=config.get('model', 'large-v3'),
                    device=config.get('device', job.get('device', 'cuda')),
                    progress=lambda f, m: progress(base_progress + range_progress * f, m))
                suffix, report = build_lines(words, text_rows, rules)
                if report['row_index'] != list(range(len(text_rows))) or len(suffix) != len(text_rows):
                    raise ValueError(f'{label}中有歌词未获得有效时间戳，已停止保存；请核对现场歌词')
                project_timeline(suffix)
                return suffix

            def make_tokens(line):
                return [KaraokeToken(text=segment.text, disp=segment.text,
                                     start=segment.start + start, end=segment.end + start)
                        for segment in line.segments]

            suffix = align_clip(end, rows, 'bounded')
            candidate = [make_tokens(line) for line in suffix]
            if any(not tokens for tokens in candidate):
                raise ValueError(f'{label}中有歌词未获得有效时间戳，已停止保存；请核对现场歌词')
            outside = [token for tokens in candidate for token in tokens
                       if token.start < start - 0.05 or token.end > end + 0.05]

            # A forced aligner can place its final words just beyond a cropped
            # boundary. Retry from the same left-anchor end, with the next
            # anchored lyric as trailing context and a short audio tail. The
            # target rows still must fit strictly between the fixed anchors.
            if outside and stop_row < len(lines):
                next_anchor = lines[stop_row]
                context_end = min(duration, next_anchor.end + max(2.0, next_anchor.end - next_anchor.start))
                if context_end > end + 0.05:
                    progress(base_progress, f'{label}首次结果越过锚点范围，正从前一锚点后重试并参考下一锚点')
                    retry_rows = rows + [next_anchor.raw]
                    retried = align_clip(context_end, retry_rows, 'anchor_context')
                    suffix = retried[:len(rows)]
                    candidate = [make_tokens(line) for line in suffix]
                    if any(not tokens for tokens in candidate):
                        raise ValueError(f'{label}中有歌词未获得有效时间戳，已停止保存；请核对现场歌词')
                    outside = [token for tokens in candidate for token in tokens
                               if token.start < start - 0.05 or token.end > end + 0.05]

            # The final clip ends exactly at the media duration. Stable-ts can
            # estimate its last word a few frames beyond EOF; there is no next
            # anchor from which to add context. For tiny tail-only overruns,
            # proportionally fit this unanchored suffix from the fixed left
            # anchor to EOF. Anchor rows are not part of candidate and remain
            # byte-for-byte unchanged.
            if (outside and stop_row == len(lines) and end >= duration - 0.05
                    and not any(token.start < start - 0.05 for token in outside)):
                latest_end = max(token.end for tokens in candidate for token in tokens)
                tail_overrun = latest_end - end
                span = latest_end - start
                factor = (end - start) / span if span > 0 else 0.0
                if 0 < tail_overrun <= 0.25 and factor >= 0.98:
                    for tokens in candidate:
                        for token in tokens:
                            token.start = start + (token.start - start) * factor
                            token.end = start + (token.end - start) * factor
                    outside = [token for tokens in candidate for token in tokens
                               if token.start < start - 0.05 or token.end > end + 0.05]
                    progress(base_progress + range_progress,
                             f'{label}末尾仅超出媒体时长 {tail_overrun:.2f} 秒，已按比例收回；锚点保持不变')

            if outside:
                actual_start = min(token.start for token in outside)
                actual_end = max(token.end for token in outside)
                raise ValueError(
                    f'{label}重试后仍超出锚点区间 {start:.2f}–{end:.2f} 秒 '
                    f'（越界时间 {actual_start:.2f}–{actual_end:.2f} 秒），已停止保存；请检查锚点位置')

            for offset, (line, tokens) in enumerate(zip(suffix, candidate)):
                row = first_row + offset
                result[row] = KaraokeLine(raw=line.text, tokens=tokens,
                                          start=tokens[0].start, end=tokens[-1].end)
    return result
