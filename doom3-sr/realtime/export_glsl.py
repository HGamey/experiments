#!/usr/bin/env python3
"""Export SmallSR's NHWC weights as three GLES 3 fragment programs.

The game framebuffer is bottom-up; the network's kernels and pixel shuffle
are top-down. Flip kernel Y and the vertical shuffle phase, not the image.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

VERTEX = '''#version 300 es
precision highp float;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
'''
PREFIX = '''#version 300 es
precision highp float;
precision highp int;
uniform ivec2 uSize;
'''


def number(x):
    s = format(float(x), '.9g')
    return s if ('.' in s or 'e' in s) else s + '.0'


def vec(x):
    return f'vec{len(x)}(' + ','.join(map(number, x)) + ')'


def source_fetch():
    return '''uniform highp sampler2D uSource;
vec3 rgb(ivec2 p) {
    if(any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, uSize))) return vec3(0.0);
    return texelFetch(uSource, p, 0).rgb;
}
'''


def feature_fetch():
    s = ''
    for i in range(4):
        s += f'''uniform highp sampler2D uFeature{i};
vec4 f{i}(ivec2 p) {{
    if(any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, uSize))) return vec4(0.0);
    return texelFetch(uFeature{i}, p, 0);
}}
'''
    return s


def hidden(w, b, first):
    lines = [PREFIX, source_fetch() if first else feature_fetch()]
    lines += [f'layout(location={g}) out vec4 o{g};' for g in range(4)]
    lines += ['void main() {', 'ivec2 p = ivec2(gl_FragCoord.xy);']
    lines += [f'vec4 a{g} = {vec(b[g*4:g*4+4])};' for g in range(4)]
    k = w.shape[0]
    for y in range(k):
        for x in range(k):
            offset = f'p + ivec2({x-k//2},{k//2-y})'
            if first:
                lines.append(f'{{ vec3 v = rgb({offset});')
                for g in range(4):
                    for c in range(3):
                        lines.append(f'a{g} += v[{c}] * {vec(w[y,x,c,g*4:g*4+4])};')
                lines.append('}')
            else:
                for ig in range(4):
                    lines.append(f'{{ vec4 v = f{ig}({offset});')
                    for og in range(4):
                        columns = ','.join(vec(w[y,x,ig*4+c,og*4:og*4+4]) for c in range(4))
                        lines.append(f'a{og} += mat4({columns}) * v;')
                    lines.append('}')
    lines += [f'o{g} = max(a{g}, vec4(0.0));' for g in range(4)]
    lines += ['}']
    return '\n'.join(lines) + '\n'


BICUBIC = '''
// Catmull-Rom (a=-0.5), half-pixel centers, normalized clipped edge support.
float cubic(float v) {
    float x = abs(v);
    if(x < 1.0) return ((1.5*x-2.5)*x)*x+1.0;
    if(x < 2.0) return ((-0.5*x+2.5)*x-4.0)*x+2.0;
    return 0.0;
}
vec3 bicubic(ivec2 hr) {
    vec2 p = (vec2(hr)+0.5)*0.5-0.5;
    ivec2 origin = ivec2(floor(p));
    vec3 value = vec3(0.0);
    float total = 0.0;
    for(int y=-1; y<=2; ++y) for(int x=-1; x<=2; ++x) {
        ivec2 q = origin+ivec2(x,y);
        if(any(lessThan(q,ivec2(0))) || any(greaterThanEqual(q,uSize))) continue;
        float a = cubic(p.x-float(q.x))*cubic(p.y-float(q.y));
        value += texelFetch(uSource,q,0).rgb*a;
        total += a;
    }
    return clamp(value/total, 0.0, 1.0);
}
'''


def output(w, b):
    lines = [PREFIX, source_fetch(), feature_fetch(), BICUBIC,
             'uniform int uModel;', 'layout(location=0) out vec4 color;']
    for c in range(3):
        values = [vec(w[y,x,ig*4:ig*4+4,phase*3+c])
                  for phase in range(4) for y in range(3) for x in range(3) for ig in range(4)]
        lines.append(f'const vec4 w{c}[144] = vec4[144](\n' + ',\n'.join(values) + ');')
    lines.append('const vec3 bias[4] = vec3[4](' + ','.join(vec(b[i*3:i*3+3]) for i in range(4)) + ');')
    lines += ['void main() {', 'ivec2 hr = ivec2(gl_FragCoord.xy);',
              'vec3 v = bicubic(hr);', 'if(uModel != 0) {',
              'ivec2 p = hr / 2;', 'int phase = (1-(hr.y & 1))*2+(hr.x & 1);',
              'vec3 r = bias[phase];']
    for y in range(3):
        for x in range(3):
            for ig in range(4):
                i = (y*3+x)*4+ig
                lines += [f'{{ vec4 f = f{ig}(p+ivec2({x-1},{1-y}));',
                          f'int i = phase*36+{i};',
                          'r += vec3(dot(f,w0[i]),dot(f,w1[i]),dot(f,w2[i])); }']
    lines += ['v += r;', '}', 'color = vec4(clamp(v,0.0,1.0),1.0);', '}']
    return '\n'.join(lines) + '\n'


def export(weights, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with np.load(weights, allow_pickle=False) as p:
        programs = {'sr_vertex': VERTEX,
                    'sr_conv1': hidden(p['w1'], p['b1'], True),
                    'sr_conv2': hidden(p['w2'], p['b2'], False),
                    'sr_output': output(p['w3'], p['b3'])}
        shapes = {k:list(p[k].shape) for k in p.files}
        count = sum(p[k].size for k in p.files)
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    header = f'// Generated from weights SHA256 {digest}\n'
    header += f'static const char *sr_weight_sha256 = "{digest}";\n'
    for name, text in programs.items():
        ext = 'vert' if name == 'sr_vertex' else 'frag'
        (destination/f'{name}.{ext}').write_text(text)
        header += f'static const char *{name} =\n' + '\n'.join(json.dumps(line) for line in text.splitlines(keepends=True)) + ';\n'
    (destination/'small_sr_shaders.h').write_text(header)
    (destination/'model.json').write_text(json.dumps({'sha256':digest, 'parameters':int(count),
        'shapes':shapes, 'layout':'NHWC', 'scale':2, 'features':'RGBA16F',
        'bicubic':'Catmull-Rom a=-0.5, half-pixel, clipped normalized support; continuous float'}, indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('weights', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    export(a.weights, a.destination)
