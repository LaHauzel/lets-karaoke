"""Realign only the suffix after a human-confirmed lyric line."""
import math
import subprocess
import tempfile
from pathlib import Path


def realign_suffix(job, lines, anchor_row, progress):
    from whisper_align import align_words, build_lines, DEFAULT_RULES
    from alignment_policy import resolve_rules, project_timeline
    from pipeline import KaraokeLine, KaraokeToken
    if isinstance(anchor_row, bool) or not isinstance(anchor_row, int) or not 0 <= anchor_row < len(lines)-1:
        raise ValueError('请选择末句之前的一句作为锚点')
    anchor = lines[anchor_row]
    start = anchor.end
    duration = float(job['media_info']['duration'])
    if not math.isfinite(start) or not 0 <= anchor.start < start < duration:
        raise ValueError('锚点起止时间必须位于音频范围内')
    source = Path(job.get('align_source') or '')
    if not source.is_file():
        source = Path(job['media'])
    if not source.is_file():
        raise ValueError('原始音频已移动或丢失，无法重新对齐')
    rows = [line.raw for line in lines[anchor_row+1:]]
    progress(0.08, f'锁定第 {anchor_row+1} 句，从 {start:.2f} 秒重新分析后续 {len(rows)} 句')
    with tempfile.TemporaryDirectory(prefix='karaoke_anchor_') as tmp:
        clip = Path(tmp)/'suffix.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(start), '-i', str(source),
                        '-vn', '-ar', '16000', '-ac', '1', str(clip)], check=True, capture_output=True)
        words = align_words(clip, '\n'.join(rows), language={'ja':'Japanese','zh':'Chinese','en':'English'}.get(job.get('lang'), job.get('lang') or 'Japanese'),
                            model_size='large-v3', progress=lambda f,m: progress(0.1+f*0.1,m))
    suffix, report = build_lines(words, rows, resolve_rules(DEFAULT_RULES, profile='automatic'))
    if report['row_index'] != list(range(len(rows))):
        raise ValueError('部分后续歌词未获得时间戳，已停止保存；请核对现场歌词')
    project_timeline(suffix)
    result = list(lines[:anchor_row+1])
    for line in suffix:
        tokens = [KaraokeToken(text=s.text, disp=s.text, start=s.start+start, end=s.end+start)
                  for s in line.segments]
        if any(t.end > duration+0.05 for t in tokens):
            raise ValueError('后续时间轴超出音频，已停止保存；请核对锚点和歌词')
        result.append(KaraokeLine(raw=line.text, tokens=tokens, start=tokens[0].start, end=tokens[-1].end))
    return result
