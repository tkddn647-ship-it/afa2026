"""Per-frame horizontal image shift of the far-field band (horizon) by phase correlation -> yaw-rate proxy."""
import cv2, numpy as np, sys, csv
src,dst=sys.argv[1],sys.argv[2]; Y0,Y1=(int(sys.argv[3]),int(sys.argv[4])) if len(sys.argv)>4 else (170,310)
cap=cv2.VideoCapture(src); w=csv.writer(open(dst,'w',newline='')); w.writerow(['frame','t_ms','dx','dy','resp'])
prev=None; i=0; win=None
while True:
    ok,fr=cap.read()
    if not ok: break
    t=cap.get(cv2.CAP_PROP_POS_MSEC)
    g=cv2.resize(cv2.cvtColor(fr[Y0:Y1,:],cv2.COLOR_BGR2GRAY),None,fx=0.5,fy=0.5,interpolation=cv2.INTER_AREA).astype(np.float32)
    if win is None: win=cv2.createHanningWindow((g.shape[1],g.shape[0]),cv2.CV_32F)
    if prev is not None:
        (dx,dy),resp=cv2.phaseCorrelate(prev,g,win); w.writerow([i,round(t,2),round(2*dx,3),round(2*dy,3),round(resp,3)])
    prev=g; i+=1
print(src[-28:],'frames',i)
