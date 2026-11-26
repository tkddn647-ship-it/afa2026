"""fig16: cone map of course B with the best lap, plus one annotated frame showing how a cone becomes a ground point."""
import numpy as np, pandas as pd, json, cv2, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt

from cones_detect import detect
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
p=json.load(open('out/cone_cam_params.json')); M=json.load(open('out/cone_slam_meta.json')); F=538.0; CX=427.0
C=pd.read_csv('out/cone_map_0813.csv'); T=pd.read_csv('out/cone_traj_0813.csv'); K=pd.read_csv('out/corners_courses.csv'); K=K[K.course=='B']
BL,YL='#2a5db0','#d9a400'
G=T[T.lap==M['ref']].reset_index(drop=True); s=np.r_[0,np.cumsum(np.hypot(np.diff(G.xc),np.diff(G.yc)))]
fig=plt.figure(figsize=(14,7.2)); a=fig.add_axes([0.02,0.04,0.55,0.9]); b=fig.add_axes([0.6,0.33,0.39,0.6])
sc=a.scatter(G.xc,G.yc,c=G.v*3.6,s=4,cmap='viridis',zorder=2)
for col,c_ in (('blue',BL),('yellow',YL)):
    q=C[C.color==col]; a.scatter(q.x,q.y,marker='^',s=22,color=c_,edgecolors='#333',linewidths=0.4,zorder=3,label=f'{col} cone ({len(q)})')
a.plot(G.xc[0],G.yc[0],'s',color='#d62728',ms=8,zorder=4); a.annotate('timing line',(G.xc[0],G.yc[0]),(8,-14),textcoords='offset points',color='#d62728',fontweight='bold',fontsize=9)
for _,k in K.iterrows():
    j=min(int(np.searchsorted(s,k.apex_m*s[-1]/898.0)),len(G)-1)
    a.annotate(f'C{int(k.corner)}',(G.xc[j],G.yc[j]),(0,0),textcoords='offset points',ha='center',va='center',fontsize=8,fontweight='bold',color='white',bbox=dict(boxstyle='circle,pad=0.2',fc='#333',lw=0),zorder=5)
a.set_aspect('equal'); a.set_xticks([]); a.set_yticks([]); [a.spines[k].set_visible(False) for k in ('left','bottom')]
a.plot([-70,-50],[-12,-12],color='#333',lw=2); a.text(-60,-10.5,'20 m',ha='center',fontsize=9)
a.legend(loc='upper left',frameon=False,fontsize=9)
q=M['quality']; a.set_title(f'Course B cone map from the onboard camera, best lap {75.14:.2f} s (8/13)')
cb=fig.colorbar(sc,ax=a,fraction=.03,pad=.01,shrink=.6); cb.set_label('km/h'); cb.outline.set_visible(False)
# ---- annotated frame
V='../udp_realtime/camera_recordings/camera-20260813_001309_427966.mp4'; cap=cv2.VideoCapture(V); cap.set(cv2.CAP_PROP_POS_MSEC,520*1000); ok,fr=cap.read()
img=cv2.cvtColor(fr,cv2.COLOR_BGR2RGB); b.imshow(img); b.set_xticks([]); b.set_yticks([]); [b.spines[k].set_visible(False) for k in b.spines]
u=np.array([0,853]); b.plot(u,p['yh']+(u-CX)*p['tilt']/CX,color='#00e676',lw=1.2,ls='--'); b.text(8,p['yh']-p['tilt']-12,'fitted horizon (camera tilt %.1f°)'%np.degrees(np.arctan(p['tilt']/CX)),color='#00e676',fontsize=8)
D=[r for r in detect(fr) if r[2]>=262]
for col,uu,vb,ar,w,h in D:
    yh=p['yh']+(uu-CX)*p['tilt']/CX; dv=vb-yh
    if dv<3: continue
    d=F*p['h']/dv
    if d>15: continue
    b.add_patch(plt.Rectangle((uu-w/2-2,vb-h-2),w+4,h+4,fill=False,ec='#ff1744',lw=1)); b.plot(uu,vb,'o',color='#ff1744',ms=3)
    b.annotate(f'{d:.1f} m',(uu,vb),(6,4),textcoords='offset points',color='white',fontsize=8,fontweight='bold',bbox=dict(fc='black',alpha=.55,lw=0,pad=1))
b.set_title('How a cone becomes a map point (frame at 520 s)')
fig.text(0.6,0.30,'\n'.join([
 'distance d = f·h / (row of cone base − horizon row)',
 f'f = 538 px (lap-closure yaw calibration; CM3 Wide spec in the cropped 1536×864 mode: ~546 px)',
 f'camera height h = {p["h"]:.2f} m, horizon and tilt fitted so one cone tracked over',
 '6+ frames lands on one ground point (median spread %.2f m)'%0.32,
 'laps aligned to the best lap with windowed ICP on same-colour cones',
 'detections within 1 m of a mapped cone: '+', '.join(f'lap {int(k)+1} {v["within_1m"]:.0%}' for k,v in q.items())+' (lap 4 dropped: 3.9 s data gap)',
 f'cones seen in 2+ laps: {len(C)}, cross-lap scatter median {C.lap_scatter_m.median():.2f} m',
 'track width (both cone rows within 7 m): median 4.9 m; yellow row on the left 97%, blue on the right 100%']),fontsize=8.6,va='top',color='#333')
plt.savefig('out/fig16_cone_map.png',bbox_inches='tight'); print('ok')
