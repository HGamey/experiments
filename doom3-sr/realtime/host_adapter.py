"""Compile the actual engine postprocess with only Doom's host interfaces stubbed."""
import ctypes
from pathlib import Path
import re
import subprocess
import tempfile


def build(shaders):
    here=Path(__file__).resolve().parent
    tmp=tempfile.TemporaryDirectory(prefix='doom-sr-native-')
    root=Path(tmp.name)
    names=sorted(set(re.findall(r'\bqgl\w+', (here/'small_sr.cpp').read_text())))
    code='''#include <GLES3/gl3.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdarg>
enum { CVAR_RENDERER=1,CVAR_INTEGER=2,CVAR_ARCHIVE=4 };
struct idCVar {
 int value;
 idCVar(const char*,const char *v,int,const char*) : value(atoi(v)) {}
 int GetInteger() const { return value; }
};
struct Common {
 void Printf(const char *s,...) { va_list a;va_start(a,s);vprintf(s,a);va_end(a); }
 void Error(const char *s,...) { va_list a;va_start(a,s);vfprintf(stderr,s,a);va_end(a);abort(); }
} commonObject;
Common *common=&commonObject;
struct { int vidWidth,vidHeight; } glConfig;
void GL_UseProgram(void*) { glUseProgram(0); }
'''
    code+='\n'.join('#define '+n+' '+n[1:] for n in names)
    code+='\n#include "small_sr.cpp"\n'
    code+='''extern "C" void sr_render(int w,int h,int mode) {
 glConfig.vidWidth=w;glConfig.vidHeight=h;r_smallSR.value=mode;RB_SmallSR_Render();
}
extern "C" void sr_shutdown() { RB_SmallSR_Shutdown(); }
'''
    (root/'adapter.cpp').write_text(code)
    subprocess.run(['g++','-std=c++11','-shared','-fPIC','-O2',str(root/'adapter.cpp'),
                    '-I'+str(here),'-I'+str(shaders),'-lGLESv2','-o',str(root/'adapter.so')],check=True)
    library=ctypes.CDLL(str(root/'adapter.so'))
    library.sr_render.argtypes=[ctypes.c_int]*3
    return tmp,library
