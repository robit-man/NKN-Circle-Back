#!/usr/bin/env python3
"""
signaller_sidecar.py — Minimal NKN rebroadcast signaller (Node sidecar) with persistent peers.json

• Creates ./sidecar_node/signaller.js (verbatim)
• Ensures npm deps (nkn-sdk) are installed
• Spawns the Node process with NUM_SUB_CLIENTS=12 (min)
• Rotates and restarts on WS flap storms; Python wrapper always restarts on crash
• Persists ./sidecar_node/peers.json (0600) with lastSeen, lastLoc, etc.; loaded on boot

ENV (optional):
  NKN_SEED_HEX      — 64-hex (auto-generate to ./.seed_hex if missing)
  IDENTIFIER        — default "signal"
  NUM_SUB_CLIENTS   — default 12 (minimum enforced)
  SEED_WS_ADDR      — "wss://a:30004,wss://b:30004" (overrides defaults)
  PEER_TTL_MS       — 300000 (5m) → set peer offline/vestigial after idle
  PEER_HARD_TTL_MS  — 21600000 (6h) → prune old vestigial
  ROSTER_PUSH_MS    — 15000 (15s) roster cadence
  PEERS_PATH        — path for peers.json (default ./sidecar_node/peers.json)
"""

import os, sys, time, signal, threading, subprocess, shutil, secrets
from pathlib import Path
from textwrap import dedent

BASE = Path(__file__).resolve().parent
NODE_DIR = BASE / "sidecar_node"
JS_PATH = NODE_DIR / "signaller.js"
PKG_JSON = NODE_DIR / "package.json"
NODE_MODULES = NODE_DIR / "node_modules"
SEED_FILE = BASE / ".seed_hex"
DEFAULT_PEERS_JSON = NODE_DIR / "peers.json"

def which(cmd: str): return shutil.which(cmd)

def ensure_node():
    if not which("node") or not which("npm"):
        print("‼️  Node.js and npm are required. Please install Node 18+.", file=sys.stderr)
        sys.exit(1)

