// Doom3 realtime SmallSR experiment. GPL-3.0-or-later.
// Included from renderer/tr_backend.cpp; built against idTech4A++ v73.
#include "small_sr_shaders.h"

idCVar r_smallSR("r_smallSR", "0", CVAR_RENDERER | CVAR_INTEGER | CVAR_ARCHIVE,
    "SmallSR: 0=native, 1=half-size+bicubic, 2=half-size+trained model");

namespace {
struct SmallSR {
    GLuint programs[3];
    GLuint source, features[8], framebuffer;
    int width, height;
} sr = {};

GLuint SR_Compile(GLenum type, const char *source) {
    GLuint shader = qglCreateShader(type);
    qglShaderSource(shader, 1, &source, NULL);
    qglCompileShader(shader);
    GLint ok;
    qglGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[8192];
        qglGetShaderInfoLog(shader, sizeof(log), NULL, log);
        common->Error("SmallSR shader: %s", log);
    }
    return shader;
}

GLuint SR_Program(const char *fragment) {
    GLuint vs = SR_Compile(GL_VERTEX_SHADER, sr_vertex);
    GLuint fs = SR_Compile(GL_FRAGMENT_SHADER, fragment);
    GLuint program = qglCreateProgram();
    qglAttachShader(program, vs);
    qglAttachShader(program, fs);
    qglLinkProgram(program);
    GLint ok;
    qglGetProgramiv(program, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[8192];
        qglGetProgramInfoLog(program, sizeof(log), NULL, log);
        common->Error("SmallSR link: %s", log);
    }
    qglDeleteShader(vs);
    qglDeleteShader(fs);
    return program;
}

void SR_Texture(GLuint tex, GLint format, GLenum type) {
    qglBindTexture(GL_TEXTURE_2D, tex);
    qglTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    qglTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    qglTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    qglTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    qglTexImage2D(GL_TEXTURE_2D, 0, format, sr.width, sr.height, 0, GL_RGBA, type, NULL);
}

void SR_Size(int width, int height) {
    if (sr.width == width && sr.height == height) return;
    sr.width = width; sr.height = height;
    SR_Texture(sr.source, GL_RGBA8, GL_UNSIGNED_BYTE);
    for (int i = 0; i < 8; ++i) SR_Texture(sr.features[i], GL_RGBA16F, GL_HALF_FLOAT);
    common->Printf("SmallSR buffers %dx%d -> %dx%d, RGBA16F, weights %s\n",
        width, height, width*2, height*2, sr_weight_sha256);
}

void SR_Use(int pass) {
    GLuint program = sr.programs[pass];
    qglUseProgram(program);
    qglUniform2i(qglGetUniformLocation(program, "uSize"), sr.width, sr.height);
    qglUniform1i(qglGetUniformLocation(program, "uSource"), 0);
    for (int i = 0; i < 4; ++i) {
        char name[32];
        sprintf(name, "uFeature%d", i);
        qglUniform1i(qglGetUniformLocation(program, name), i+1);
    }
}

void SR_Features(int first) {
    for (int i = 0; i < 4; ++i) {
        qglActiveTexture(GL_TEXTURE1+i);
        qglBindTexture(GL_TEXTURE_2D, sr.features[first+i]);
    }
}

void SR_Target(int first) {
    qglBindFramebuffer(GL_FRAMEBUFFER, sr.framebuffer);
    const GLenum outputs[] = {GL_COLOR_ATTACHMENT0, GL_COLOR_ATTACHMENT1,
                              GL_COLOR_ATTACHMENT2, GL_COLOR_ATTACHMENT3};
    for (int i = 0; i < 4; ++i)
        qglFramebufferTexture2D(GL_FRAMEBUFFER, outputs[i], GL_TEXTURE_2D, sr.features[first+i], 0);
    qglDrawBuffers(4, outputs);
}
} // namespace

void RB_SmallSR_Shutdown() {
    for (int i = 0; i < 3; ++i) if (sr.programs[i]) qglDeleteProgram(sr.programs[i]);
    qglDeleteTextures(1, &sr.source);
    qglDeleteTextures(8, sr.features);
    qglDeleteFramebuffers(1, &sr.framebuffer);
    memset(&sr, 0, sizeof(sr));
}

void RB_SmallSR_Render() {
    const int mode = r_smallSR.GetInteger();
    if (!mode) return;
    // This is the last game draw, before the native ImGui/Android controls.
    GLint viewport[4], drawFbo, readFbo, active, textures[5];
    GLboolean depthMask, colorMask[4];
    qglGetIntegerv(GL_VIEWPORT, viewport);
    qglGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &drawFbo);
    qglGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFbo);
    qglGetIntegerv(GL_ACTIVE_TEXTURE, &active);
    qglGetBooleanv(GL_DEPTH_WRITEMASK, &depthMask);
    qglGetBooleanv(GL_COLOR_WRITEMASK, colorMask);
    const GLenum flags[] = {GL_BLEND, GL_DEPTH_TEST, GL_STENCIL_TEST, GL_SCISSOR_TEST, GL_CULL_FACE};
    GLboolean enabled[5];
    for (int i = 0; i < 5; ++i) {
        enabled[i] = qglIsEnabled(flags[i]); qglDisable(flags[i]);
        qglActiveTexture(GL_TEXTURE0+i);
        qglGetIntegerv(GL_TEXTURE_BINDING_2D, &textures[i]);
    }
    qglActiveTexture(GL_TEXTURE0);
    qglColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    qglDepthMask(GL_FALSE);
    GL_UseProgram(NULL);

    if (!sr.programs[0]) {
        sr.programs[0] = SR_Program(sr_conv1);
        sr.programs[1] = SR_Program(sr_conv2);
        sr.programs[2] = SR_Program(sr_output);
        qglGenTextures(1, &sr.source);
        qglGenTextures(8, sr.features);
        qglGenFramebuffers(1, &sr.framebuffer);
    }
    SR_Size(glConfig.vidWidth/2, glConfig.vidHeight/2);
    qglBindTexture(GL_TEXTURE_2D, sr.source);
    qglCopyTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 0, 0, sr.width, sr.height);

    if (mode == 2) {
        qglViewport(0, 0, sr.width, sr.height);
        SR_Target(0);
        if (qglCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
            common->Error("SmallSR requires renderable RGBA16F and four color attachments");
        SR_Use(0);
        qglDrawArrays(GL_TRIANGLES, 0, 3);
        SR_Target(4);
        SR_Features(0);
        SR_Use(1);
        qglDrawArrays(GL_TRIANGLES, 0, 3);
    }

    qglBindFramebuffer(GL_FRAMEBUFFER, drawFbo);
    qglViewport(0, 0, glConfig.vidWidth, glConfig.vidHeight);
    SR_Features(4);
    SR_Use(2);
    qglUniform1i(qglGetUniformLocation(sr.programs[2], "uModel"), mode == 2);
    qglDrawArrays(GL_TRIANGLES, 0, 3);

    qglUseProgram(0);
    qglBindFramebuffer(GL_READ_FRAMEBUFFER, readFbo);
    qglViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    qglDepthMask(depthMask);
    qglColorMask(colorMask[0], colorMask[1], colorMask[2], colorMask[3]);
    for (int i = 0; i < 5; ++i) {
        if (enabled[i]) qglEnable(flags[i]);
        qglActiveTexture(GL_TEXTURE0+i);
        qglBindTexture(GL_TEXTURE_2D, textures[i]);
    }
    qglActiveTexture(active);
}
