"""Frame-to-frame image roll from the vertical shift difference between the left and right halves of the horizon band."""
import cv2, numpy as np, sys, csv
src,dst=sys.argv[1],sys.argv[2]; Y0,Y1=170,310
cap=cv2.VideoCapture(src); w=csv.writer(open(dst,'w',newline='')); w.writerow(['frame','t_ms','dxL','dyL','rL','dxR','dyR','rR'])
prev=None; i=0; win=None
while True:
    ok,fr=cap.read()
    if not ok: break
    t=cap.get(cv2.CAP_PROP_POS_MSEC); g=cv2.cvtColor(fr[Y0:Y1,:],cv2.COLOR_BGR2GRAY).astype(np.float32); h=g.shape[1]//2
    L,R=g[:,:h],g[:,h:2*h]
    if win is None: win=cv2.createHanningWindow((h,g.shape[0]),cv2.CV_32F)
    if prev is not None:
        (a,b),c=cv2.phaseCorrelate(prev[0],L,win); (d,e),f=cv2.phaseCorrelate(prev[1],R,win)
        w.writerow([i,round(t,2),round(a,3),round(b,3),round(c,3),round(d,3),round(e,3),round(f,3)])
    prev=(L.copy(),R.copy()); i+=1
print(src[-24:],i)
