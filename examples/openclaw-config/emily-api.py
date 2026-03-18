#!/usr/bin/env python3
import json,os,subprocess,time,logging,secrets
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse
API_PORT=int(os.environ.get("EMILY_API_PORT","8901"))
API_TOKEN=os.environ.get("EMILY_API_TOKEN","")
SESSION_ID="emily-home-sensor"
AGENT_ID="emily"
TIMEOUT=60
logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger("emily-api")
def call_emily(message):
    cmd=["openclaw","agent","--agent",AGENT_ID,"--session-id",SESSION_ID,"--message",message,"--json"]
    log.info(f"Calling Emily: '{message[:100]}...'")
    start=time.time()
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=TIMEOUT)
        elapsed=time.time()-start
        if r.returncode!=0:
            return {"error":f"CLI failed: {r.stderr[:200]}","elapsed":elapsed}
        data=json.loads(r.stdout)
        payloads=data.get("result",{}).get("payloads",[])
        text=payloads[0].get("text","") if payloads else ""
        meta=data.get("result",{}).get("meta",{}).get("agentMeta",{})
        log.info(f"Emily replied ({elapsed:.1f}s, {len(text)} chars)")
        return {"text":text.strip(),"elapsed":round(elapsed,2),"model":meta.get("model","unknown"),"tokens":meta.get("usage",{}).get("total",0)}
    except subprocess.TimeoutExpired:
        return {"error":f"Timeout after {TIMEOUT}s"}
    except Exception as e:
        return {"error":str(e)}
class H(BaseHTTPRequestHandler):
    def _json(s,code,data):
        s.send_response(code);s.send_header("Content-Type","application/json; charset=utf-8");s.end_headers()
        s.wfile.write(json.dumps(data,ensure_ascii=False).encode("utf-8"))
    def _auth(s):
        if not API_TOKEN: return True
        a=s.headers.get("Authorization","")
        return a.startswith("Bearer ") and secrets.compare_digest(a[7:],API_TOKEN)
    def do_POST(s):
        if urlparse(s.path).path!="/api/emily/speak": return s._json(404,{"error":"Not found"})
        if not s._auth(): return s._json(401,{"error":"Unauthorized"})
        cl=int(s.headers.get("Content-Length",0))
        if cl==0 or cl>10000: return s._json(400,{"error":"Bad request"})
        try: req=json.loads(s.rfile.read(cl))
        except: return s._json(400,{"error":"Invalid JSON"})
        mode=req.get("mode","pass_by");scene=req.get("scene","pass_by_hello")
        target=req.get("target","family");ts=req.get("time","12:00")
        desc=req.get("description","");hint=req.get("content_hint","")
        parts=[f"[HOME_SENSOR] mode={mode}",f"scene={scene}",f"time={ts}",f"target={target}","format=bilingual(English first, then --- on new line, then Chinese explanation)"]
        if desc: parts.append(f"description={desc}")
        if hint: parts.append(f"content_hint={hint}")
        r=call_emily(" | ".join(parts))
        s._json(500 if "error" in r else 200,r)
    def do_GET(s):
        if urlparse(s.path).path=="/api/emily/health": s._json(200,{"status":"ok"})
        else: s._json(404,{"error":"Use POST /api/emily/speak"})
    def log_message(s,fmt,*a): log.info(f"{s.client_address[0]} - {fmt%a}")
def main():
    global API_TOKEN
    if not API_TOKEN:
        tf=os.path.expanduser("~/.emily-api-token")
        if os.path.exists(tf):
            with open(tf) as f: API_TOKEN=f.read().strip()
        else:
            API_TOKEN=secrets.token_hex(24)
            with open(tf,"w") as f: f.write(API_TOKEN)
            os.chmod(tf,0o600)
    log.info(f"API Token: {API_TOKEN}")
    srv=HTTPServer(("0.0.0.0",API_PORT),H)
    log.info(f"Emily API on 0.0.0.0:{API_PORT}")
    try: srv.serve_forever()
    except KeyboardInterrupt: srv.shutdown()
if __name__=="__main__": main()
