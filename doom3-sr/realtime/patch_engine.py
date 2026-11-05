#!/usr/bin/env python3
"""Apply the SmallSR experiment to a clean v1.1.0harmattan73 checkout."""
import argparse
from pathlib import Path
import shutil


def replace(path, before, after):
    source = path.read_text()
    assert source.count(before) == 1, (path, before)
    path.write_text(source.replace(before, after))


def patch(root, shaders):
    renderer = root/'doom3/neo/renderer'
    here = Path(__file__).resolve().parent
    shutil.copyfile(here/'small_sr.cpp', renderer/'rb/small_sr.cpp')
    shutil.copyfile(shaders/'small_sr_shaders.h', renderer/'rb/small_sr_shaders.h')
    replace(renderer/'tr_backend.cpp', '#include "tr_local.h"',
            '#include "tr_local.h"\n#include "rb/small_sr.cpp"')
    replace(renderer/'tr_backend.cpp', 'const void\tRB_SwapBuffers(const void *data)\n{',
            'const void\tRB_SwapBuffers(const void *data)\n{\n\tRB_SmallSR_Render();')
    replace(renderer/'tr_local.h', 'void RB_SetDefaultGLState(void);',
            'void RB_SetDefaultGLState(void);\nextern idCVar r_smallSR;\nvoid RB_SmallSR_Shutdown();')
    replace(renderer/'RenderSystem.cpp', 'if (r_screenFraction.GetInteger() != 100) {',
            'if (r_smallSR.GetInteger() != 0) {\n'
            '\t\tCropRenderSize(windowWidth/2, windowHeight/2, false, true);\n'
            '\t} else if (r_screenFraction.GetInteger() != 100) {')
    replace(renderer/'glsl/draw_glsl_shader.cpp', 'void R_GLSL_Shutdown(void)\n{',
            'void R_GLSL_Shutdown(void)\n{\n    RB_SmallSR_Shutdown();')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('engine', type=Path)
    p.add_argument('shaders', type=Path)
    a = p.parse_args()
    patch(a.engine, a.shaders)
