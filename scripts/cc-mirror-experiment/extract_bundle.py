"""Claude Code bundle extraction (read only). usage: python3 -I extract_bundle.py <claude binary> <out.js>
Then: LC_ALL=C tr ";" "\n" < out.js > out-split.js   (the line numbers in docs/BROKER.md are of that file)
"""
# Dumps every NUL-terminated text region that starts with "// @bun" in the binary,
# in file order, each preceded by a marker line with its byte offset.
import sys,mmap
f=open(sys.argv[1],'rb'); m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
out=open(sys.argv[2],'wb'); pos=0; n=0; last_end=-1
while True:
    i=m.find(b'// @bun',pos)
    if i<0: break
    e=m.find(b'\x00',i)
    if i>last_end:
        out.write(b'\n//==== CC-MODULE byte_offset=%d len=%d ====\n'%(i,e-i)); out.write(m[i:e]); n+=1; last_end=e
    pos=i+7
print(n,'modules')
