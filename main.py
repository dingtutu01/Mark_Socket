from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import httpx
import os
import uvicorn

# === 依赖 ===
from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple, Any
import base64, datetime, json, traceback
from PIL import Image, ImageDraw, ImageFont

import asyncio
import io
import time
import secrets
import hashlib
import requests
from functools import lru_cache

# 每个 (sid,id) 一把锁，避免并发覆盖
_locks: Dict[Tuple[str, str], asyncio.Lock] = {}

def get_lock(sid: str, file_id: str) -> asyncio.Lock:
    key = (sid, file_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态资源
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ========= 标定数据根目录 =========
DATA_ROOT = "calibration_data"
os.makedirs(DATA_ROOT, exist_ok=True)
# 公开访问整个树（含每个 sid 的子目录）
app.mount("/calib", StaticFiles(directory=DATA_ROOT), name="calib")


def now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_sid(sid: Optional[str]) -> str:
    """仅保留字母/数字/下划线/连字符，默认 public。"""
    if not sid:
        return "public"
    s = "".join([c for c in sid if c.isalnum() or c in ("_", "-")])
    return s if s else "public"


def get_user_dirs(sid: str):
    """返回该 sid 的 snapshots/annotated/json 目录，确保存在。"""
    base = os.path.join(DATA_ROOT, sid)
    snap = os.path.join(base, "snapshots")
    ann = os.path.join(base, "annotated")
    jsn = os.path.join(base, "json")
    os.makedirs(snap, exist_ok=True)
    os.makedirs(ann, exist_ok=True)
    os.makedirs(jsn, exist_ok=True)
    return base, snap, ann, jsn


# ===================== 普通播放器 =====================
@app.get("/player", response_class=HTMLResponse)
async def basic_player():
    return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>标准视频播放</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xgplayer@3.0.17/dist/index.min.css">
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei','Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#f0f2f5;padding:20px}
    .wrap{max-width:1600px;margin:0 auto}
    .topbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}
    .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid #1677ff;color:#1677ff;text-decoration:none;background:#fff}
    .pill:hover{background:#1677ff;color:#fff}
    .box{background:#fff;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.08);overflow:hidden}
    #player{width:100%;height:800px;background:#000}
    .bar{padding:16px;border-top:1px solid #eee}
    .row{display:flex;gap:10px}
    input{flex:1;min-width:360px;padding:10px 14px;border:1px solid #d9d9d9;border-radius:10px}
    .btn{padding:10px 18px;border:none;border-radius:10px;background:#1677ff;color:#fff;cursor:pointer;box-shadow:0 6px 14px rgba(22,119,255,.25)}
    .btn:hover{filter:brightness(1.05)}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <a class="pill" href="/">← 返回主页</a>
    </div>
    <div class="box">
      <div id="player"></div>
      <div class="bar">
        <div class="row">
          <input id="mp4Url" value="https://lf9-cdn-tos.bytecdntp.com/cdn/expire-1-M/byted-player-videos/1.0.0/xgplayer-demo.mp4" />
          <button class="btn" onclick="load()">加载</button>
        </div>
      </div>
    </div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/xgplayer@3.0.17/dist/index.min.js"></script>
  <script>
    let player=null;
    function init(u){
      if(player){try{player.destroy()}catch(e){} player=null}
      player=new Player({id:'player',url:u,width:'100%',height:800,autoplay:false,fluid:true,controls:true,lang:'zh-cn'});
    }
    function load(){ const u=document.getElementById('mp4Url').value.trim(); if(!u){alert('请输入地址');return} init(u) }
    window.addEventListener('DOMContentLoaded',load)
  </script>
</body>
</html>
    """


# ===================== FLV 播放器（含 sid 与按钮美化） =====================
@app.get("/player-flv", response_class=HTMLResponse)
async def flv_player():
    return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>实时视频流 (FLV)</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xgplayer@3.0.17/dist/index.min.css">
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei','Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#f0f2f5;padding:20px}
    .wrap{max-width:1600px;margin:0 auto}
    .topbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}
    .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid #1677ff;color:#1677ff;text-decoration:none;background:#fff}
    .pill.secondary{border-color:#52c41a;color:#52c41a}
    .pill:hover{filter:brightness(1.05);background:linear-gradient(0deg, rgba(22,119,255,.06), rgba(22,119,255,.06))}
    .pill.secondary:hover{background:linear-gradient(0deg, rgba(82,196,26,.08), rgba(82,196,26,.08))}
    .sid{margin-left:auto;color:#666;font-size:12px}
    .box{background:#fff;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.08);overflow:hidden}
    #player{width:100%;height:800px;background:#000}
    .bar{padding:16px;border-top:1px solid #eee}
    .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
    input{flex:1;min-width:420px;padding:12px 14px;border:1px solid #d9d9d9;border-radius:12px}
    .btnx{display:inline-flex;align-items:center;gap:8px;padding:12px 18px;border:none;border-radius:999px;color:#fff;cursor:pointer;
          box-shadow:0 6px 16px rgba(0,0,0,.15); transition:.15s transform, .15s filter}
    .btnx .ico{font-size:16px}
    .btnx:hover{transform:translateY(-1px); filter:brightness(1.05)}
    .btnx:active{transform:translateY(0)}
    .btnx.blue{background:linear-gradient(135deg,#1677ff,#4096ff)}
    .btnx.green{background:linear-gradient(135deg,#45c13d,#6ede5e)}
    .btnx.orange{background:linear-gradient(135deg,#ff8a3d,#ffa45c)}
    .btnx[disabled]{opacity:.45;cursor:not-allowed;filter:none;transform:none}
    .status{margin-top:10px;color:#666;background:#f8fafc;border-radius:10px;padding:10px 12px}
    .mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;z-index:999}
    .modal{width:420px;background:#fff;border-radius:14px;box-shadow:0 30px 80px rgba(0,0,0,.35);overflow:hidden}
    .modal .hd{padding:16px 20px;border-bottom:1px solid #f0f0f0;font-weight:700}
    .modal .bd{padding:20px;color:#555;line-height:1.7}
    .modal .ft{padding:12px 20px;border-top:1px solid #f0f0f0;display:flex;gap:10px;justify-content:flex-end}
    .btnGhost{padding:10px 18px;border-radius:10px;border:1px solid #1677ff;background:#fff;color:#1677ff;cursor:pointer}
    .btnFill{padding:10px 18px;border-radius:10px;border:none;background:#52c41a;color:#fff;cursor:pointer}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <a class="pill" href="/portal"> 返回设备列表</a>
      <a class="pill secondary" href="#player" onclick="document.getElementById('flvUrl').focus()">🎥 转到播放输入</a>
      <div class="sid" id="sidView"></div>
    </div>
    <div class="box">
      <div id="player"></div>
      <div class="bar">
        <div class="row">
          <input id="flvUrl" placeholder="请输入 FLV 地址或 /stream/flv?url=..." />
          <button class="btnx green" onclick="loadWithProxy()"><span class="ico">▶️</span>视频流加载</button>
          <button id="btnCal" class="btnx orange" style="display:none" disabled onclick="openConfirm()"><span class="ico">📐</span>标定</button>
        </div>
        <div id="status" class="status">状态：等待连接</div>
      </div>
    </div>
  </div>

  <!-- 美化的确认弹窗 -->
  <div id="confirmMask" class="mask" role="dialog" aria-modal="true">
    <div class="modal">
      <div class="hd">截图并进入标定？</div>
      <div class="bd">
        将对<strong>当前画面</strong>进行截图并跳转到标定页面。完成后可一键返回继续播放。
      </div>
      <div class="ft">
        <button class="btnGhost" onclick="closeConfirm()">取消</button>
        <button class="btnFill" onclick="doCapture()">确定</button>
      </div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/xgplayer@3.0.17/dist/index.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xgplayer-flv@3.0.17/dist/index.min.js"></script>
  <script>
    let player=null, currentUrl='';
    const sampleUrl='https://sf1-cdn-tos.huoshanstatic.com/obj/media-fe/xgplayer_doc_video/flv/xgplayer-demo-360p.flv';
    const $ = (id)=>document.getElementById(id);
    function qs(k){ const p=new URLSearchParams(location.search); return p.get(k); }

    // === sid：从 URL > localStorage 获取/生成 ===
    function getSID(){
      let s = qs('sid') || localStorage.getItem('fc_sid');
      if(!s){
        s = 'u_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      }
      localStorage.setItem('fc_sid', s);
      return s;
    }
    const SID = getSID();
    $('sidView').textContent = 'sid: ' + SID;

    function showCal(flag){ const b=$('btnCal'); b.style.display=flag?'inline-flex':'none'; b.disabled=!flag; }
    function setStatus(m){ $('status').textContent='状态：'+m; }

    function initPlayer(url){
      if(player){ try{player.destroy()}catch(e){} player=null }
      currentUrl=url; showCal(false);
      player=new Player({
        id:'player', url, width:'100%', height:800, autoplay:true, fluid:true, controls:true, lang:'zh-cn',
        plugins:[FlvPlayer],
        flv:{retryCount:3,retryDelay:1000,enableWorker:false,enableStashBuffer:true,stashInitialSize:384,cors:true,seekType:'range'}
      });
      player.on('canplay',()=>{ setStatus('可以播放'); if(player?.video?.videoWidth) showCal(true); });
      player.on('playing',()=>{ setStatus('正在播放'); showCal(true);});
      player.on('waiting',()=>setStatus('缓冲中...'));
      player.on('pause',()=>setStatus('已暂停'));
      player.on('ended',()=>{ setStatus('播放结束'); showCal(false); });
      player.on('error',e=>{ console.error(e); setStatus('错误'); showCal(false); });
      player.on('destroy',()=>showCal(false));
    }

    function loadWithProxy(){
      const raw=$('flvUrl').value.trim() || sampleUrl;
      const u = raw.startsWith('/stream/flv?url=') ? raw : '/stream/flv?url='+encodeURIComponent(raw);
      $('flvUrl').value = u; setStatus('代理连接...'); initPlayer(u);
      history.replaceState(null,'', location.pathname+'?sid='+encodeURIComponent(SID)+(u?('&url='+encodeURIComponent(u)):''));
    }

    // 自动加载（从标定页返回会带 url & sid）
    window.addEventListener('DOMContentLoaded', ()=>{
      const u = qs('url'); if(u){ $('flvUrl').value = decodeURIComponent(u); initPlayer(decodeURIComponent(u)); }
      setStatus('等待连接'); showCal(false);
      if(!qs('sid')) history.replaceState(null,'', location.pathname+'?sid='+encodeURIComponent(SID)+(u?('&url='+encodeURIComponent(u)):''));
    });

    // —— 美化确认弹窗 ——
    function openConfirm(){ $('confirmMask').style.display='flex'; }
    function closeConfirm(){ $('confirmMask').style.display='none'; }

    // 真正执行截图（带 sid）
    async function doCapture(){
      closeConfirm();
      if(!player || !player.video){ alert('播放器未就绪'); return; }
      const v=player.video;
      if(!v.videoWidth || !v.videoHeight){ alert('无画面'); return; }
      try{
        const c=document.createElement('canvas');
        c.width=v.videoWidth; c.height=v.videoHeight;
        c.getContext('2d').drawImage(v,0,0,c.width,c.height);
        const dataURL=c.toDataURL('image/jpeg', 0.85);
        const resp=await fetch('/api/upload-snapshot',{
          method:'POST',
          headers:{'Content-Type':'application/json','Accept':'application/json'},
          body:JSON.stringify({imageData:dataURL, sid: SID})
        }).then(r=>r.json());
        if(!resp.ok){ alert('上传失败：'+(resp.error||'未知错误')); return; }
        const back = '/calibrate?id='+resp.id+'&src='+encodeURIComponent(currentUrl||'')+'&sid='+encodeURIComponent(SID);
        location.href = back;
      }catch(err){ console.error(err); alert('截图失败：建议使用“代理加载”后再试'); }
    }
    // —— 离开页面时，确保销毁播放器，主动断开网络 —— //
    let _cleanupDone = false;
    function cleanupStream() {
      if (_cleanupDone) return;
      _cleanupDone = true;
      try { player && player.destroy && player.destroy(); } catch(e) {}
      console.log('播放器已销毁')
      player = null;
    }

    // 1) 页面关闭 / 刷新 / 跳转
    window.addEventListener('beforeunload', cleanupStream);
    window.addEventListener('pagehide', cleanupStream, {capture: true});

    // 2) 页面不可见（切到后台）时，立即断流（也可改为延时）
    let _hideTimer = null;
    document.addEventListener('visibilitychange', () => {
      clearTimeout(_hideTimer);
      if (document.hidden) {
        _hideTimer = setTimeout(() => cleanupStream(), 15_000); // 后台 15 秒后断流
      }
    });

    // 3) 单页内跳转（若有 hash/router），可在路由变化时清理
    window.addEventListener('hashchange', cleanupStream);

    // 4) 保险：播放器自己的销毁事件
    //    你原来已有 player.on('destroy',...) 的逻辑，可以保留/不动
  </script>
</body>
</html>
    """


# # ===================== 标定后端 API（含 sid） =====================
class SnapshotIn(BaseModel):
    imageData: str  # dataURL
    sid: Optional[str] = None

@app.get("/stream/flv")
async def stream_flv(url: str, request: Request):
    """代理 FLV 流以绕过 CORS；当客户端断开时立即停止上游请求"""
    async def gen():
        try:
            # timeout 可按需调整；Connection: close 避免上游长连
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None, write=30.0, connect=10.0)) as client:
                async with client.stream("GET", url, headers={"Connection": "close"}) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(8192):
                        # —— 关键：浏览器断开时，立刻 break，结束上游拉流 ——
                        if await request.is_disconnected():
                            break
                        yield chunk
        except asyncio.CancelledError:
            # 客户端主动断开会触发取消，直接结束
            return
        except Exception:
            # 静默结束生成器，避免后台持续占用
            return

    return StreamingResponse(
        gen(),
        media_type="video/x-flv",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )

@app.post("/api/upload-snapshot")
async def upload_snapshot(payload: SnapshotIn):
    try:
        if "," not in payload.imageData:
            return {"ok": False, "error": "invalid dataURL"}
        sid = sanitize_sid(payload.sid)
        _, snap_dir, _, _ = get_user_dirs(sid)

        header, b64 = payload.imageData.split(",", 1)
        h = header.lower()
        ext = "png"
        if "jpeg" in h or "jpg" in h: ext = "jpg"
        elif "webp" in h: ext = "webp"

        raw = base64.b64decode(b64)
        ts = now_id()
        with open(os.path.join(snap_dir, f"{ts}.{ext}"), "wb") as f:
            f.write(raw)
        return {"ok": True, "id": ts, "image_url": f"/calib/{sid}/snapshots/{ts}.{ext}"}
    except Exception as e:
        print("upload_snapshot error:", traceback.format_exc())
        return {"ok": False, "error": str(e)}


# —— 标定页面（美化右上角信息区 + 点位显示表格/进度条；显示两位小数；双击不误加点；放大镜贴边越界黑色；带 sid） ——
@app.get("/calibrate", response_class=HTMLResponse)
async def calibrate_page(id: str, sid: Optional[str] = None):
    sid = sanitize_sid(sid)
    # 找到真实存在的截图文件
    _, snap_dir, _, _ = get_user_dirs(sid)
    ext = None
    for e in ("png", "jpg", "jpeg", "webp"):
        if os.path.exists(os.path.join(snap_dir, f"{id}.{e}")):
            ext = e
            break
    if not ext:
        return HTMLResponse(f"<h3>Snapshot not found: {id}</h3>", status_code=404)

    img_url = f"/calib/{sid}/snapshots/{id}.{ext}"
    
    HTML_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>图片标定</title>
<style>
  :root{
    --blue:#1677ff; --green:#52c41a; --orange:#fa8c16; --gray:#94a3b8; --text:#1f2937;
  }
  body{font-family:system-ui,-apple-system,'Microsoft YaHei',Arial;background:#f5f6f7;margin:0;padding:24px;color:var(--text)}
  .wrap{max-width:1600px;margin:0 auto;}
  .topbar{display:flex;align-items:center;gap:10px;margin-bottom:16px}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid var(--blue);color:var(--blue);text-decoration:none;background:#fff}
  .pill.secondary{border-color:var(--green);color:var(--green)}
  .pill:hover{filter:brightness(1.05);background:linear-gradient(0deg, rgba(22,119,255,.06), rgba(22,119,255,.06))}
  .pill.secondary:hover{background:linear-gradient(0deg, rgba(82,196,26,.08), rgba(82,196,26,.08))}
  /* 右上角信息区（美化） */
  .meta{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;font-size:12px;color:#fff;box-shadow:0 6px 14px rgba(0,0,0,.12)}
  .chip.sid{background:linear-gradient(135deg,#2b6cb0,#3182ce)}
  .chip.points{background:linear-gradient(135deg,#0ea5e9,#38bdf8)}
  .chip.lens.on{background:linear-gradient(135deg,#16a34a,#4ade80)}
  .chip.lens.off{background:linear-gradient(135deg,#9ca3af,#6b7280)}
  .chip.hint{background:linear-gradient(135deg,#f59e0b,#fbbf24)}
  .chip .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas}
  .board{position:relative;background:#000;border-radius:12px;overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,.08);align-self:start;}
  #img{width:100%;display:block;}
  #overlay{position:absolute;left:0;top:0;pointer-events:none;}
  .layout{display:grid;grid-template-columns:1fr;gap:16px;margin-top:16px;align-items:start}
  /* 右侧信息面板 */
  .panel{background:#fff;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 10px 24px rgba(0,0,0,.06);padding:14px;align-self:start;}
  .panel h3{margin:0 0 10px 0;font-size:14px;color:#111827}
  .progress{height:8px;background:#f1f5f9;border-radius:999px;overflow:hidden;margin:8px 0 12px 0}
  .progress>span{display:block;height:100%;background:linear-gradient(90deg,#60a5fa,#22d3ee)}
  .points-table{width:100%;border-collapse:separate;border-spacing:0 8px;font-size:13px}
  .points-table thead th{font-weight:700;color:#64748b;text-align:left;padding:4px 6px}
  .points-table tbody tr{background:#f8fafc}
  .points-table tbody td{padding:8px 10px}
  .points-table .idx{width:36px;text-align:center;font-weight:700;color:#0ea5e9}
  .pill-mini{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;color:#fff;background:#94a3b8}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .bar{margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .btn{padding:10px 18px;border:none;background:var(--blue);color:#fff;border-radius:10px;cursor:pointer;box-shadow:0 6px 14px rgba(22,119,255,.25)}
  .btn.gray{background:#888;box-shadow:none}
  .btn.red{background:#ff4d4f}
  .btn.outline{background:transparent;color:var(--blue);border:1px solid var(--blue);box-shadow:none}
  .btn.green{background:var(--green);box-shadow:0 6px 14px rgba(82,196,26,.25)}
  input[type="text"]{padding:10px 12px;border:1px solid #d9d9d9;border-radius:10px;width:140px}
  .list{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas;font-size:12px;background:#fff;border:1px solid #eee;border-radius:12px;padding:12px;margin-top:12px;word-break:break-all}
  a.link{color:var(--blue);text-decoration:none}
  /* 放大镜 */
  #loupe{position:fixed;display:none;width:240px;height:240px;border:2px solid var(--blue);border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.35);background:#000;z-index:999;pointer-events:none}
  /* 小屏栅格降级 */
  @media (max-width:1200px){ .layout{grid-template-columns:1fr} }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="pill" href="/portal"> 返回设备列表</a>
    <a class="pill secondary" id="backPlayer" href="/player-flv" style="display:none">↩ 返回视频流</a>
    <div class="meta" id="metaArea">
      <span class="chip sid">🔑 SID：<span class="mono" id="chipSid">-</span></span>
      <span class="chip points">📍 点位：<span id="chipCnt">0</span>/10</span>
      <span class="chip lens off" id="chipLens">🔍 放大镜：关</span>
      <span class="chip hint">💡 单击落点、双击放大镜</span>
    </div>
  </div>

  <div class="layout">
    <div class="board" id="board">
      <img id="img" src="__IMG_URL__" crossorigin="anonymous"/>
      <canvas id="overlay"></canvas>
    </div>

    <div class="panel">
      <h3>标定点位（显示两位小数）</h3>
      <div class="progress"><span id="progBar" style="width:0%"></span></div>
      <table class="points-table">
        <thead>
          <tr id="pointsHeader"><th>#</th></tr>
        </thead>
        <tbody>
          <tr id="rowX"><td>X</td></tr>
          <tr id="rowY"><td>Y</td></tr>
        </tbody>
      </table>


      <div class="grid-2" style="margin-top:10px">
        <div><span class="pill-mini">zoomf</span></div>
        <div style="text-align:right"><span class="pill-mini" id="leftCount">剩余：10</span></div>
      </div>

      <div class="bar" style="margin-top:12px">
        <span>水平：</span><input id="zoomx" type="text" placeholder="如 1.1" value="1.0"/>
        <span>垂直：</span><input id="zoomy" type="text" placeholder="如 1.2" value="1.0"/>
      </div>
      <div class="bar">
        <button class="btn gray" id="undo">撤销</button>
        <button class="btn gray" id="reset">重置</button>
        <button class="btn red" id="save">保存标定</button>
      </div>
    </div>
  </div>

  <div class="list" id="result" style="display:none"></div>
</div>

<canvas id="loupe" width="240" height="240"></canvas>

<script>
  // 工具
  const $ = (id)=>document.getElementById(id);
  function qs(k){ const p=new URLSearchParams(location.search); return p.get(k); }

  // 组件
  const img=$('img');
  const overlay=$('overlay');
  const board=$('board');
  const resultBox=$('result');
  const loupe=$('loupe');
  const headerRow=$('pointsHeader');
  const rowX=$('rowX');
  const rowY=$('rowY');
  const chipCnt=$('chipCnt');
  const chipSid=$('chipSid');
  const chipLens=$('chipLens');
  const leftCount=$('leftCount');
  const progBar=$('progBar');

  // 状态
  const MAX_POINTS=10;
  let points=[];
  let lensOn=false, ZOOM=2; // 放大 2 倍
  let clickTimer=null;      // —— 用于避免双击误加点

  // sid 显示与返回播放链接
  const SID = qs('sid') || localStorage.getItem('fc_sid') || 'public';
  localStorage.setItem('fc_sid', SID);
  chipSid.textContent = SID;
  const src = qs('src');
  if(src){ const a=$('backPlayer'); a.href='/player-flv?sid='+encodeURIComponent(SID)+'&url='+encodeURIComponent(src); a.style.display='inline-flex'; }

  function updateMeta(){
    chipCnt.textContent = String(points.length);
    leftCount.textContent = '剩余：' + (MAX_POINTS - points.length);
    progBar.style.width = (points.length / MAX_POINTS * 100) + '%';
    chipLens.className = 'chip lens ' + (lensOn ? 'on' : 'off');
    chipLens.textContent = '🔍 放大镜：' + (lensOn ? '开' : '关');
  }

  // 绘制：点 + 线（闭合）
  function draw(){
    const rect=img.getBoundingClientRect();
    overlay.width=rect.width; overlay.height=rect.height;
    const ctx=overlay.getContext('2d');
    ctx.clearRect(0,0,overlay.width,overlay.height);

    if(points.length>=2){
      ctx.beginPath();
      for(let i=0;i<points.length;i++){
        const x=points[i][0]*overlay.width, y=points[i][1]*overlay.height;
        if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      }
      if(points.length>=3){
        const x0=points[0][0]*overlay.width, y0=points[0][1]*overlay.height;
        ctx.lineTo(x0,y0);
      }
      ctx.lineWidth=3;
      ctx.strokeStyle='rgba(255,0,0,0.95)';
      ctx.stroke();
    }

    ctx.lineWidth=2; ctx.strokeStyle='red'; ctx.fillStyle='red';
    ctx.font='14px ui-monospace, SFMono-Regular, Menlo, Consolas';
    points.forEach((p,i)=>{
      const x=p[0]*overlay.width, y=p[1]*overlay.height;
      const r=6;
      ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fill();
      ctx.fillText(String(i+1), x+8, y-8);
    });

    // —— 仅显示两位小数（不改变原始 points），渲染到表格 —— 
    renderPointsTable();
    updateMeta();
  }

  function renderPointsTable(){
    const headerRow = document.getElementById('pointsHeader');
    const rowX = document.getElementById('rowX');
    const rowY = document.getElementById('rowY');

    // 清空
    headerRow.innerHTML = '<th>#</th>';
    rowX.innerHTML = '<td>X</td>';
    rowY.innerHTML = '<td>Y</td>';

    for(let i=0;i<MAX_POINTS;i++){
      const p = points[i];
      const idx = i+1;
      headerRow.innerHTML += `<th>${idx}</th>`;
      if(p){
        rowX.innerHTML += `<td>${p[0].toFixed(2)}</td>`;
        rowY.innerHTML += `<td>${p[1].toFixed(2)}</td>`;
      }else{
        rowX.innerHTML += `<td>—</td>`;
        rowY.innerHTML += `<td>—</td>`;
      }
    }
  }


  function pageToPoint(e){
    const rect=img.getBoundingClientRect();
    const x=(e.clientX-rect.left)/rect.width;
    const y=(e.clientY-rect.top)/rect.height;
    return [Math.min(Math.max(x,0),1), Math.min(Math.max(y,0),1)];
  }

  // —— 交互：单击落点（延迟提交，若发生 dblclick 将被取消）——
  board.addEventListener('click', e=>{
    if(points.length>=MAX_POINTS) return;
    const p = pageToPoint(e);
    if(clickTimer) clearTimeout(clickTimer);
    clickTimer = setTimeout(()=>{
      points.push(p);
      clickTimer = null;
      draw();
    }, 220);
  });

  // 撤销 / 重置
  $('undo').onclick=()=>{ points.pop(); draw(); };
  $('reset').onclick=()=>{ points=[]; draw(); };

  // 放大镜：双击开关；移动时跟随（中心可靠边，越界黑色）
  board.addEventListener('dblclick', e=>{
    e.preventDefault();
    if(clickTimer){ clearTimeout(clickTimer); clickTimer=null; } // 取消单击加点
    lensOn = !lensOn;
    loupe.style.display = lensOn ? 'block' : 'none';
    updateMeta();
    if(lensOn) updateLoupe(e);
  });
  board.addEventListener('mousemove', e=>{
    if(lensOn) updateLoupe(e);
  });

  function updateLoupe(e){
    const lw = loupe.width, lh = loupe.height;
    const rect = img.getBoundingClientRect();
    const W = img.naturalWidth, H = img.naturalHeight;
    const scaleX = W / rect.width;
    const scaleY = H / rect.height;
    const cx = (e.clientX - rect.left) * scaleX;
    const cy = (e.clientY - rect.top) * scaleY;

    const sw = lw / ZOOM;
    const sh = lh / ZOOM;
    let sx = cx - sw/2;
    let sy = cy - sh/2;

    loupe.style.left = (e.clientX + 16) + 'px';
    loupe.style.top  = (e.clientY + 16) + 'px';

    const ctx = loupe.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, lw, lh);

    // 与图片相交的真实采样窗口
    let sxc = sx, syc = sy, sWidth = sw, sHeight = sh;
    let dx = 0, dy = 0; // 目标偏移
    const kx = lw / sw;
    const ky = lh / sh;

    if (sxc < 0) { const cut = -sxc; dx += cut * kx; sxc = 0; sWidth -= cut; }
    if (syc < 0) { const cut = -syc; dy += cut * ky; syc = 0; sHeight -= cut; }
    if (sxc + sWidth > W) { const cut = (sxc + sWidth) - W; sWidth -= cut; }
    if (syc + sHeight > H){ const cut = (syc + sHeight) - H; sHeight -= cut; }

    if (sWidth > 0 && sHeight > 0) {
      const dWidth  = sWidth  * kx;
      const dHeight = sHeight * ky;
      try { ctx.drawImage(img, sxc, syc, sWidth, sHeight, dx, dy, dWidth, dHeight); } catch(e){}
    }

    // 十字线
    ctx.strokeStyle='rgba(0,0,0,.95)'; ctx.lineWidth=4;
    ctx.beginPath(); ctx.moveTo(lw/2,12); ctx.lineTo(lw/2,lh-12); ctx.moveTo(12,lh/2); ctx.lineTo(lw-12,lh/2); ctx.stroke();
    ctx.strokeStyle='rgba(255,255,255,.98)'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(lw/2,12); ctx.lineTo(lw/2,lh-12); ctx.moveTo(12,lh/2); ctx.lineTo(lw-12,lh/2); ctx.stroke();
    ctx.beginPath(); ctx.arc(lw/2, lh/2, 7, 0, Math.PI*2); ctx.fillStyle='rgba(255,255,255,.98)'; ctx.fill();
    ctx.lineWidth=2; ctx.strokeStyle='rgba(0,0,0,.9)'; ctx.stroke();
  }

  // 自适应
  window.addEventListener('resize', draw);
  img.onload = draw;

  // 保存（带 sid）—— headers 增加 Accept，失败给出更明确信息（JSON 保留原始精度）
  $('save').onclick = async ()=>{
    if(points.length!==MAX_POINTS){ alert('请标定满 10 个点（当前 '+points.length+'）'); return; }
    const zx=( $('zoomx').value || '1' ).trim();
    const zy=( $('zoomy').value || '1' ).trim();
    const zoomf=`${zx}:${zy}`;
    try{
      const r = await fetch('/api/save-calibration',{
        method:'POST',
        headers:{'Content-Type':'application/json','Accept':'application/json'},
        body: JSON.stringify({ id:'__ID__', zoomf, calibration_points: points, sid: SID })
      }).then(r=>r.json());

      resultBox.style.display='block';
      if(!r.ok){
        resultBox.innerHTML = '<span style="color:#ff4d4f">❌ 保存失败：'+(r.error||'未知错误')+'</span>';
        return;
      }

      const back = src ? `/player-flv?sid=${encodeURIComponent(SID)}&url=${encodeURIComponent(src)}` : `/player-flv?sid=${encodeURIComponent(SID)}`;
      resultBox.innerHTML = `
        <div>✅ 已保存</div>
        <div>JSON：<a class="text">${r.json_url}</a></div>
        <div>标定图：<a class="text">${r.image_url}</a></div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn green" href="${back}">↩ 继续播放视频流</a>
        </div>
      `;
      // 尝试使用 File System Access API（Chrome/Edge 支持，体验最佳）
      async function saveLocallyFS(filename, blob){
        if (!('showSaveFilePicker' in window)) return false;
        try{
          const handle = await window.showSaveFilePicker({
            suggestedName: filename,
            types: [{description: 'Files', accept: {'*/*': ['.json','.png']}}]
          });
          const writable = await handle.createWritable();
          await writable.write(blob);
          await writable.close();
          return true;
        }catch(e){ console.warn(e); return false; }
      }

      async function downloadFallback(filename, blob){
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.style.display = 'none';
        document.body.appendChild(a); a.click();
        setTimeout(()=>{ URL.revokeObjectURL(url); a.remove(); }, 0);
      }

      // 从接口拿到本地保存用的数据
      const jsonInline = r.json_inline;
      const imgDataURL = r.image_data_url;

      // 组装 Blob
      const jsonBlob = new Blob([JSON.stringify(jsonInline, null, 2)], {type:'application/json'});
      const imgBlob  = await (async ()=>{
        const res = await fetch(imgDataURL);   // data:URL -> Blob
        return await res.blob();
      })();

      // 尝试原生文件系统 API；不支持则走下载兜底
      (async ()=>{
        const base = '__ID__';  // 和服务器 id 对齐
        const ok1 = await saveLocallyFS(base + '.json', jsonBlob);
        const ok2 = await saveLocallyFS(base + '.png',  imgBlob);
        if(!ok1) await downloadFallback(base + '.json', jsonBlob);
        if(!ok2) await downloadFallback(base + '.png',  imgBlob);
      })();
    }catch(err){
      console.error(err);
      resultBox.style.display='block';
      resultBox.innerHTML = '<span style="color:#ff4d4f">❌ 保存失败：网络或服务器异常</span>';
    }
  }
</script>
</body>
</html>
    """
    html = HTML_TEMPLATE.replace("__IMG_URL__", img_url).replace("__ID__", id)
    return HTMLResponse(html)


# 保存标定（写 JSON + 生成标定图）——只画线与点；按 sid 隔离；兼容 .png/.jpg 截图
class SaveCalIn(BaseModel):
    id: str
    zoomf: str
    calibration_points: List[List[float]]
    sid: Optional[str] = None
@app.post("/api/save-calibration")
async def save_calibration(payload: SaveCalIn):
    try:
        pts_raw = payload.calibration_points or []
        if len(pts_raw) != 10:
            return {"ok": False, "error": "need 10 points"}

        # 归一化坐标（精度不变）
        pts = []
        for i, p in enumerate(pts_raw):
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                return {"ok": False, "error": f"point[{i}] invalid"}
            x, y = float(p[0]), float(p[1])
            x = 0.0 if x < 0 else 1.0 if x > 1 else x
            y = 0.0 if y < 0 else 1.0 if y > 1 else y
            pts.append([x, y])

        sid = sanitize_sid(payload.sid)
        # 仍需定位已上传的原始截图（只读，不落盘结果）
        _, snap_dir, _, _ = get_user_dirs(sid)

        # 兼容多扩展名，找到快照源图
        candidates = [os.path.join(snap_dir, f"{payload.id}.{e}") for e in ("png","jpg","jpeg","webp")]
        snap_path = next((p for p in candidates if os.path.exists(p)), None)
        if not snap_path:
            return {"ok": False, "error": "snapshot not found"}

        # —— 并发安全：同一 (sid,id) 串行处理（避免同时读改同一张图导致开销抖动）——
        async with get_lock(sid, payload.id):
            # 组装内存 JSON（仅返回给前端，不写服务器文件）
            json_obj = {"zoomf": payload.zoomf, "calibration_points": pts}

            # 生成标注图（仅在内存，返回 dataURL）
            with Image.open(snap_path) as src:
                im = src.convert("RGB")
            W, H = im.size
            draw = ImageDraw.Draw(im)
            try:
                font = ImageFont.truetype("arial.ttf", 22)
            except Exception:
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
                except Exception:
                    font = ImageFont.load_default()

            poly = [(int(x*W), int(y*H)) for x,y in pts]
            if len(poly) >= 2:
                draw.line(poly + [poly[0]], fill=(255,0,0), width=3)
            R = 10
            for i,(x,y) in enumerate(poly):
                draw.ellipse((x-R,y-R,x+R,y+R), outline=(255,0,0), width=3)
                draw.ellipse((x-3,y-3,x+3,y+3), fill=(255,0,0))
                try:
                    draw.text((x+12,y-14), str(i+1), fill=(255,0,0), font=font)
                except Exception:
                    pass

            # 转为 dataURL（PNG）返回
            buf = io.BytesIO()
            im.save(buf, format="JPEG")
            b64_img = base64.b64encode(buf.getvalue()).decode("ascii")
            data_url_image = f"data:image/jpeg;base64,{b64_img}"

        # 仅回传“前端本地保存”所需的数据；不提供服务器 URL
        return {
            "ok": True,
            "id": payload.id,
            "json_inline": json_obj,         # 给前端保存 .json
            "image_data_url": data_url_image,
            "json_url": "json已保存",
            "image_url": "jpeg已保存"
        }

    except Exception as e:
        print("save_calibration error:", traceback.format_exc())
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

# ===================== 示例 API（可留可删） =====================
@app.get("/api/field-calibration")
async def field_calibration():
    return {
        "field": {"name": "足球场", "type": "标准11人制", "dimensions": {"length": 105, "width": 68, "unit": "米"}},
        "status": "已标定"
    }


# ====== 登录与远端设备集成（基于代码2） ======

SESSION_TTL  = 7 * 24 * 3600  # 7 天
_SESSIONS: Dict[str, Dict[str, Any]] = {}   # token -> {"username":..., "exp":...}

def _create_session(username: str) -> str:
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = {"username": username, "exp": time.time() + SESSION_TTL}
    return tok

def _get_user_from_cookie(request: Request) -> Optional[str]:
    tok = request.cookies.get("auth_token")
    if not tok: return None
    sess = _SESSIONS.get(tok)
    if not sess: return None
    if sess["exp"] < time.time():
        _SESSIONS.pop(tok, None)
        return None
    # 滚动续期（可选）
    sess["exp"] = time.time() + SESSION_TTL
    return sess["username"]

def require_login(request: Request) -> str:
    u = _get_user_from_cookie(request)
    if not u:
        raise HTTPException(status_code=401, detail="unauthorized")
    return u



# ====== 远端用户/设备适配（来自代码2） ======
REMOTE_BASE = "http://223.84.144.232:10000"
ADMIN_USERNAME = "admin123"
PLAINTEXT_PASSWORD = "123456"  # 统一密码
PASSWORD_SHA256 = hashlib.sha256(PLAINTEXT_PASSWORD.encode("utf-8")).hexdigest()

def _admin_token() -> str:
    """管理员登录，拿到 Token（代码2同逻辑）"""
    r = requests.get(
        f"{REMOTE_BASE}/api/v1/login",
        params={"username": ADMIN_USERNAME, "password": PASSWORD_SHA256},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["EasyDarwin"]["Body"]["Token"]

@lru_cache(maxsize=1)
def _cached_users_list(_ts_bucket: int) -> set:
    """
    远端用户列表轻量缓存（60 秒刷新一次）。
    调用时传入 int(time.time()//60) 作为 _ts_bucket。
    """
    token = _admin_token()
    res = requests.get(f"{REMOTE_BASE}/users",
                       headers={"Authorization": f"Bearer {token}"},
                       timeout=5)
    res.raise_for_status()
    items = res.json().get("items", [])
    return {it["username"] for it in items}

def remote_users() -> set:
    return _cached_users_list(int(time.time() // 60))

def _user_token(username: str) -> str:
    """普通用户登录，拿到 Token（代码2同逻辑）"""
    r = requests.get(
        f"{REMOTE_BASE}/api/v1/login",
        params={"username": username, "password": PASSWORD_SHA256},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["EasyDarwin"]["Body"]["Token"]

def remote_devices_for_user(username: str) -> List[Dict[str, Any]]:
    """
    返回当前用户名下的设备列表：[{id, name, status}, ...]
    对应代码2：/devices + 用户 Token
    """
    tok = _user_token(username)
    res = requests.get(f"{REMOTE_BASE}/channels",
                       headers={"Authorization": f"Bearer {tok}"},
                       timeout=5)
    res.raise_for_status()
    items = res.json().get("items", [])
    cleaned = []
    for dev in items:
        cid = dev.get("id")
        name = dev.get("name")
        online = bool(dev.get("status", False))
        
        stream_url = ""
        # 仅在线时尝试拿播放地址（离线多数会报错/无效）
        if cid and online:
            try:
                play = requests.post(
                    f"{REMOTE_BASE}/channels/{cid}/play",
                    headers={"Authorization": f"Bearer {tok}"},
                    timeout=5
                )
                play.raise_for_status()
                addr = play.json().get("address", {}) or {}
                stream_url = addr.get("http_flv") or ""
            except Exception:
                stream_url = ""
        
        cleaned.append({
            "id": cid,
            "name": name,
            "status": online,
            "stream_url": stream_url,
        })
    return cleaned


# ========== 登录/退出 ==========
class LoginIn(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def api_login(payload: LoginIn, response: Response):
    # —— 用户名：来自远端（代码2 users 列表）；密码：统一 123456 —— #
    users = remote_users()
    if payload.username not in users:
        return {"ok": False, "error": "用户名不存在"}
    if payload.password != PLAINTEXT_PASSWORD:
        return {"ok": False, "error": "密码错误"}
    tok = _create_session(payload.username)
    response.set_cookie("auth_token", tok, httponly=True, max_age=SESSION_TTL, samesite="lax", path="/")
    return {"ok": True, "user": payload.username}

@app.get("/logout")
async def logout(response: Response, request: Request):
    tok = request.cookies.get("auth_token")
    if tok: _SESSIONS.pop(tok, None)
    response.delete_cookie("auth_token", path="/")
    return HTMLResponse("<h3>已退出</h3><a href='/'>返回登录</a>")

# ========== 登录页面（独立） ==========
@app.get("/", response_class=HTMLResponse)
async def login_page():
    return """
<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"/><title>登录</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xgplayer@3.0.17/dist/index.min.css">
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',Arial;background:#f0f2f5;margin:0;display:grid;place-items:center;height:100vh}
  .card{width:420px;background:#fff;border:1px solid #eee;border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.08);padding:24px}
  h2{margin:0 0 12px 0}
  .row{display:flex;flex-direction:column;gap:10px;margin-top:12px}
  input{padding:12px 14px;border:1px solid #d9d9d9;border-radius:10px}
  .btn{margin-top:12px;padding:12px;border:none;border-radius:10px;background:#1677ff;color:#fff;cursor:pointer}
  .msg{color:#ff4d4f;min-height:20px;margin-top:8px}
</style></head><body>
  <div class="card">
    <h2>账号登录</h2>
    <div>请使用管理员分配的账号密码</div>
    <div class="row">
      <input id="u" placeholder="用户名"/>
      <input id="p" type="password" placeholder="密码"/>
      <button class="btn" onclick="login()">登录</button>
      <div id="m" class="msg"></div>
    </div>
    <div style="margin-top:10px;color:#888;font-size:12px">登录成功后将进入“我的设备”列表</div>
  </div>
<script>
async function login(){
  const u=document.getElementById('u').value.trim();
  const p=document.getElementById('p').value;
  const m=document.getElementById('m');
  m.textContent='';
  if(!u||!p){ m.textContent='请输入用户名和密码'; return; }
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({username:u,password:p})}).then(r=>r.json());
    if(!r.ok){ m.textContent=r.error||'登录失败'; return; }
    location.href='/portal';
  }catch(e){ m.textContent='网络或服务器错误'; }
}
</script>
</body></html>
    """

# ========== 设备列表（直接用远端 status 判在线） ==========
@app.get("/api/devices")
async def list_my_devices(response: Response, curr: str = Depends(require_login)):
    """
    设备列表改为远端透传（代码2）：在线=远端 status。
    不再读取/依赖本地文件、心跳、探测结果。
    """
    try:
        devs = remote_devices_for_user(curr)
    except Exception as e:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return {"ok": False, "error": f"fetch remote devices failed: {type(e).__name__}: {e}"}

    items = []
    for d in devs:
        items.append({
            "device_id": d["id"],
            "online": bool(d.get("status", False)),  # ← 直接用 status
            "last_seen": 0,                          # 无心跳，置 0
            "stream_url": d.get("stream_url") or "", # 远端未给播放地址，置空（按钮会禁用）
            "meta": {"name": d.get("name", "")},     # 展示友好名称
        })

    items.sort(key=lambda x: (not x["online"], x["device_id"]))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return {"ok": True, "devices": items}


# ========== “我的设备”页面 ==========
@app.get("/portal", response_class=HTMLResponse)
async def portal_page(curr: str = Depends(require_login)):
    # 单页应用：拉取 /api/devices 渲染
    return f"""
<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"/><title>我的设备</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',Arial;background:#f0f2f5;margin:0;padding:20px}}
  .wrap{{max-width:1200px;margin:0 auto}}
  .top{{display:flex;gap:10px;align-items:center;margin-bottom:12px}}
  .pill{{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid #1677ff;color:#1677ff;text-decoration:none;background:#fff}}
  .me{{margin-left:auto;color:#666}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}
  .card{{background:#fff;border:1px solid #eee;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.06);padding:14px;display:flex;flex-direction:column;gap:8px}}
  .title{{font-weight:700}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:999px;color:#fff;font-size:12px}}
  .on{{background:#52c41a}}
  .off{{background:#999}}
  .row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
  .btn{{padding:10px 14px;border:none;border-radius:10px;background:#1677ff;color:#fff;cursor:pointer}}
  input{{width:100%;padding:8px 10px;border:1px solid #d9d9d9;border-radius:10px}}
  .empty{{color:#999}}
</style></head><body>
  <div class="wrap">
    <div class="top">
      <a class="pill" href="/">🏠 返回主页</a>
      <a class="pill" href="/logout">退出登录</a>
      <div class="me">当前用户：<b>{curr}</b></div>
    </div>
    <h2 style="margin:6px 0 12px 0">我的设备</h2>
    <div id="list" class="grid"></div>
    <div id="hint" class="empty" style="margin-top:10px"></div>
  </div>
""" + """
<script>
const REFRESH_MS = 30000;   // 刷新间隔
let _timer = null;
const PROBE_TTL = 60;       // 兼容旧逻辑的常量（保留无害）
async function load(){
  const box=document.getElementById('list');
  const hint=document.getElementById('hint');
  box.innerHTML=''; hint.textContent='';
  try{
    const r = await fetch('/api/devices?ts=' + Date.now(), { cache: 'no-store' });
    const data = await r.json();
    if(!data.ok){ hint.textContent='加载失败'; return; }
    const arr = data.devices || [];
    if(arr.length===0){ hint.textContent='暂无设备。请联系管理员为你分配设备。'; return; }

    for(const d of arr){
      const now = Date.now()/1000;
      const po = d.meta?.probe_ok;      // 兼容旧字段
      const pa = d.meta?.probe_at || 0;
      const recent = (now - pa) <= PROBE_TTL;

      // —— 新：优先采用后端 online；没有时回退旧逻辑 —— //
      const online2 = (typeof d.online === 'boolean')
        ? d.online
        : ((po === true) && recent);

      const st = online2 ? '<span class="badge on">在线</span>' : '<span class="badge off">离线</span>';

      const nameLine = d.meta?.name ? `<div style="color:#666">名称：${d.meta.name}</div>` : '';

      const lastBeat = d.last_seen ? new Date(d.last_seen*1000).toLocaleString() : '无';

      const card=document.createElement('div'); card.className='card';
      card.innerHTML = `
        <div class="row">
          <div class="title">${d.device_id}</div>
          ${st}
        </div>
        ${nameLine}
        <div>流地址：</div>
        <input value="${d.stream_url||''}" readonly/>
        <div class="row">
          <button class="btn" ${!d.stream_url?'disabled':''} onclick="openPlayer('${encodeURIComponent(d.stream_url||'')}')">进入播放/标定</button>
        </div>
        <div style="color:#999;font-size:12px">最近心跳：${lastBeat}</div>
      `;
      box.appendChild(card);
    }
  }catch(e){
    hint.textContent='网络错误';
  }
}

function openPlayer(enc){
  if(!enc){ alert('无流地址'); return; }
  const u = '/stream/flv?url='+enc;
  location.href = '/player-flv?url='+encodeURIComponent(u);
}

load();
_timer = setInterval(load, REFRESH_MS);
document.addEventListener('visibilitychange', ()=>{
  if (document.hidden) { clearInterval(_timer); _timer = null; }
  else { load(); if(!_timer) _timer = setInterval(load, REFRESH_MS); }
});
</script>
</body></html>
    """

# ===================== 启动 =====================
if __name__ == "__main__":
    print("\n=== ⚽ 足球场地标定系统（整合远端用户/设备；status 判在线） ===")
    print("http://localhost:8001")
    print("- /player-flv  实时流播放（按钮渐变胶囊），标定弹窗；自动生成 sid")
    print("- /calibrate?id=...&sid=...&src=...  标定页（点位表格两位小数；顶部徽章状态区）")
    print("输出目录：calibration_data/<sid>/{snapshots,annotated,json}\n")

    uvicorn.run(app, host="0.0.0.0", port=8001)
