// rdtool — RenderDoc 1.46 helper that runs on an Android phone (arm64, root), next to libVkLayer_GLES_RenderDoc.so.
//
//   rdtool cap <nframes> <timeout_s> [name-substring]
//       Connect to an app that already loaded the RenderDoc layer and trigger a capture of <nframes> frames.
//       The app listens on abstract unix sockets renderdoc_<port>; the replay library's client only speaks TCP,
//       so rdtool forwards TCP 127.0.0.1:<port> to the abstract socket for ports 38920..38927 itself.
//       Prints one JSON line per capture: {"capture_id","frame","bytes","path"} (path is on this phone).
//   rdtool info <x.rdc> <out.json>
//       Textures, resources, every action (flattened, with outputs/depth), render pass begin chunks and the
//       creation chunks of images, image views, render passes, framebuffers and swapchains from the structured data.
//   rdtool passes <x.rdc> <outdir> [min_width]
//       For every render pass, a JPEG preview of each colour output and of the depth target at the pass's last
//       action, value range stretched from GetMinMax. Writes <outdir>/passes.json describing them.
//   rdtool export <x.rdc> <outdir> <eid>:<resid>|<eid>:all ...
//       Replay to <eid> and write the texture's raw bytes (mip 0, slice 0) plus a JPEG preview.
//       "all" = the colour outputs and depth target of the action at <eid>.
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <thread>
#include <vector>

#include "renderdoc_replay.h"

REPLAY_PROGRAM_MARKER()

static uint64_t rid(ResourceId id)
{
  uint64_t v = 0;
  static_assert(sizeof(ResourceId) == sizeof(uint64_t), "ResourceId size");
  memcpy(&v, &id, sizeof v);
  return v;
}

static int fail(const char *what, const ResultDetails &r)
{
  fprintf(stderr, "%s failed: code=%u %s\n", what, (unsigned)r.code, r.internal_msg ? r.internal_msg->c_str() : "");
  return 1;
}

static std::string jstr(const char *s)
{
  std::string o = "\"";
  for(; s && *s; s++)
  {
    unsigned char c = (unsigned char)*s;
    if(c == '"' || c == '\\')
    {
      o += '\\';
      o += (char)c;
    }
    else if(c < 0x20)
    {
      char b[8];
      snprintf(b, sizeof b, "\\u%04x", c);
      o += b;
    }
    else
      o += (char)c;
  }
  return o + "\"";
}

// ---------------------------------------------------------------- cap

static void pump(int a, int b)
{
  char buf[65536];
  pollfd p[2] = {{a, POLLIN, 0}, {b, POLLIN, 0}};
  for(;;)
  {
    if(poll(p, 2, -1) <= 0)
      break;
    bool done = false;
    for(int i = 0; i < 2; i++)
    {
      if(p[i].revents & (POLLIN | POLLHUP | POLLERR))
      {
        ssize_t n = read(p[i].fd, buf, sizeof buf);
        if(n <= 0)
        {
          done = true;
          break;
        }
        int to = p[1 - i].fd;
        ssize_t off = 0;
        while(off < n)
        {
          ssize_t w = write(to, buf + off, (size_t)(n - off));
          if(w <= 0)
          {
            done = true;
            break;
          }
          off += w;
        }
      }
    }
    if(done)
      break;
  }
  close(a);
  close(b);
}

static void forwarder(uint16_t port)
{
  int s = socket(AF_INET, SOCK_STREAM, 0);
  int one = 1;
  setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  sockaddr_in in{};
  in.sin_family = AF_INET;
  in.sin_port = htons(port);
  in.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if(bind(s, (sockaddr *)&in, sizeof in) != 0 || listen(s, 4) != 0)
  {
    fprintf(stderr, "forwarder: tcp %u busy, skipped\n", port);
    close(s);
    return;
  }
  for(;;)
  {
    int c = accept(s, nullptr, nullptr);
    if(c < 0)
      continue;
    int u = socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::string name = "renderdoc_" + std::to_string(port);
    memcpy(addr.sun_path + 1, name.c_str(), name.size());
    socklen_t len = (socklen_t)(offsetof(sockaddr_un, sun_path) + 1 + name.size());
    if(connect(u, (sockaddr *)&addr, len) != 0)
    {
      close(u);
      close(c);
      continue;
    }
    std::thread(pump, c, u).detach();
  }
}