SIGNALLER_JS = dedent(r"""
// signaller.js — NKN MultiClient rebroadcaster + durable roster
// • Node flavor: use client.on('connect')
// • Persists peers.json (0600) and loads on boot
// • Roster: {type:'peers', ts, items:[{pub,last,vestigial,lat?,lon?}]}
// • Flap-storm detector: exit so wrapper restarts

const nkn = require('nkn-sdk');
const fs = require('fs');
const path = require('path');

const SEED_HEX = (process.env.NKN_SEED_HEX || '').trim().toLowerCase().replace(/^0x/,'');
if (!/^[0-9a-f]{64}$/.test(SEED_HEX)) {
  console.error('ERROR: NKN_SEED_HEX (64-hex) is required in environment.');
  process.exit(1);
}
const IDENTIFIER       = process.env.IDENTIFIER || 'signal';
const NUM_SUB_CLIENTS  = Math.max(12, parseInt(process.env.NUM_SUB_CLIENTS || '12', 10) || 12);
const PEER_TTL_MS      = Math.max(30_000, parseInt(process.env.PEER_TTL_MS || '300000', 10) || 300_000);
const PEER_HARD_TTL_MS = Math.max(600_000, parseInt(process.env.PEER_HARD_TTL_MS || '21600000', 10) || 21_600_000);
const ROSTER_PUSH_MS   = Math.max(5_000, parseInt(process.env.ROSTER_PUSH_MS || '15000', 10) || 15000);
const PEERS_PATH       = (process.env.PEERS_PATH && process.env.PEERS_PATH.trim()) || path.join(process.cwd(), 'peers.json');

const DEFAULT_WS_SEEDS = [
  'wss://66-113-14-95.ipv4.nknlabs.io:30004',
  'wss://3-137-144-60.ipv4.nknlabs.io:30004',
  'wss://144-202-102-11.ipv4.nknlabs.io:30004',
];
const ENV_WS = (process.env.SEED_WS_ADDR || '').split(',').map(s=>s.trim()).filter(Boolean);
const SEED_WS_ADDR = ENV_WS.length ? ENV_WS : DEFAULT_WS_SEEDS.slice();

let flapTimes = [];  // timestamps of WS close/connectFailed
const FLAP_WINDOW_MS = 60_000;
const FLAP_MAX = 10; // exit() if > 10 events in 60s → wrapper restarts us

function noteFlap() {
  const now = Date.now();
  flapTimes.push(now);
  while (flapTimes.length && (now - flapTimes[0]) > FLAP_WINDOW_MS) flapTimes.shift();
  if (flapTimes.length >= FLAP_MAX) {
    console.error('[flap-storm] too many WS errors recently — exiting for supervisor restart');
    process.exit(17);
  }
}

function now(){ return Date.now(); }
function pubFromAddr(addr) { const m = /\.([0-9a-f]{64})$/i.exec(addr || ''); return m ? m[1].toLowerCase() : null; }

// Durable peers store
// peers map: pub -> { addr, joinedAt, lastSeen, online, lastLoc:{lat,lon}?, precisionDeg? }
const peers = new Map();

function safeWritePeers() {
  const out = { version: 1, updatedAt: now(), items: {} };
  for (const [pub, meta] of peers.entries()) {
    out.items[pub] = {
      addr: meta.addr || null,
      joinedAt: meta.joinedAt || null,
      lastSeen: meta.lastSeen || null,
      online: !!meta.online,
      lastLoc: meta.lastLoc || null,
      precisionDeg: meta.precisionDeg || null,
    };
  }
  const tmp = PEERS_PATH + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify(out), { mode: 0o600 });
    try { fs.renameSync(tmp, PEERS_PATH); } catch { /* windows */ fs.writeFileSync(PEERS_PATH, JSON.stringify(out)); }
    try { fs.chmodSync(PEERS_PATH, 0o600); } catch {}
  } catch (e) {
    console.warn('[peers.json write warn]', e?.message || e);
  }
}
let persistTimer = null;
function persistSoon() {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(safeWritePeers, 500);
}

function loadPeers() {
  try {
    if (!fs.existsSync(PEERS_PATH)) return;
    const txt = fs.readFileSync(PEERS_PATH, 'utf8');
    const j = JSON.parse(txt);
    if (!j || typeof j !== 'object' || !j.items) return;
    const ts = now();
    for (const [pub, v] of Object.entries(j.items)) {
      const meta = {
        addr: v.addr || null,
        joinedAt: v.joinedAt || ts,
        lastSeen: v.lastSeen || ts,
        online: false, // treat as offline until we hear from them again
        lastLoc: v.lastLoc || null,
        precisionDeg: v.precisionDeg || null,
      };
      peers.set(pub, meta);
    }
    console.log(`[peers.json] loaded ${peers.size} entries from disk`);
  } catch (e) {
    console.warn('[peers.json load warn]', e?.message || e);
  }
}

// Roster helpers
function rosterItems() {
  const t = now();
  const items = [];
  for (const [pub, meta] of peers.entries()) {
    const idle = t - (meta.lastSeen || 0);
    const vestigial = idle >= PEER_TTL_MS;
    const it = { pub, last: meta.lastSeen || 0, vestigial };
    if (meta.lastLoc && typeof meta.lastLoc.lat === 'number' && typeof meta.lastLoc.lon === 'number') {
      it.lat = meta.lastLoc.lat; it.lon = meta.lastLoc.lon;
    }
    items.push(it);
  }
  return items;
}

async function sendRoster(toAddr) {
  const payload = JSON.stringify({ type: 'peers', ts: now(), items: rosterItems() });
  try { await client.send(toAddr, payload, { noReply: true, maxHoldingSeconds: 120 }); }
  catch (e) { console.warn('[roster send warn]', e?.message || e); }
}
async function pushRosterToAll() {
  const payload = JSON.stringify({ type: 'peers', ts: now(), items: rosterItems() });
  const targets = [];
  for (const [, meta] of peers.entries()) if (meta.addr) targets.push(meta.addr);
  await Promise.allSettled(
    targets.map(to => client.send(to, payload, { noReply: true, maxHoldingSeconds: 120 }).catch(()=>{}))
  );
}

function reap() {
  const t = now();
  let pruned = 0;
  for (const [pub, meta] of [...peers.entries()]) {
    const idle = t - (meta.lastSeen || 0);
    meta.online = idle < PEER_TTL_MS;
    if (idle >= PEER_HARD_TTL_MS) { peers.delete(pub); pruned++; }
  }
  if (pruned) { console.log(`[reap] pruned=${pruned}`); persistSoon(); }
}

function ensurePeer(src) {
  const pub = pubFromAddr(src);
  if (!pub) return null;
  let p = peers.get(pub);
  const t = now();
  if (!p) {
    p = { addr: src, joinedAt: t, lastSeen: t, online: true };
    peers.set(pub, p);
    persistSoon();
  } else {
    p.addr = src; p.lastSeen = t; p.online = true;
  }
  return { pub, p };
}

async function broadcastVerbatim(fromPub, payload) {
  const targets = [];
  for (const [pub, meta] of peers.entries()) if (pub !== fromPub && meta.addr) targets.push(meta.addr);
  if (targets.length === 0) return;
  await Promise.allSettled(
    targets.map(to => client.send(to, payload, { noReply: true, maxHoldingSeconds: 120 }).catch(e=>{
      console.warn(`[send warn] ${fromPub} → ${to}: ${e?.message || e}`);
    }))
  );
}

// Health guard: if not ready for too long, exit for wrapper restart
let READY = false;
let lastConnectAt = 0;
let lastAnyActivity = now();

function healthLoop() {
  const t = now();
  if (!READY && (t - lastConnectAt) > 120_000) {
    console.error('[health] not ready for 120s — exiting for supervisor restart');
    process.exit(19);
  }
  if ((t - lastAnyActivity) > 600_000) {
    console.error('[health] no activity for 10m — exiting for supervisor restart');
    process.exit(20);
  }
}
setInterval(healthLoop, 15_000);

loadPeers();

const client = new nkn.MultiClient({
  seed: SEED_HEX,
  identifier: IDENTIFIER,
  numSubClients: NUM_SUB_CLIENTS,
  originalClient: true,
  seedWsAddr: SEED_WS_ADDR.length ? SEED_WS_ADDR : undefined,
  wsConnHeartbeatTimeout: 120000,
  reconnectIntervalMin: 1000,
  reconnectIntervalMax: 10000,
  connectTimeout: 15000,
});

client.on('connect', () => {
  READY = true;
  lastConnectAt = now();
  console.log(`ready ${client.addr} id=${IDENTIFIER} sub=${NUM_SUB_CLIENTS}`);
  // Immediately advertise a roster built from disk (vestigial peers included)
  pushRosterToAll().catch(()=>{});
  // Start periodic roster pushes
  if (typeof globalThis._rosterTimer !== 'number') {
    globalThis._rosterTimer = setInterval(()=> pushRosterToAll().catch(()=>{}), ROSTER_PUSH_MS);
  }
});

client.on('message', async ({ src, payload }) => {
  lastAnyActivity = now();
  const entry = ensurePeer(src);
  if (!entry) return;
  const { pub, p } = entry;

  let text = (typeof payload === 'string') ? payload :
             (Buffer.isBuffer(payload) ? payload.toString('utf8') : null);
  let msg = null;
  if (text && text.trim().startsWith('{')) { try { msg = JSON.parse(text); } catch {} }

  if (msg) {
    const t = String(msg.type || msg.event || '').toLowerCase();
    if (t === 'hb') {
      // heartbeat ack
      const ack = { type: 'hb_ack', ts: now(), t_client: msg.t_client || null };
      try { await client.send(src, JSON.stringify(ack), { noReply: true, maxHoldingSeconds: 120 }); } catch {}
      p.lastSeen = now(); p.online = true; return;
    }
    if (t === 'join') {
      p.lastSeen = now(); p.online = true;
      if (msg.loc && typeof msg.loc.lat==='number' && typeof msg.loc.lon==='number') {
        p.lastLoc = { lat: msg.loc.lat, lon: msg.loc.lon };
        p.precisionDeg = msg?.precision?.deg;
      }
      try {
        await client.send(src, JSON.stringify({ type:'joined', ts: now(), addr: client.addr, prefix: IDENTIFIER }),
                          { noReply: true, maxHoldingSeconds: 120 });
      } catch {}
      await sendRoster(src);
      safeWritePeers();
      await broadcastVerbatim(pub, text);
      return;
    }
    if (t === 'loc') {
      if (msg.loc && typeof msg.loc.lat==='number' && typeof msg.loc.lon==='number') {
        p.lastLoc = { lat: msg.loc.lat, lon: msg.loc.lon };
        p.precisionDeg = msg?.precision?.deg;
      }
      p.lastSeen = now(); p.online = true;
      persistSoon();
      await broadcastVerbatim(pub, text);
      return;
    }
    if (t === 'leave') {
      p.lastSeen = now(); p.online = false;
      persistSoon();
      await broadcastVerbatim(pub, text);
      return;
    }
    // Unknown json → broadcast after join
    await broadcastVerbatim(pub, text);
    return;
  }

  // Non-JSON payloads: only after join
  await broadcastVerbatim(pub, payload);
});

client.on('error', (e) => { console.warn('[nkn error]', e?.message || e); noteFlap(); });
client.on('connectFailed', (e) => { console.warn('[nkn connect failed]', e?.message || e); noteFlap(); });
client.on('willreconnect', () => console.log('[nkn] will reconnect…'));
client.on('close', () => { console.log('[nkn] connection closed'); READY = false; noteFlap(); });

setInterval(()=> {
  // mark vestigial vs online; prune very old
  reap();
}, Math.min(PEER_TTL_MS, 60_000));

// graceful shutdown
function shutdown(code=0){ try{ client.close && client.close(); }catch{} try{ safeWritePeers(); }catch{} setTimeout(()=>process.exit(code), 200); }
process.on('SIGINT',  () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
""").lstrip()

