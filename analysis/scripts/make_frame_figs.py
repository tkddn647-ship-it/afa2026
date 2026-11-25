"""Annotated video frames showing what the trackers measure (for the README / portfolio)."""
import cv2, numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, ConnectionPatch
from common import ROOT
plt.rcParams.update({'font.size':10,'figure.dpi':140,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
V=f'{ROOT}/udp_realtime/camera_recordings'
def frame(path,t):
    cap=cv2.VideoCapture(path); cap.set(cv2.CAP_PROP_POS_MSEC,t*1000); ok,f=cap.read(); ok2,f2=cap.read(); return f,f2
# ---------------- Figure A: potentiometer tracking (7/31, same ROI/thresholds as track_pots.py)
X0,X1,Y0,Y1=315,625,280,350
f,_=frame(f'{V}/camera-20260731_034542_245924.mp4',700.0)
roi=f[Y0:Y1,X0:X1]; hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV); m=cv2.inRange(hsv,(45,90,70),(90,255,255))
n,lab,st,cen=cv2.connectedComponentsWithStats(m,8); bl=sorted([(st[k,4],k) for k in range(1,n) if st[k,4]>=6],reverse=True)[:4]
pts=[]
for a,k in bl:
    ys,xs=np.nonzero(lab==k); w=hsv[:,:,1][ys,xs].astype(float); pts.append(((xs*w).sum()/w.sum()+X0,(ys*w).sum()/w.sum()+Y0,a))
pts.sort(); names=['L outer','L inner','R inner','R outer']
print('markers',[(nm,round(x,1),round(y,1),a) for nm,(x,y,a) in zip(names,pts)])
Lpx=np.hypot(pts[0][0]-pts[1][0],pts[0][1]-pts[1][1]); Rpx=np.hypot(pts[3][0]-pts[2][0],pts[3][1]-pts[2][1]); Cpx=np.hypot(pts[2][0]-pts[1][0],pts[2][1]-pts[1][1])
rgb=cv2.cvtColor(f,cv2.COLOR_BGR2RGB)
fig=plt.figure(figsize=(12,9.2)); gs=fig.add_gridspec(3,2,height_ratios=[1.55,1,1],hspace=.35,wspace=.08)
a0=fig.add_subplot(gs[0,:]); a0.imshow(rgb); a0.add_patch(Rectangle((X0,Y0),X1-X0,Y1-Y0,fill=False,ec='#ffd400',lw=2)); a0.text(X0,Y1+20,f'search window x {X0}-{X1}, y {Y0}-{Y1} px',color='#ffd400',fontsize=9,fontweight='bold',bbox=dict(fc='black',alpha=.55,lw=0,pad=1.5))
a0.set_title('1. Onboard frame, 7/31 03:45 session, t = 700 s (854 x 480)'); a0.axis('off')
z=6; big=cv2.resize(cv2.cvtColor(roi,cv2.COLOR_BGR2RGB),None,fx=z,fy=z,interpolation=cv2.INTER_NEAREST)
a1=fig.add_subplot(gs[1,:]); a1.imshow(big); a1.axis('off'); a1.set_title('2. Zoomed window: green end-caps of both pots are the markers')
for i,(nm,(x,y,a)) in enumerate(zip(names,pts)):
    inner=i in (1,2); a1.plot((x-X0)*z,(y-Y0)*z,'+',color='#ff2d55',ms=16,mew=2.2)
    a1.text((x-X0)*z+(-8 if i in (0,1) else 8),(y-Y0)*z+(95 if inner else -40),f'{nm}\n({x:.1f}, {y:.1f})',color='white',fontsize=8,fontweight='bold',ha='right' if i in (0,1) else 'left',bbox=dict(fc='black',alpha=.6,lw=0,pad=1.5))
for (p,q,L,c) in ((pts[0],pts[1],Lpx,'#00e5ff'),(pts[2],pts[3],Rpx,'#00e5ff'),(pts[1],pts[2],Cpx,'#ffd400')):
    a1.plot([(p[0]-X0)*z,(q[0]-X0)*z],[(p[1]-Y0)*z,(q[1]-Y0)*z],color=c,lw=2.2,ls='-' if c=='#00e5ff' else '--')
    a1.text(((p[0]+q[0])/2-X0)*z,((p[1]+q[1])/2-Y0)*z+(40 if c=='#ffd400' else -45),f'{L:.1f} px',color=c,fontsize=10,fontweight='bold',ha='center',bbox=dict(fc='black',alpha=.6,lw=0,pad=1.5))
a2=fig.add_subplot(gs[2,0]); a2.imshow(cv2.resize(m,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST),cmap='gray'); a2.axis('off'); a2.set_title('3. HSV mask: H 45-90, S >= 90, V >= 70')
a3=fig.add_subplot(gs[2,1]); a3.axis('off'); a3.set_title('4. What is measured')
a3.text(0,.95,'\n'.join([
 f'- 4 largest green blobs, saturation-weighted centroid (sub-pixel)',
 f'- left pot length  = |L outer - L inner|  = {Lpx:.1f} px',
 f'- right pot length = |R outer - R inner| = {Rpx:.1f} px',
 f'- inner gap |L inner - R inner| = {Cpx:.1f} px is fixed on the chassis:',
 f'  frames where it moves > 2 px from its median are rejected (11% dropped)',
 f'- scale: about 1.0-1.2 mm per px, fitted against the logged pot',
 f'- residual vs. log about 2.4 mm RMS; 15 fps limits it to < 7 Hz']),va='top',fontsize=9.5,family='monospace',transform=a3.transAxes)
plt.savefig('out/fig0_pot_tracking.png',bbox_inches='tight'); plt.close()
# ---------------- Figure B: horizon band for yaw and roll (8/13, same band/method as track_yaw.py / track_roll.py)
B0,B1=170,310; f,f2=frame(f'{V}/camera-20260813_001309_427966.mp4',412.0)
g1=cv2.cvtColor(f[B0:B1],cv2.COLOR_BGR2GRAY).astype(np.float32); g2=cv2.cvtColor(f2[B0:B1],cv2.COLOR_BGR2GRAY).astype(np.float32)
win=cv2.createHanningWindow((g1.shape[1],g1.shape[0]),cv2.CV_32F); (dx,dy),resp=cv2.phaseCorrelate(g1,g2,win)
h=g1.shape[1]//2; wh=cv2.createHanningWindow((h,g1.shape[0]),cv2.CV_32F)
(lx,ly),_=cv2.phaseCorrelate(g1[:,:h],g2[:,:h],wh); (rx,ry),_=cv2.phaseCorrelate(g1[:,h:2*h],g2[:,h:2*h],wh)
print('frame pair dx %.2f dy %.2f resp %.2f | left dy %.2f right dy %.2f'%(dx,dy,resp,ly,ry))
K=0.001858; droll=np.degrees(np.arctan2(ry-ly,427.0))
fig,ax=plt.subplots(2,1,figsize=(11,8.2),gridspec_kw=dict(height_ratios=[2.6,1]))
a=ax[0]; a.imshow(cv2.cvtColor(f,cv2.COLOR_BGR2RGB)); a.axis('off')
a.add_patch(Rectangle((0,B0),853,B1-B0,fill=False,ec='#ffd400',lw=2)); a.axvline(h,ymin=1-B1/480,ymax=1-B0/480,color='#00e5ff',lw=1.5,ls='--')
a.text(6,B0-8,f'horizon band, rows {B0}-{B1} px (yaw: whole band)',color='#ffd400',fontsize=9,fontweight='bold')
a.text(h/2,B1+18,'left half',color='#00e5ff',ha='center',fontweight='bold'); a.text(h*1.5,B1+18,'right half',color='#00e5ff',ha='center',fontweight='bold')
a.annotate('',xy=(427+dx*5,240),xytext=(427,240),arrowprops=dict(arrowstyle='->',color='#ff2d55',lw=2.5))
a.text(427,228,f'dx = {dx:+.2f} px/frame (arrow x5)',color='#ff2d55',ha='center',fontweight='bold',bbox=dict(fc='black',alpha=.55,lw=0,pad=1.5))
a.set_title('Front camera (8/13, t = 412 s): yaw and roll are read from the far-field band')
a=ax[1]; a.axis('off')
a.text(0,1,'\n'.join([
 'Yaw rate (track_yaw.py)',
 '  - phase correlation of the band between consecutive frames, Hanning window',
 f'  - this frame pair: dx = {dx:+.2f} px, peak response {resp:.2f}',
 f'  - scale {K:.6f} rad/px from lap closure (one lap = one full turn of heading)',
 f'    -> yaw rate = dx x {K:.6f} x 15 fps = {np.degrees(dx*K*15):+.1f} deg/s;  focal length ~ 538 px, FoV ~ 77 deg',
 '',
 'Roll rate (track_roll.py)',
 f'  - the same correlation on each half separately: dy left {ly:+.2f} px, dy right {ry:+.2f} px',
 f'  - roll step = atan((dy_right - dy_left) / 427 px) = {droll:+.3f} deg this frame',
 '  - regressed on d(lateral g)/dt with yaw coupling -> 1.2-1.3 deg/g']),va='top',fontsize=9.5,family='monospace',transform=a.transAxes)
plt.tight_layout(); plt.savefig('out/fig0_horizon_tracking.png',bbox_inches='tight'); plt.close()