static int cmd_cap(int argc, char **argv)
{
  uint32_t n = (uint32_t)strtoul(argv[2], nullptr, 10);
  int timeout = atoi(argv[3]);
  const char *want = argc > 4 ? argv[4] : "";
  for(uint16_t p = 38920; p <= 38927; p++)
    std::thread(forwarder, p).detach();
  usleep(200000);

  ITargetControl *tc = nullptr;
  for(uint32_t ident = RENDERDOC_EnumerateRemoteTargets("", 0); ident; ident = RENDERDOC_EnumerateRemoteTargets("", ident))
  {
    ITargetControl *t = RENDERDOC_CreateTargetControl("", ident, "hgamey-rdtool", true);
    if(!t)
      continue;
    std::string target = t->GetTarget().c_str();
    printf("{\"target\":%s,\"ident\":%u,\"pid\":%u,\"api\":%s}\n", jstr(target.c_str()).c_str(), ident, t->GetPID(),
           jstr(t->GetAPI().c_str()).c_str());
    fflush(stdout);
    if(!tc && (!*want || target.find(want) != std::string::npos))
      tc = t;
    else
      t->Shutdown();
  }
  if(!tc)
  {
    fprintf(stderr, "no RenderDoc target found\n");
    return 3;
  }
  // let RegisterAPI arrive before triggering
  time_t t0 = time(nullptr);
  while(time(nullptr) - t0 < 3)
  {
    TargetControlMessage m = tc->ReceiveMessage(nullptr);
    if(m.type == TargetControlMessageType::RegisterAPI)
      printf("{\"api_registered\":%s}\n", jstr(m.apiUse.name.c_str()).c_str());
    if(m.type == TargetControlMessageType::Disconnected)
    {
      fprintf(stderr, "disconnected before trigger\n");
      return 4;
    }
  }
  tc->TriggerCapture(n);
  printf("{\"triggered\":%u,\"t\":%ld}\n", n, (long)time(nullptr));
  fflush(stdout);
  uint32_t got = 0;
  t0 = time(nullptr);
  while(got < n && time(nullptr) - t0 < timeout)
  {
    TargetControlMessage m = tc->ReceiveMessage(nullptr);
    if(m.type == TargetControlMessageType::NewCapture)
    {
      got++;
      printf("{\"capture_id\":%u,\"frame\":%u,\"bytes\":%llu,\"timestamp\":%llu,\"path\":%s}\n", m.newCapture.captureId,
             m.newCapture.frameNumber, (unsigned long long)m.newCapture.byteSize,
             (unsigned long long)m.newCapture.timestamp, jstr(m.newCapture.path.c_str()).c_str());
      fflush(stdout);
    }
    else if(m.type == TargetControlMessageType::Disconnected)
    {
      fprintf(stderr, "target disconnected after %u captures\n", got);
      break;
    }
  }
  tc->Shutdown();
  return got == n ? 0 : 5;
}

// ---------------------------------------------------------------- replay helpers

struct Replay
{
  ICaptureFile *file = nullptr;
  IReplayController *ctrl = nullptr;
};

static int open_replay(const char *path, Replay &r)
{
  GlobalEnvironment env;
  env.enumerateGPUs = false;
  RENDERDOC_InitialiseReplay(env, {});
  r.file = RENDERDOC_OpenCaptureFile();
  ResultDetails res = r.file->OpenFile(path, "", nullptr);
  if(!res.OK())
    return fail("OpenFile", res);
  ReplayOptions opts;
  rdcpair<ResultDetails, IReplayController *> rc = r.file->OpenCapture(opts, nullptr);
  if(!rc.first.OK())
    return fail("OpenCapture", rc.first);
  r.ctrl = rc.second;
  return 0;
}

static void close_replay(Replay &r)
{
  if(r.ctrl)
    r.ctrl->Shutdown();
  if(r.file)
    r.file->Shutdown();
  RENDERDOC_ShutdownReplay();
}

