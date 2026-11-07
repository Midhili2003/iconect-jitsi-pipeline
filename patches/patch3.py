import sys
p = sys.argv[1]
s = open(p).read()
old = '''    for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sg, on_signal)'''
new = '''    for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        if signal.getsignal(sg) != signal.SIG_IGN:      # respect nohup / background jobs
            signal.signal(sg, on_signal)'''
n = s.count(old)
if n != 1:
    sys.exit(f"PATCH FAILED: expected 1 match, found {n}")
open(p, "w").write(s.replace(old, new))
print("patched OK")
