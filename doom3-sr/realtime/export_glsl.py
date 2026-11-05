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
// Pillow RGB8 bicubic: a=-0.5, half-pixel centers, 22-bit coefficients,
// horizontal RGB8 rounding/clipping followed by vertical RGB8 rounding/clipping.
float cubic(float v) {
    float x = abs(v);
    if(x < 1.0) return ((1.5*x-2.5)*x)*x+1.0;
    if(x < 2.0) return ((-0.5*x+2.5)*x-4.0)*x+2.0;
    return 0.0;
}
vec3 bicubic(ivec2 hr) {
    vec2 p = (vec2(hr)+0.5)*0.5-0.5;
    ivec2 origin = ivec2(floor(p));
    vec4 wx=vec4(0.0), wy=vec4(0.0);
    for(int i=0; i<4; ++i) {
        int x=origin.x+i-1, y=origin.y+i-1;
        if(x>=0 && x<uSize.x) wx[i]=cubic(p.x-float(x));
        if(y>=0 && y<uSize.y) wy[i]=cubic(p.y-float(y));
    }
    wx /= dot(wx,vec4(1.0));
    wy /= dot(wy,vec4(1.0));
    ivec4 cx=ivec4(sign(wx)*floor(abs(wx)*4194304.0+0.5));
    ivec4 cy=ivec4(sign(wy)*floor(abs(wy)*4194304.0+0.5));
    ivec3 total=ivec3(2097152);
    for(int y=0; y<4; ++y) {
        int sy=origin.y+y-1;
        if(sy<0 || sy>=uSize.y) continue;
        ivec3 row=ivec3(2097152);
        for(int x=0; x<4; ++x) {
            int sx=origin.x+x-1;
            if(sx<0 || sx>=uSize.x) continue;
            ivec3 rgb8=ivec3(floor(texelFetch(uSource,ivec2(sx,sy),0).rgb*255.0+0.5));
            row += rgb8*cx[x];
        }
        total += clamp(row >> 22,ivec3(0),ivec3(255))*cy[y];
    }
    return vec3(clamp(total >> 22,ivec3(0),ivec3(255)))/255.0;
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
        'bicubic':'Pillow RGB8: a=-0.5, half-pixel, 22-bit coefficients, separable rounding/clipping'}, indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('weights', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    export(a.weights, a.destination)