static void flatten(const rdcarray<ActionDescription> &in, std::vector<const ActionDescription *> &out)
{
  for(const ActionDescription &a : in)
  {
    out.push_back(&a);
    flatten(a.children, out);
  }
}

static const TextureDescription *tex_of(IReplayController *ctrl, ResourceId id)
{
  if(id == ResourceId::Null())
    return nullptr;
  for(const TextureDescription &t : ctrl->GetTextures())
    if(t.resourceId == id)
      return &t;
  return nullptr;
}

static std::string sdjson(const SDObject *o, int depth)
{
  if(!o)
    return "null";
  char b[64];
  switch(o->type.basetype)
  {
    case SDBasic::Chunk:
    case SDBasic::Struct:
    {
      if(depth > 10)
        return "\"...\"";
      std::string s = "{";
      for(size_t i = 0; i < o->NumChildren(); i++)
      {
        const SDObject *c = o->GetChild(i);
        if(i)
          s += ",";
        s += jstr(c->name.c_str()) + ":" + sdjson(c, depth + 1);
      }
      return s + "}";
    }
    case SDBasic::Array:
    {
      std::string s = "[";
      size_t n = o->NumChildren(), lim = n < 128 ? n : 128;
      for(size_t i = 0; i < lim; i++)
        s += (i ? "," : "") + sdjson(o->GetChild(i), depth + 1);
      if(n > lim)
        s += ",\"+" + std::to_string(n - lim) + " more\"";
      return s + "]";
    }
    case SDBasic::Null: return "null";
    case SDBasic::Buffer:
      snprintf(b, sizeof b, "{\"buffer\":%llu,\"bytes\":%llu}", (unsigned long long)o->data.basic.u,
               (unsigned long long)o->type.byteSize);
      return b;
    case SDBasic::String: return jstr(o->data.str.c_str());
    case SDBasic::Enum:
      snprintf(b, sizeof b, "%llu", (unsigned long long)o->data.basic.u);
      return o->data.str.empty() ? std::string(b) : jstr(o->data.str.c_str());
    case SDBasic::UnsignedInteger:
    case SDBasic::GPUAddress:
      snprintf(b, sizeof b, "%llu", (unsigned long long)o->data.basic.u);
      return b;
    case SDBasic::SignedInteger: snprintf(b, sizeof b, "%lld", (long long)o->data.basic.i); return b;
    case SDBasic::Float: snprintf(b, sizeof b, "%.9g", o->data.basic.d); return b;
    case SDBasic::Boolean: return o->data.basic.b ? "true" : "false";
    case SDBasic::Character: snprintf(b, sizeof b, "%d", (int)o->data.basic.c); return b;
    case SDBasic::Resource: snprintf(b, sizeof b, "%llu", (unsigned long long)rid(o->data.basic.id)); return b;
  }
  return "null";
}

static std::string tex_json(const TextureDescription &t)
{
  char b[512];
  snprintf(b, sizeof b,
           "{\"id\":%llu,\"w\":%u,\"h\":%u,\"d\":%u,\"mips\":%u,\"layers\":%u,\"samples\":%u,\"format\":%s,"
           "\"flags\":%u,\"bytes\":%llu}",
           (unsigned long long)rid(t.resourceId), t.width, t.height, t.depth, t.mips, t.arraysize, t.msSamp,
           jstr(t.format.Name().c_str()).c_str(), (unsigned)t.creationFlags, (unsigned long long)t.byteSize);
  return b;
}

