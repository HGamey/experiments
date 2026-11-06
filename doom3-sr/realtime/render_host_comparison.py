#!/usr/bin/env python3
"""Render three SmallSR modes using the C++ and GLSL from a released package.

This is a host postprocess comparison. Its low-resolution input is downsampled
from a captured image; it does not render the Doom scene or measure phone FPS.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sheet(images, labels, title, footer, path):
    w, h = images[0].size
    gap, top, label_h, bottom = 12, 42, 32, 42
    canvas = Image.new('RGB', (w*3+gap*4, top+label_h+h+bottom), '#15191f')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
    draw.text((gap, 12), title, font=font, fill='#f5f5f5')
    for i, (im, label) in enumerate(zip(images, labels)):
        x = gap+(w+gap)*i
        draw.text((x, top+6), label, font=font, fill='#f5f5f5')
        canvas.paste(im, (x, top+label_h))
    draw.text((gap, top+label_h+h+12), footer, font=small, fill='#c6c9ce')
    canvas.save(path)


def main(archive, source, out, crop, zoom):
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out/'source-contact.jpg')
    hr = Image.open(source).convert('RGB').crop(crop)
    width, height = hr.size
    lr = hr.resize((width//2, height//2), Image.Resampling.BICUBIC)
    hr.save(out/'source-hr-crop.png')
    lr.save(out/'input-half.png')
    report = {'archive_sha256': sha(archive), 'source_sha256': sha(source),
              'source': 'C trial 3aa25dd077c5/test-crop-contact.jpg, first row HR, frame_0124',
              'source_crop_xyxy': crop, 'hr_size': hr.size, 'lr_size': lr.size,
              'zoom_xyxy': zoom, 'zoom_scale': 4, 'zoom_filter': 'nearest',
              'input_method': 'Pillow RGB8 bicubic downsample of the JPEG HR crop',
              'scope': 'Host postprocess only; not a live game render or phone FPS measurement'}
    with tempfile.TemporaryDirectory(prefix='doom-sr-release-') as work:
        stage = Path(work)
        with tarfile.open(archive) as tar:
            tar.extractall(stage, filter='data')
        # Import the actual, archived implementation and its host adapter.
        sys.path.insert(0, str(stage/'source'))
        gpu = importlib.import_module('validate_gles')
        adapter = importlib.import_module('host_adapter')
        GL, EGL = gpu.GL, gpu.EGL
        ctx = gpu.context(width, height)
        report['renderer'] = GL.glGetString(GL.GL_RENDERER).decode()
        report['gles_version'] = GL.glGetString(GL.GL_VERSION).decode()
        report['weights_sha256'] = sha(stage/'weights.npz')
        report['engine_source_sha256'] = sha(stage/'source/small_sr.cpp')
        tmp, runner = adapter.build(stage/'shaders')
        GL.glDisable(GL.GL_DITHER)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_DEPTH_TEST)
        images = []
        for mode, name in [(0, 'native'), (1, 'bicubic'), (2, 'model')]:
            im = hr if mode == 0 else lr
            w, h = im.size
            rgba = np.asarray(im.convert('RGBA'))
            texture = gpu.texture(w, h, GL.GL_RGBA8, np.ascontiguousarray(rgba[::-1]))
            fbo = gpu.target([texture])
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, fbo)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, 0)
            GL.glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
            GL.glViewport(0, 0, w, h)
            runner.sr_render(width, height, mode)
            GL.glReadBuffer(GL.GL_BACK)
            raw = np.empty((height, width, 4), np.uint8)
            GL.glReadPixels(0, 0, width, height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, raw)
            image = Image.fromarray(raw[::-1, :, :3].copy())
            image.save(out/f'mode-{mode}-{name}.png')
            images.append(image)
            GL.glDeleteFramebuffers(1, [fbo])
            GL.glDeleteTextures(1, [texture])
        assert np.array_equal(np.asarray(images[0]), np.asarray(hr))
        base = lr.resize(hr.size, Image.Resampling.BICUBIC)
        assert np.array_equal(np.asarray(images[1]), np.asarray(base))
        with np.load(stage/'weights.npz') as weights:
            x = np.asarray(lr).astype(np.float32)/255
            a = np.maximum(gpu.conv(x, weights['w1'], weights['b1']), 0)
            b = np.maximum(gpu.conv(a, weights['w2'], weights['b2']), 0)
            r = gpu.conv(b, weights['w3'], weights['b3'])
            r = r.reshape(height//2, width//2, 2, 2, 3).transpose(0, 2, 1, 3, 4).reshape(height, width, 3)
            expected = np.clip(np.asarray(base).astype(np.float32)/255+r, 0, 1)
        error = np.abs(np.asarray(images[2]).astype(np.float32)/255-expected)
        assert error.max() < 2/255
        report['checks'] = {'native_equals_source': True, 'bicubic_equals_pillow': True,
                            'model_max_error_vs_numpy': float(error.max()),
                            'model_mean_error_vs_numpy': float(error.mean())}
        report['crop_psnr_db'] = {}
        for name, image in zip(['bicubic', 'model'], images[1:]):
            mse = np.mean((np.asarray(image).astype(np.float64)-np.asarray(hr))**2)
            report['crop_psnr_db'][name] = float(10*np.log10(255**2/mse))
        runner.sr_shutdown()
        tmp.cleanup()
        EGL.eglMakeCurrent(ctx[0], EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
        EGL.eglDestroySurface(ctx[0], ctx[1])
        EGL.eglDestroyContext(ctx[0], ctx[2])
        EGL.eglTerminate(ctx[0])
    labels = ['0 | Native reference', '1 | Half + bicubic', '2 | Half + model']
    sheet(images, labels, 'frame_0124 | HR crop | Host GLES',
          'NPZ 6cc44b53 | Half input: downsampled HR crop | No phone FPS claim', out/'three-modes.png')
    zoomed = [im.crop(zoom).resize(((zoom[2]-zoom[0])*4, (zoom[3]-zoom[1])*4), Image.Resampling.NEAREST)
              for im in images]
    sheet(zoomed, labels, 'frame_0124 | Same crop, 4x nearest enlargement | Host GLES',
          'NPZ 6cc44b53 | No sharpening or colour changes | JPEG source crop; not an in-game phone capture', out/'zoom-three-modes.png')
    report['files'] = {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file()}
    (out/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive', type=Path)
    p.add_argument('source_contact', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--crop', nargs=4, type=int, default=[512, 24, 768, 280])
    p.add_argument('--zoom', nargs=4, type=int, default=[72, 40, 200, 168])
    a = p.parse_args()
    main(a.archive, a.source_contact, a.output, a.crop, a.zoom)
