"""Bisect native-frame intervals, preserving repeated appearances and explicit uncertainty."""
from collections import deque
import hashlib
from pathlib import Path


def refine_states(times,observe,known=None,budget=96,identities=None):
    """Midpoints first. Equal endpoint states alone NEVER justify pruning an interval."""
    known=dict(known or {}); calls=0; queue=deque([(0,len(times)-1)]) if times else deque()
    by_image={}
    def inspect(i):
        nonlocal calls
        if i in known: return True
        identity=identities[i] if identities else None
        if identity is not None and identity in by_image:
            known[i]=by_image[identity]; return True
        if calls>=budget: return False
        state=observe(i)
        if state not in (True,False,None): raise ValueError('State must be true, false or unknown')
        known[i]=state; calls+=1
        if identity is not None: by_image[identity]=state
        return True
    while queue:
        lo,hi=queue.popleft()
        if not inspect(lo) or not inspect(hi): break
        if hi-lo<=1: continue
        mid=(lo+hi)//2
        if not inspect(mid): break
        halves=[(lo,mid),(mid,hi)]
        halves.sort(key=lambda pair:known.get(pair[0])==known.get(pair[1]))
        for pair in reversed(halves):
            if known.get(pair[0]) != known.get(pair[1]): queue.appendleft(pair)
            else: queue.append(pair)
    transitions=[]
    for i in range(1,len(times)):
        if i-1 in known and i in known and known[i-1] is not None and known[i] is not None and known[i-1]!=known[i]:
            transitions.append(dict(from_state=known[i-1],to_state=known[i],
                bounds_seconds=[times[i-1],times[i]],seconds=times[i],native_indices=[i-1,i]))
    return dict(calls=calls,complete=len(known)==len(times),checked=len(known),total=len(times),
                observations={str(i):v for i,v in known.items()},transitions=transitions,
                unresolved_indices=[i for i in range(len(times)) if i not in known or known[i] is None])


def extract_interval(video_source,start,end,out):
    """Decode exact source PTS, without synthesizing frames or decimal times."""
    import av
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    frames=[]; origin=None
    with av.open(str(video_source)) as container:
        for frame in container.decode(video=0):
            if frame.pts is None: continue
            absolute=float(frame.pts*frame.time_base)
            if origin is None: origin=absolute
            t=absolute-origin
            if t<start-1e-8: continue
            if t>end+1e-8: break
            image=frame.to_image().convert('RGB')
            path=out/f'{frame.pts}.png'
            if not path.exists(): image.save(path)
            frames.append(dict(seconds=t,path=str(path),pts=frame.pts,
                identity=hashlib.sha256(image.tobytes()).hexdigest()))
    return frames