// JPEG preview with the value range stretched to the texture's min..max over RGB (depth: the depth channel)
static bool preview(IReplayController *ctrl, const TextureDescription &t, const std::string &path)
{
  rdcpair<PixelValue, PixelValue> mm = ctrl->GetMinMax(t.resourceId, Subresource(0, 0, 0), CompType::Typeless);
  bool depth = (uint32_t(t.creationFlags) & uint32_t(TextureCategory::DepthTarget)) != 0;
  int nc = depth ? 1 : (t.format.compCount < 3 ? t.format.compCount : 3);
  float lo = mm.first.floatValue[0], hi = mm.second.floatValue[0];
  for(int c = 1; c < nc; c++)
  {
    lo = lo < mm.first.floatValue[c] ? lo : mm.first.floatValue[c];
    hi = hi > mm.second.floatValue[c] ? hi : mm.second.floatValue[c];
  }
  if(!(hi > lo))
    hi = lo + 1.0f;
  TextureSave ts;
  ts.resourceId = t.resourceId;
  ts.destType = FileType::JPG;
  ts.mip = 0;
  ts.comp.blackPoint = lo;
  ts.comp.whitePoint = hi;
  ts.alpha = AlphaMapping::Discard;
  ts.jpegQuality = 85;
  if(depth)
    ts.channelExtract = 0;
  ResultDetails r = ctrl->SaveTexture(ts, path.c_str());
  return r.OK();
}

// ---------------------------------------------------------------- info

static int cmd_info(int argc, char **argv)
{
  Replay r;
  if(open_replay(argv[2], r))
    return 1;
  IReplayController *ctrl = r.ctrl;
  const SDFile &sd = ctrl->GetStructuredFile();
  FILE *f = fopen(argv[3], "w");
  if(!f)
    return 1;
  fprintf(f, "{\"textures\":[");
  bool first = true;
  for(const TextureDescription &t : ctrl->GetTextures())
  {
    fprintf(f, "%s\n%s", first ? "" : ",", tex_json(t).c_str());
    first = false;
  }
  fprintf(f, "],\n\"resources\":[");
  first = true;
  for(const ResourceDescription &d : ctrl->GetResources())
  {
    fprintf(f, "%s\n{\"id\":%llu,\"type\":%u,\"name\":%s}", first ? "" : ",", (unsigned long long)rid(d.resourceId),
            (unsigned)d.type, jstr(d.name.c_str()).c_str());
    first = false;
  }
  fprintf(f, "],\n\"actions\":[");
  std::vector<const ActionDescription *> acts;
  flatten(ctrl->GetRootActions(), acts);
  first = true;
  for(const ActionDescription *a : acts)
  {
    std::string outs;
    for(int i = 0; i < 8; i++)
      if(a->outputs[i] != ResourceId::Null())
        outs += (outs.empty() ? "" : ",") + std::to_string(rid(a->outputs[i]));
    fprintf(f,
            "%s\n{\"eid\":%u,\"name\":%s,\"flags\":%u,\"parent\":%u,\"outputs\":[%s],\"depth\":%llu,\"indices\":%u,"
            "\"instances\":%u,\"dispatch\":[%u,%u,%u],\"copy_src\":%llu,\"copy_dst\":%llu",
            first ? "" : ",", a->eventId, jstr(a->GetName(sd).c_str()).c_str(), (unsigned)a->flags,
            a->parent ? a->parent->eventId : 0, outs.c_str(), (unsigned long long)rid(a->depthOut), a->numIndices,
            a->numInstances, a->dispatchDimension[0], a->dispatchDimension[1], a->dispatchDimension[2],
            (unsigned long long)rid(a->copySource), (unsigned long long)rid(a->copyDestination));
    if((uint32_t(a->flags) & uint32_t(ActionFlags::BeginPass)) && !a->events.empty() &&
       a->events.back().chunkIndex < sd.chunks.size())
      fprintf(f, ",\"chunk\":%s", sdjson(sd.chunks[a->events.back().chunkIndex], 0).c_str());
    fprintf(f, "}");
    first = false;
  }
  fprintf(f, "],\n\"chunks\":[");
  static const char *keep[] = {"vkCreateImage",     "vkCreateImageView",      "vkCreateRenderPass",
                               "vkCreateRenderPass2", "vkCreateRenderPass2KHR", "vkCreateFramebuffer",
                               "vkCreateSwapchainKHR", nullptr};
  first = true;
  for(size_t i = 0; i < sd.chunks.size(); i++)
  {
    const SDChunk *c = sd.chunks[i];
    bool k = false;
    for(int j = 0; keep[j]; j++)
      k = k || strcmp(c->name.c_str(), keep[j]) == 0;
    if(!k)
      continue;
    fprintf(f, "%s\n{\"index\":%zu,\"name\":%s,\"data\":%s}", first ? "" : ",", i, jstr(c->name.c_str()).c_str(),
            sdjson(c, 0).c_str());
    first = false;
  }
  fprintf(f, "]}\n");
  fclose(f);
  printf("textures=%zu actions=%zu chunks=%zu\n", ctrl->GetTextures().size(), acts.size(), sd.chunks.size());
  close_replay(r);
  return 0;
}

