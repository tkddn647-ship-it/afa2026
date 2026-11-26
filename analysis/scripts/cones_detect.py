"""Detect blue / yellow traffic cones in the forward video. Writes one row per blob:
frame, t_ms, color, u (centroid x), vb (bottom row), area, w, h.  Only rows below the horizon band are searched."""
import cv2, numpy as np, sys, csv
YMIN=248
RANGES={'blue':((100,25,20),(160,255,72)),'yellow':((18,110,110),(34,255,255))}   # blue cones are dark navy (V<72); asphalt is H~19, S~65, V~85
VK=np.ones((7,1),np.uint8)   # joins the two halves of a cone split by its white band
def detect(fr):
    hsv=cv2.cvtColor(fr[YMIN:],cv2.COLOR_BGR2HSV); out=[]
    for col,(lo,hi) in RANGES.items():
        m=cv2.inRange(hsv,lo,hi); m=cv2.morphologyEx(m,cv2.MORPH_OPEN,np.ones((2,2),np.uint8))
        n,lab,st,cen=cv2.connectedComponentsWithStats(cv2.dilate(m,VK),8)
        for i in range(1,n):
            x,y,w,h,a=st[i]; a=int((m[lab==i]>0).sum()); h-=6
            if a<5 or h<3 or w>3*h+4 or h>8*w+6: continue
            out.append((col,round(cen[i][0],1),y+h-1+YMIN,a,w,h))
    return out
if __name__=='__main__':
    src,dst=sys.argv[1],sys.argv[2]; t0=float(sys.argv[3]) if len(sys.argv)>3 else 0; t1=float(sys.argv[4]) if len(sys.argv)>4 else 1e9
    cap=cv2.VideoCapture(src); cap.set(cv2.CAP_PROP_POS_MSEC,t0*1000); w=csv.writer(open(dst,'w',newline='')); w.writerow(['frame','t_ms','color','u','vb','area','w','h'])
    i=int(round(t0*15)); n=0
    while True:
        ok,fr=cap.read()
        if not ok: break
        t=i*1000/15
        if t/1000>=t1: break
        for r in detect(fr): w.writerow([i,round(t,2),*r]); n+=1
        i+=1
    print('frames to',i,'detections',n)
