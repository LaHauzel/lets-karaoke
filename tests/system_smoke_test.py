"""GPU smoke for supplied-lyrics and no-lyrics ASR routes.

Examples:
  python tests/system_smoke_test.py --lang zh
  python tests/system_smoke_test.py --lang en --asr
  python tests/system_smoke_test.py --lang ja --asr
  python tests/system_smoke_test.py --lang ja --sofa
  python tests/system_smoke_test.py --lang ja --media song.ogg --lyrics lyrics.txt
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
args.add_argument('--sofa', action='store_true')
args.add_argument('--media', type=Path, help='optional external media file')
args.add_argument('--lyrics', type=Path, help='optional known-lyrics text file')
args = args.parse_args()
asr_mode = args.asr
if asr_mode and args.sofa:
    raise SystemExit('--asr and --sofa cannot be combined')
if asr_mode and args.lyrics:
    raise SystemExit('--lyrics cannot be combined with --asr')
lang = args.lang
manifest = json.loads((ROOT / 'data/synth/manifest.json').read_text(encoding='utf-8'))
lang_data = manifest['langs'][lang]
variant = 'gapped_mix' if lang != 'ja' else 'legato_mix'
media = args.media.resolve() if args.media else ROOT / lang_data['variants'][variant]['audio']
if not media.exists():
    raise SystemExit(f'media file not found: {media}')
lyrics_text = (args.lyrics.read_text(encoding='utf-8') if args.lyrics
               else '\n'.join(lang_data['lyrics_lines']))
webui.OUT_ROOT = ROOT / 'out' / 'system_smoke'
job_prefix = 'asr' if asr_mode else 'automatic'
job_id = f'{job_prefix}_{lang}_{media.stem}'
job = webui.Job(job_id, webui.OUT_ROOT / job_id)
job.dir.mkdir(parents=True, exist_ok=True)
cfg = {
    'media': str(media),
    'lyrics_text': lyrics_text,
    'whisper_align': not args.sofa, 'sofa_align': args.sofa,
    'whisper_model': 'base',
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
(job.dir / ('asr_report.json' if asr_mode else 'report.json')).write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(job.state != 'done')