// ---------------------------------------------------------------- passes

static int cmd_passes(int argc, char **argv)
{
  Replay r;
  if(open_replay(argv[2], r))
    return 1;
  IReplayController *ctrl = r.ctrl;
  const SDFile &sd = ctrl->GetStructuredFile();
  std::string out = argv[3];
  uint32_t minw = argc > 4 ? (uint32_t)strtoul(argv[4], nullptr, 10) : 0;
  std::vector<const ActionDescription *> acts;
  flatten(ctrl->GetRootActions(), acts);

  struct Pass
  {
    uint32_t begin = 0, end = 0, last = 0, draws = 0;
    std::vector<ResourceId> outs;
    ResourceId depth;
  };
  std::vector<Pass> passes;
  Pass cur;
  bool in = false;
  for(const ActionDescription *a : acts)
  {
    uint32_t fl = uint32_t(a->flags);
    if(fl & uint32_t(ActionFlags::BeginPass))
    {
      cur = Pass();
      cur.begin = a->eventId;
      in = true;
      continue;
    }
    if(fl & uint32_t(ActionFlags::EndPass))
    {
      if(in)
      {
        cur.end = a->eventId;
        if(cur.last)
          passes.push_back(cur);
      }
      in = false;
      continue;
    }
    if(!in || !(fl & (uint32_t(ActionFlags::Drawcall) | uint32_t(ActionFlags::Clear))))
      continue;
    cur.last = a->eventId;
    if(fl & uint32_t(ActionFlags::Drawcall))
      cur.draws++;
    for(int i = 0; i < 8; i++)
    {
      ResourceId o = a->outputs[i];
      if(o == ResourceId::Null())
        continue;
      bool seen = false;
      for(ResourceId x : cur.outs)
        seen = seen || x == o;
      if(!seen)
        cur.outs.push_back(o);
    }
    if(a->depthOut != ResourceId::Null())
      cur.depth = a->depthOut;
  }

  FILE *f = fopen((out + "/passes.json").c_str(), "w");
  fprintf(f, "[");
  for(size_t p = 0; p < passes.size(); p++)
  {
    Pass &ps = passes[p];
    ctrl->SetFrameEvent(ps.last, true);
    std::string items;
    std::vector<std::pair<std::string, ResourceId>> targets;
    for(size_t i = 0; i < ps.outs.size(); i++)
      targets.push_back({"c" + std::to_string(i), ps.outs[i]});
    if(ps.depth != ResourceId::Null())
      targets.push_back({"depth", ps.depth});
    for(auto &tg : targets)
    {
      const TextureDescription *t = tex_of(ctrl, tg.second);
      if(!t)
        continue;
      std::string file;
      if(t->width >= minw)
      {
        char nm[160];
        snprintf(nm, sizeof nm, "p%03zu_e%u_%s_r%llu_%ux%u.jpg", p, ps.last, tg.first.c_str(),
                 (unsigned long long)rid(tg.second), t->width, t->height);
        if(preview(ctrl, *t, out + "/" + nm))
          file = nm;
      }
      items += (items.empty() ? "" : ",") + std::string("{\"slot\":") + jstr(tg.first.c_str()) +
               ",\"tex\":" + tex_json(*t) + ",\"file\":" + jstr(file.c_str()) + "}";
    }
    fprintf(f, "%s\n{\"pass\":%zu,\"begin\":%u,\"end\":%u,\"last\":%u,\"draws\":%u,\"targets\":[%s]}", p ? "," : "", p,
            ps.begin, ps.end, ps.last, ps.draws, items.c_str());
    fflush(f);
    printf("pass %zu begin=%u last=%u draws=%u targets=%zu\n", p, ps.begin, ps.last, ps.draws, targets.size());
    fflush(stdout);
  }
  fprintf(f, "]\n");
  fclose(f);
  (void)sd;
  close_replay(r);
  return 0;
}

