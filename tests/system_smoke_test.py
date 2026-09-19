"""GPU smoke: separation -> Whisper dual alignment -> subtitle -> encoded video."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import webui

asr_mode = '--asr' in sys.argv
webui.OUT_ROOT = ROOT / 'out' / 'system_smoke'
job_id = 'asr_chained' if asr_mode else 'automatic'
job = webui.Job(job_id, webui.OUT_ROOT / job_id)
job.dir.mkdir(parents=True, exist_ok=True)
cfg = {
    'media': str(ROOT / 'data/synth/zh/zh_gapped_mix.wav'),
    'lyrics_text': '\n'.join(json.loads((ROOT / 'data/synth/manifest.json').read_text(encoding='utf-8'))['langs']['zh']['lyrics_lines']),
    'whisper_align': True, 'whisper_model': 'base',
    'vocal_guide': True, 'align_on_vocals': True, 'align_dual': True,
    'alignment_profile': 'balanced', 'lang': 'zh', 'device': 'cuda',
    'vocal_mode': 'keep', 'separate': True, 'demucs': 'htdemucs',
    'encoder': 'auto',
}
if asr_mode:
    cfg.update(lyrics_text='', whisper_align=False, asr_lyrics=True)
webui._run_job(job, cfg)
report = {'state': job.state, 'error': job.error, 'logs': job.logs,
          'whisper_info': job.whisper_info, 'asr_info': job.asr_info, 'result': job.result}
(webui.OUT_ROOT / ('asr_report.json' if asr_mode else 'report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(job.state != 'done')
