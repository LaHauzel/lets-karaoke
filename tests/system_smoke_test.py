"""GPU smoke for supplied-lyrics and no-lyrics ASR routes.

Examples:
  python tests/system_smoke_test.py --lang zh
  python tests/system_smoke_test.py --lang en --asr
  python tests/system_smoke_test.py --lang ja --asr
"""
import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import webui

args = argparse.ArgumentParser()
args.add_argument('--lang', choices=('zh', 'en', 'ja'), default='zh')
args.add_argument('--asr', action='store_true')
args = args.parse_args()
asr_mode = args.asr
lang = args.lang
manifest = json.loads((ROOT / 'data/synth/manifest.json').read_text(encoding='utf-8'))
lang_data = manifest['langs'][lang]
variant = 'gapped_mix' if lang != 'ja' else 'legato_mix'
media = ROOT / lang_data['variants'][variant]['audio']
webui.OUT_ROOT = ROOT / 'out' / 'system_smoke'
job_id = 'asr_chained' if asr_mode else 'automatic'
job = webui.Job(job_id, webui.OUT_ROOT / job_id)
job.dir.mkdir(parents=True, exist_ok=True)
cfg = {
    'media': str(media),
    'lyrics_text': '\n'.join(lang_data['lyrics_lines']),
    'whisper_align': True, 'whisper_model': 'base',
    'vocal_guide': True, 'align_on_vocals': True, 'align_dual': True,
    'alignment_profile': 'balanced', 'lang': lang, 'device': 'cuda',
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