def ensure_js():
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    if not JS_PATH.exists() or JS_PATH.read_text(encoding="utf-8") != SIGNALLER_JS:
        JS_PATH.write_text(SIGNALLER_JS, encoding="utf-8")

def ensure_npm_deps():
    if not PKG_JSON.exists():
        subprocess.check_call(["npm", "init", "-y"], cwd=NODE_DIR)
    need_install = not (NODE_MODULES / "nkn-sdk").exists()
    if need_install:
        subprocess.check_call(["npm", "i", "nkn-sdk@^1.3.6"], cwd=NODE_DIR)

def get_seed_hex() -> str:
    env = os.environ.get("NKN_SEED_HEX", "").strip().lower().replace("0x", "")
    if env and len(env) == 64 and all(c in "0123456789abcdef" for c in env):
        return env
    if SEED_FILE.exists():
        s = SEED_FILE.read_text().strip().lower()
        if len(s) == 64 and all(c in "0123456789abcdef" for c in s):
            return s
    s = secrets.token_hex(32)
    SEED_FILE.write_text(s)
    print(f"→ Generated NKN_SEED_HEX and saved to {SEED_FILE}")
    return s

class Runner:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.stop = False
        self.backoff = 1.0
        self.addr = None

    def env(self):
        e = os.environ.copy()
        e.setdefault("NKN_SEED_HEX", get_seed_hex())
        e.setdefault("IDENTIFIER", os.environ.get("IDENTIFIER", "signal"))
        try:
            n_int = max(12, int(os.environ.get("NUM_SUB_CLIENTS", "12")))
        except Exception:
            n_int = 12
        e["NUM_SUB_CLIENTS"] = str(n_int)
        # Pass-throughs
        if "SEED_WS_ADDR" in os.environ: e["SEED_WS_ADDR"] = os.environ["SEED_WS_ADDR"]
        if "PEER_TTL_MS" in os.environ: e["PEER_TTL_MS"] = os.environ["PEER_TTL_MS"]
        if "PEER_HARD_TTL_MS" in os.environ: e["PEER_HARD_TTL_MS"] = os.environ["PEER_HARD_TTL_MS"]
        if "ROSTER_PUSH_MS" in os.environ: e["ROSTER_PUSH_MS"] = os.environ["ROSTER_PUSH_MS"]
        e.setdefault("PEERS_PATH", str(DEFAULT_PEERS_JSON))
        return e

    def spawn(self):
        with self.lock:
            if self.stop: return
            cmd = ["node", str(JS_PATH)]
            self.proc = subprocess.Popen(
                cmd, cwd=NODE_DIR, env=self.env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )
            self.backoff = 1.0
            threading.Thread(target=self._pump_stdout, daemon=True).start()
            threading.Thread(target=self._pump_stderr, daemon=True).start()
            threading.Thread(target=self._waiter, daemon=True).start()

    def _pump_stdout(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("ready "):
                try:
                    parts = line.split()
                    if len(parts) >= 2: self.addr = parts[1]
                except Exception: pass
            print(f"[signaller] {line}", flush=True)

    def _pump_stderr(self):
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            print(f"[signaller:err] {line.rstrip()}", flush=True)

    def _waiter(self):
        assert self.proc
        code = self.proc.wait()
        if self.stop:
            print(f"signaller exited with code {code}")
            return
        print(f"⚠️ signaller crashed (code {code}); restarting in {self.backoff:.1f}s …")
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, 15.0)
        self.spawn()

    def shutdown(self, *_):
        with self.lock:
            self.stop = True
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except Exception:
                    pass
        print("bye.")

def main():
    ensure_node()
    ensure_js()
    ensure_npm_deps()

    # ensure peers.json parent exists with safe perms
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.umask(0o077)  # best-effort restrictive default for created files
    except Exception:
        pass

    runner = Runner()
    signal.signal(signal.SIGINT, runner.shutdown)
    signal.signal(signal.SIGTERM, runner.shutdown)
    runner.spawn()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.shutdown()

if __name__ == "__main__":
    main()