// ---------------------------------------------------------------- export

static int cmd_export(int argc, char **argv)
{
  Replay r;
  if(open_replay(argv[2], r))
    return 1;
  IReplayController *ctrl = r.ctrl;
  std::string out = argv[3];
  std::vector<const ActionDescription *> acts;
  flatten(ctrl->GetRootActions(), acts);
  int bad = 0;
  for(int i = 4; i < argc; i++)
  {
    uint32_t eid = (uint32_t)strtoul(argv[i], nullptr, 10);
    const char *colon = strchr(argv[i], ':');
    if(!colon)
    {
      fprintf(stderr, "bad spec %s\n", argv[i]);
      bad++;
      continue;
    }
    std::vector<std::pair<std::string, ResourceId>> targets;
    if(!strcmp(colon + 1, "all"))
    {
      const ActionDescription *act = nullptr;
      for(const ActionDescription *a : acts)
        if(a->eventId == eid)
          act = a;
      if(!act)
      {
        fprintf(stderr, "no event %u\n", eid);
        bad++;
        continue;
      }
      for(int k = 0; k < 8; k++)
        if(act->outputs[k] != ResourceId::Null())
          targets.push_back({"c" + std::to_string(k), act->outputs[k]});
      if(act->depthOut != ResourceId::Null())
        targets.push_back({"depth", act->depthOut});
    }
    else
    {
      uint64_t want = strtoull(colon + 1, nullptr, 10);
      for(const TextureDescription &t : ctrl->GetTextures())
        if(rid(t.resourceId) == want)
          targets.push_back({"r", t.resourceId});
    }
    ctrl->SetFrameEvent(eid, true);
    for(auto &tg : targets)
    {
      const TextureDescription *t = tex_of(ctrl, tg.second);
      if(!t)
        continue;
      bytebuf data = ctrl->GetTextureData(tg.second, Subresource(0, 0, 0));
      char nm[200];
      snprintf(nm, sizeof nm, "e%u_%s_r%llu_%ux%u_%s", eid, tg.first.c_str(), (unsigned long long)rid(tg.second),
               t->width, t->height, t->format.Name().c_str());
      std::string base = out + "/" + nm;
      FILE *f = fopen((base + ".raw").c_str(), "wb");
      if(f)
      {
        fwrite(data.data(), 1, data.size(), f);
        fclose(f);
      }
      bool pv = preview(ctrl, *t, base + ".jpg");
      printf("{\"eid\":%u,\"slot\":%s,\"tex\":%s,\"raw\":%s,\"raw_bytes\":%zu,\"preview\":%s}\n", eid,
             jstr(tg.first.c_str()).c_str(), tex_json(*t).c_str(), jstr((std::string(nm) + ".raw").c_str()).c_str(),
             (size_t)data.size(), pv ? "true" : "false");
      fflush(stdout);
    }
  }
  close_replay(r);
  return bad ? 1 : 0;
}

int main(int argc, char **argv)
{
  if(argc >= 4 && !strcmp(argv[1], "cap"))
    return cmd_cap(argc, argv);
  if(argc == 4 && !strcmp(argv[1], "info"))
    return cmd_info(argc, argv);
  if(argc >= 4 && !strcmp(argv[1], "passes"))
    return cmd_passes(argc, argv);
  if(argc >= 5 && !strcmp(argv[1], "export"))
    return cmd_export(argc, argv);
  fprintf(stderr,
          "usage: rdtool cap <nframes> <timeout_s> [name]\n"
          "       rdtool info <x.rdc> <out.json>\n"
          "       rdtool passes <x.rdc> <outdir> [min_width]\n"
          "       rdtool export <x.rdc> <outdir> <eid>:<resid>|<eid>:all ...\n");
  return 2;
}
