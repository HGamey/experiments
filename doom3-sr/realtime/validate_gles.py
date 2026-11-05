#!/usr/bin/env python3
"""Run exported GLSL on a real EGL/GLES context and compare with NumPy.

Host validation, not a phone performance claim. Requires numpy and PyOpenGL.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import time

os.environ.setdefault('PYOPENGL_PLATFORM', 'egl')
os.environ.setdefault('EGL_PLATFORM', 'surfaceless')
import numpy as np
from OpenGL import EGL, GL
from OpenGL.GL.shaders import compileShader, compileProgram


def context(w, h):
    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    major, minor = EGL.EGLint(), EGL.EGLint()
    EGL.eglInitialize(display, major, minor)
    attrs = (EGL.EGLint * 13)(EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
        EGL.EGL_RENDERABLE_TYPE, 0x40, EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8,
        EGL.EGL_BLUE_SIZE, 8, EGL.EGL_ALPHA_SIZE, 8, EGL.EGL_NONE)
    config, count = EGL.EGLConfig(), EGL.EGLint()
    EGL.eglChooseConfig(display, attrs, ctypes.byref(config), 1, count)
    EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
    ctx = EGL.eglCreateContext(display, config, EGL.EGL_NO_CONTEXT,
        (EGL.EGLint * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION, 3, EGL.EGL_NONE))
    surface = EGL.eglCreatePbufferSurface(display, config,
        (EGL.EGLint * 5)(EGL.EGL_WIDTH,w,EGL.EGL_HEIGHT,h,EGL.EGL_NONE))
    EGL.eglMakeCurrent(display,surface,surface,ctx)
    return display, surface, ctx


def texture(w, h, format=GL.GL_RGBA16F, data=None):
    t = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D,t)
    for key,value in [(GL.GL_TEXTURE_MIN_FILTER,GL.GL_NEAREST),
                      (GL.GL_TEXTURE_MAG_FILTER,GL.GL_NEAREST),
                      (GL.GL_TEXTURE_WRAP_S,GL.GL_CLAMP_TO_EDGE),
                      (GL.GL_TEXTURE_WRAP_T,GL.GL_CLAMP_TO_EDGE)]:
        GL.glTexParameteri(GL.GL_TEXTURE_2D,key,value)
    GL.glTexImage2D(GL.GL_TEXTURE_2D,0,format,w,h,0,GL.GL_RGBA,
        GL.GL_UNSIGNED_BYTE if format==GL.GL_RGBA8 else GL.GL_FLOAT,data)
    return t


def target(textures):
    f = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER,f)
    for i,t in enumerate(textures):
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER,GL.GL_COLOR_ATTACHMENT0+i,GL.GL_TEXTURE_2D,t,0)
    GL.glDrawBuffers(len(textures),[GL.GL_COLOR_ATTACHMENT0+i for i in range(len(textures))])
    assert GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)==GL.GL_FRAMEBUFFER_COMPLETE
    return f


def read(w,h,attachment=0):
    GL.glReadBuffer(GL.GL_COLOR_ATTACHMENT0+attachment)
    a = np.empty((h,w,4),np.float32)
    GL.glReadPixels(0,0,w,h,GL.GL_RGBA,GL.GL_FLOAT,a)
    return a[::-1].copy()


def conv(x,w,b):
    h,width,_=x.shape
    k=w.shape[0]
    p=np.pad(x,((k//2,k//2),(k//2,k//2),(0,0)))
    out=np.broadcast_to(b,(h,width,len(b))).copy()
    for y in range(k):
        for xx in range(k):
            out += p[y:y+h,xx:xx+width,:]@w[y,xx]
    return out


def bicubic(x):
    from PIL import Image
    h,w,_=x.shape
    rgb=np.rint(x*255).astype(np.uint8)
    return np.asarray(Image.fromarray(rgb).resize((w*2,h*2),Image.Resampling.BICUBIC)).astype(np.float32)/255


def main(weights,shaders,w=31,h=19):
    ctx=context(w*2,h*2)
    report={'renderer':GL.glGetString(GL.GL_RENDERER).decode(),
            'version':GL.glGetString(GL.GL_VERSION).decode(),'size':[w,h]}
    programs=[]
    for name in ['sr_conv1','sr_conv2','sr_output']:
        programs.append(compileProgram(compileShader((shaders/'sr_vertex.vert').read_text(),GL.GL_VERTEX_SHADER),
            compileShader((shaders/(name+'.frag')).read_text(),GL.GL_FRAGMENT_SHADER)))
    rgba=np.random.default_rng(23).integers(0,256,(h,w,4),dtype=np.uint8)
    rgba[:,:,3]=255
    source=texture(w,h,GL.GL_RGBA8,np.ascontiguousarray(rgba[::-1]))
    features=[texture(w,h) for _ in range(8)]
    fbos=[target(features[:4]),target(features[4:]),target([texture(w*2,h*2,GL.GL_RGBA32F)])]
    GL.glDisable(GL.GL_DITHER)
    GL.glDisable(GL.GL_BLEND)
    GL.glDisable(GL.GL_DEPTH_TEST)

    def draw(pass_id,model=1):
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER,fbos[pass_id])
        GL.glViewport(0,0,w*(2 if pass_id==2 else 1),h*(2 if pass_id==2 else 1))
        p=programs[pass_id];GL.glUseProgram(p)
        GL.glUniform2i(GL.glGetUniformLocation(p,'uSize'),w,h)
        GL.glUniform1i(GL.glGetUniformLocation(p,'uModel'),model)
        GL.glActiveTexture(GL.GL_TEXTURE0);GL.glBindTexture(GL.GL_TEXTURE_2D,source)
        GL.glUniform1i(GL.glGetUniformLocation(p,'uSource'),0)
        for i in range(4):
            GL.glActiveTexture(GL.GL_TEXTURE1+i)
            GL.glBindTexture(GL.GL_TEXTURE_2D,features[i+(4 if pass_id==2 else 0)])
            GL.glUniform1i(GL.glGetUniformLocation(p,'uFeature'+str(i)),i+1)
        GL.glDrawArrays(GL.GL_TRIANGLES,0,3)

    with np.load(weights) as p:
        x=rgba[:,:,:3].astype(np.float32)/255
        expected1=np.maximum(conv(x,p['w1'],p['b1']),0)
        expected2=np.maximum(conv(expected1,p['w2'],p['b2']),0)
        residual=conv(expected2,p['w3'],p['b3'])
        residual=residual.reshape(h,w,2,2,3).transpose(0,2,1,3,4).reshape(h*2,w*2,3)
        base=bicubic(x)
        expected=np.clip(base+residual,0,1)
    for i,ref in enumerate([expected1,expected2]):
        draw(i)
        value=np.concatenate([read(w,h,c) for c in range(4)],axis=2)
        error=np.abs(value-ref)
        report['conv'+str(i+1)]={'max_abs':float(error.max()),'mean_abs':float(error.mean())}
        assert error.max()<.003,report
    for model,ref in [(0,base),(1,expected)]:
        draw(2,model)
        value=read(w*2,h*2)[:,:,:3]
        error=np.abs(value-ref)
        report['model' if model else 'bicubic']={'max_abs':float(error.max()),'mean_abs':float(error.mean())}
        assert error.max() < (.003 if model else .000001),report
    # Exercise the actual C++ copy/pass/state-restore code against the default
    # framebuffer too, not only standalone shaders. Only Doom's host API is stubbed.
    import host_adapter
    tmp,runner=host_adapter.build(shaders)
    source_fbo=target([source])
    for mode,ref in [(1,base),(2,expected),(1,base)]:
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER,source_fbo)
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER,0)
        GL.glBlitFramebuffer(0,0,w,h,0,0,w,h,GL.GL_COLOR_BUFFER_BIT,GL.GL_NEAREST)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER,0)
        GL.glViewport(0,0,w,h)
        runner.sr_render(w*2,h*2,mode)
        assert list(GL.glGetIntegerv(GL.GL_VIEWPORT))==[0,0,w,h]
        GL.glReadBuffer(GL.GL_BACK)
        raw=np.empty((h*2,w*2,4),np.uint8)
        GL.glReadPixels(0,0,w*2,h*2,GL.GL_RGBA,GL.GL_UNSIGNED_BYTE,raw)
        error=np.abs(raw[::-1,:,:3].astype(np.float32)/255-ref)
        report['engine_mode_'+str(mode)]={'max_abs':float(error.max()),'mean_abs':float(error.mean())}
        assert error.max()<2/255,report
    runner.sr_shutdown()
    tmp.cleanup()
    print(json.dumps(report,indent=2))
    EGL.eglMakeCurrent(ctx[0],EGL.EGL_NO_SURFACE,EGL.EGL_NO_SURFACE,EGL.EGL_NO_CONTEXT)
    EGL.eglDestroySurface(ctx[0],ctx[1]);EGL.eglDestroyContext(ctx[0],ctx[2]);EGL.eglTerminate(ctx[0])


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('weights',type=Path);p.add_argument('shaders',type=Path)
    p.add_argument('--width',type=int,default=31);p.add_argument('--height',type=int,default=19)
    a=p.parse_args();main(a.weights,a.shaders,a.width,a.height)
