#!/usr/bin/env python3
"""Validate and rebuild one NPZ into an installable arm64 package.

The engine checkout must already have patch_engine.py applied and build-sr
configured (build_engine.sh performs that first build).
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from export_glsl import export


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package(weights, engine, ndk, archive, purpose):
    here = Path(__file__).resolve().parent
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='small-sr-', dir=archive.parent) as work:
        stage = Path(work)
        shaders = stage/'shaders'
        export(weights, shaders)
        validation = subprocess.run([sys.executable, str(here/'validate_gles.py'),
                                     str(weights), str(shaders)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (stage/'validation.log').write_text(validation.stdout)
        print(validation.stdout, flush=True)
        validation.check_returncode()
        rb = engine/'doom3/neo/renderer/rb'
        shutil.copyfile(shaders/'small_sr_shaders.h', rb/'small_sr_shaders.h')
        shutil.copyfile(here/'small_sr.cpp', rb/'small_sr.cpp')
        run('cmake', '--build', engine/'build-sr', '--target', 'doom3', '-j8')
        shutil.copyfile(engine/'build-sr/doom3/neo/libdoom3.so', stage/'libdoom3.so')
        run(ndk/'toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip',
            '--strip-unneeded', stage/'libdoom3.so')
        shutil.copyfile(weights, stage/'weights.npz')
        shutil.copyfile(shaders/'model.json', stage/'model.json')
        shutil.copyfile(engine/'LICENSE', stage/'COPYING')
        shutil.copyfile(here/'README.md', stage/'README.md')
        for cfg in here.glob('sr_*.cfg'):
            shutil.copyfile(cfg, stage/cfg.name)
        source = stage/'source'
        source.mkdir()
        for cfg in here.glob('sr_*.cfg'):
            shutil.copyfile(cfg, source/cfg.name)
        for name in ['small_sr.cpp', 'patch_engine.py', 'export_glsl.py',
                     'build_engine.sh', 'package_model.py', 'validate_gles.py', 'host_adapter.py']:
            shutil.copyfile(here/name, source/name)
        metadata = {
            'purpose': purpose,
            'upstream': 'https://github.com/glKarin/com.n0n3m4.diii4a',
            'upstream_commit': run('git', '-C', engine, 'rev-parse', 'HEAD',
                                   capture_output=True, text=True).stdout.strip(),
            'implementation_commit': run('git', '-C', here, 'rev-parse', 'HEAD',
                                         capture_output=True, text=True).stdout.strip(),
            'abi': 'arm64-v8a', 'min_api': 24,
            'ndk': (ndk/'source.properties').read_text().strip(),
            'files': {str(p.relative_to(stage)): {'sha256': digest(p), 'bytes': p.stat().st_size}
                      for p in sorted(stage.rglob('*')) if p.is_file()},
        }
        (stage/'build-info.json').write_text(json.dumps(metadata, indent=2)+'\n')
        with tarfile.open(archive, 'w:gz') as tar:
            for p in sorted(stage.iterdir()):
                tar.add(p, arcname=p.name)
        metadata['archive_sha256'] = digest(archive)
        metadata['archive_bytes'] = archive.stat().st_size
        archive.with_suffix('').with_suffix('.json').write_text(json.dumps(metadata, indent=2)+'\n')
        print(json.dumps({'archive': str(archive), 'sha256': metadata['archive_sha256'],
                          'bytes': metadata['archive_bytes'],
                          'library': metadata['files']['libdoom3.so'],
                          'weights': metadata['files']['weights.npz']}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('weights', type=Path)
    p.add_argument('--engine', type=Path, required=True, help='already patched/configured v73 checkout')
    p.add_argument('--ndk', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='output .tar.gz file')
    p.add_argument('--purpose', default='SmallSR model; phone acceptance pending')
    a = p.parse_args()
    package(a.weights.resolve(), a.engine.resolve(), a.ndk.resolve(), a.output.resolve(), a.purpose)
