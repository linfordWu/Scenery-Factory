# SPDX-License-Identifier: GPL-3.0-only
"""Install into an existing isolated ComfyUI. Never download model weights."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
NAME = 'ComfyUI-MiniMax-H3-W4A4-VSA'
MODEL = 'minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors'
GATE = 'fasth3_vsa_gate.safetensors'
DEPENDENCIES = {
    'ComfyUI-KJNodes': 'https://github.com/kijai/ComfyUI-KJNodes.git',
    'ComfyUI-MiniMax-H3-MotionCache-FastVAE':
        'https://github.com/Mozer/ComfyUI-MiniMax-H3-MotionCache-FastVAE.git',
}
FILES = ('__init__.py', 'nodes.py', 'cache.py', 'streaming.py', 'convert.py',
         'setup_env.py', 'README.md', 'LICENSE',
         'THIRD_PARTY_NOTICES.md', 'VALIDATION.md', 'compatibility.json', '.gitignore',
         'adaptive.py', 'av_adaptive.py', 'block_adaptive.py', 'layer_adaptive.py',
         'jev_client.py', 'laya_client.py', 'openjev_client.py', 'test_adaptive.py', 'test_laya_client.py',
         'test_openjev_client.py',
         'test_sdk_transport.py', 'JEV_ADAPTIVE.md', 'JEV_ADAPTIVE.en.md',
         'requirements-jev.txt', 'requirements-laya.txt', 'LAYA.md',
         'native_sla.py', 'native_sla_worker.py', 'test_native_sla.py',
         '009JEV.md', '009JEV.en.md')


def distribution_files():
    files = [Path(n) for n in FILES]
    for directory, pattern in [('workflows', '*.json'), ('examples', '*.json'), ('docs', '*'), ('skills', '*')]:
        files.extend(p.relative_to(HERE) for p in (HERE / directory).rglob(pattern) if p.is_file())
    return files


def run(args, cwd=None):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def link_or_copy(source, target):
    # Same-volume hard links reuse large local weights without consuming disk twice.
    if target.exists():
        if os.path.samefile(source, target):
            return
        raise RuntimeError(f'Will not overwrite existing file: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', required=True, type=Path)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--gate', type=Path)
    parser.add_argument('--cache-source', type=Path)
    parser.add_argument('--gpu', default='0')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--install-dependencies', action='store_true')
    parser.add_argument('--update', action='store_true')
    args = parser.parse_args()
    root = args.comfy_root.resolve()
    for name in ('model', 'gate', 'cache_source'):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    executable = Path(sys.executable).resolve()
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Refusing global Python. Select the ComfyUI venv Python (e.g. .venv/bin/python).')
    if not (root / 'comfy/sd.py').is_file():
        parser.error('--comfy-root must contain comfy/sd.py')
    os.chdir(root)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(root))
    import folder_paths
    from utils.extra_config import load_extra_path_config
    config = root / 'extra_model_paths.yaml'
    if config.is_file():
        load_extra_path_config(str(config))

    def find(category, name, explicit=None):
        if explicit:
            path = explicit.resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            return path
        matches = [Path(folder_paths.get_full_path_or_raise(category, n))
                   for n in folder_paths.get_filename_list(category)
                   if Path(n).name == name]
        matches = list(dict.fromkeys(p.resolve() for p in matches))
        if len(matches) != 1:
            raise RuntimeError(f'Expected one local {name}, found {len(matches)}. '
                               'Use --model / --gate, or configure ComfyUI extra_model_paths.yaml. '
                               'No model will be downloaded.')
        return matches[0]

    model = find('diffusion_models', MODEL, args.model)
    gate = find('loras', GATE, args.gate)
    for category, name in (
        ('text_encoders', 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'),
        ('vae', 'minimax_h3_video_vae_int8_convrot.safetensors'),
        ('vae', 'minimax_h3_audio_vae_fp32.safetensors'),
    ):
        print(f'{category}: {find(category, name)}', flush=True)
    print(f'Python: {executable}\nComfyUI: {root}\nModel: {model}\nGate: {gate}', flush=True)
    convert = [executable, '-B', '-X', 'utf8', HERE / 'convert.py', '--comfy-root', root, '--gpu', args.gpu]
    run([*convert, '--check'])
    target = root / 'custom_nodes' / NAME
    missing = [name for name in DEPENDENCIES if not (root / 'custom_nodes' / name / '__init__.py').is_file()]
    if missing and (not args.install_dependencies or args.check_only):
        raise RuntimeError(f'Missing custom nodes: {missing}. Install them, or rerun with --install-dependencies.')
    files = distribution_files()
    if target.resolve() != HERE and target.exists() and not args.update and not args.check_only:
        conflicts = [str(p) for p in files if (target / p).exists()
                     and (target / p).read_bytes() != (HERE / p).read_bytes()]
        if conflicts:
            raise RuntimeError(f'Installed files differ: {conflicts}. Review them, then use --update to replace these files only.')
    cache = root / 'models/h3_preconverted/fc1_gate'
    reuse = cache if cache.exists() else args.cache_source
    if reuse:
        reuse = reuse.resolve()
        run([*convert, '--verify', reuse, '--model', model, '--gate', gate])
    elif args.check_only:
        print('No converted cache yet. Setup will convert 50 FC1 blocks and the gate once.', flush=True)
    if args.check_only:
        print('CHECK PASS. No files written.', flush=True)
        return
    for name in missing:
        destination = root / 'custom_nodes' / name
        if destination.exists():
            raise RuntimeError(f'Incomplete dependency directory; inspect it first: {destination}')
        run(['git', 'clone', '--depth', '1', DEPENDENCIES[name], destination])
        requirements = destination / 'requirements.txt'
        if requirements.is_file():
            run([executable, '-m', 'pip', '--isolated', 'install', '--no-cache-dir', '-r', requirements])
    if target.resolve() != HERE:
        for relative in files:
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists() or dest.read_bytes() != (HERE / relative).read_bytes():
                shutil.copy2(HERE / relative, dest)
    # Explicit sources outside configured search paths become visible to ComfyUI.
    registered = [Path(p).resolve() for p in folder_paths.get_folder_paths('diffusion_models')]
    if not any(model.is_relative_to(p) for p in registered):
        link_or_copy(model, root / 'models/diffusion_models' / model.name)
    if reuse and reuse != cache.resolve():
        import json
        manifest = json.loads((reuse / 'manifest.json').read_text(encoding='utf-8'))
        for row in manifest['fc1'] + [manifest['gate']]:
            link_or_copy(reuse / row['file'], cache / row['file'])
        link_or_copy(reuse / 'manifest.json', cache / 'manifest.json')
    elif not reuse:
        run([*convert, '--model', model, '--gate', gate, '--output', cache])
    print(f'SETUP COMPLETE: {target}\nCache: {cache}\nRestart ComfyUI and open workflows/H3_Streaming_v2.json. '
          'Select your reference images and model names if stored in subfolders.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Setup stopped: {exc}', file=sys.stderr)
        raise SystemExit(1)
