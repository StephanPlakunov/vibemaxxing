"""Two turns in one Claude Code process; the Keychain item is replaced between them (no 401)."""
import hashlib, json, os, subprocess, sys, time
EXP, DIR, WAIT = sys.argv[1], sys.argv[2], float(sys.argv[3])
SERVICE = "Claude Code-credentials-" + hashlib.sha256(DIR.encode()).hexdigest()[:8]
def item(tok):
    return json.dumps({"claudeAiOauth": {"accessToken": tok, "expiresAt": int(time.time()*1000)+8*3600*1000,
        "scopes": ["user:inference","user:profile","user:sessions:claude_code","user:mcp_servers"],
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}})
def put(tok):
    subprocess.run(["/usr/bin/security","add-generic-password","-U","-a",os.environ["USER"],"-s",SERVICE,"-w",item(tok)], check=True)
put("fake-at-A-not-a-real-token")
env = {"PATH":"/usr/bin:/bin","HOME":os.environ["HOME"],"USER":os.environ["USER"],"TERM":"dumb",
       "CLAUDE_CONFIG_DIR":DIR,"ANTHROPIC_BASE_URL":"http://127.0.0.1:18911",
       "HTTPS_PROXY":"http://127.0.0.1:18912","HTTP_PROXY":"http://127.0.0.1:18912","NO_PROXY":"127.0.0.1,localhost",
       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1","DISABLE_AUTOUPDATER":"1"}
p = subprocess.Popen(["/Users/stephanplakunov/.local/share/claude/versions/2.1.318","-p","--input-format","stream-json",
     "--output-format","stream-json","--verbose","--debug"], cwd=EXP, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
     stderr=open(os.path.join(EXP,"claude.err"),"w"), text=True)
def turn(text):
    p.stdin.write(json.dumps({"type":"user","message":{"role":"user","content":text}})+"\n"); p.stdin.flush()
    while True:
        line = p.stdout.readline()
        if not line: return "EOF"
        ev = json.loads(line)
        if ev.get("type") == "result": return ev.get("result")
print("turn1:", turn("say pong"), flush=True)
put("fake-at-B-not-a-real-token"); print("swapped at", round(time.time(),3), flush=True)
time.sleep(WAIT)
print("turn2:", turn("say pong again"), flush=True)
p.stdin.close(); p.wait(timeout=30)
subprocess.run(["/usr/bin/security","delete-generic-password","-a",os.environ["USER"],"-s",SERVICE], capture_output=True)
print("item deleted", flush=True)
