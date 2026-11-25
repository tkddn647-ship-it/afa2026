import numpy as np, pandas as pd, json, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from common import *
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
# ================= A. camera as yaw-rate sensor, 8/13, self-calibrated by lap closure
sid='20260813_001309_427966'; LAG=1.67
d,_=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]; tw0=d.t_wall.iloc[0]
y=pd.read_csv('out/yaw_0813_001309.csv'); tc=video_t0(sid)+y.t_ms.values/1000+LAG-tw0      # clean-file time of each frame
dx=y.dx.values.copy(); bad=(np.abs(dx)>80)|(y.resp.values<0.03); dxm=pd.Series(np.where(bad,np.nan,dx)).interpolate(limit=8).fillna(0).values
print('frames',len(dx),'rejected %.1f%%'%(100*bad.mean()))
S=np.load('out/lap_starts.npy'); tl=S[:,1]; px=[dxm[(tc>=a)&(tc<b)].sum() for a,b in zip(tl[:-1],tl[1:])]
print('pixels of horizontal image motion per lap:',np.round(px,0),'CV %.1f%%'%(100*np.std(px)/abs(np.mean(px))))
good=np.abs(np.array(px)-np.median(px))<0.1*abs(np.median(px)); print('laps used for calibration:',good.astype(int),'CV %.1f%%'%(100*np.std(np.array(px)[good])/abs(np.mean(np.array(px)[good])))); k=2*np.pi/abs(np.mean(np.array(px)[good])); f=1/k; print('rad per px %.6f -> focal length %.0f px -> horizontal FoV %.1f deg'%(k,f,2*np.degrees(np.arctan(427/f))))
turns=np.array(px)*k/(2*np.pi); print('heading per lap from video (turns):',np.round(np.abs(turns),3),'| from accel/speed: 0.99 1.03 1.07 1.07 1.10 1.16')
# locate where the outlier lap loses heading
for i,(a,b) in enumerate(zip(tl[:-1],tl[1:])):
    if not good[i]:
        for q0 in np.arange(a,b,10):
            mq=(tc>=q0)&(tc<q0+10); print('   lap %d  t=%.0f-%.0f s: video px %.0f, rejected %d, |dx|>60: %d, median resp %.2f'%(i+1,q0-a,q0-a+10,dxm[mq].sum(),bad[mq].sum(),(np.abs(dx[mq])>60).sum(),np.median(y.resp.values[mq])))
rv=-np.sign(np.mean(px))*dxm*k*15.0   # rad/s, sign so that it matches ay/v sign convention below
G=pd.DataFrame({'t':np.arange(0,d.t_wall.iloc[-1]-tw0,0.01)}); tt=d.t_wall.values-tw0
v=sm(np.interp(G.t,tt,d.vehicle_speed_kmh.clip(lower=0))/3.6,20); ay=sm(np.interp(G.t,tt,d.y_g),20)*9.81; ay-=np.median(ay[v<0.5])
r_acc=np.where(v>2.5,ay/np.maximum(v,2.5),0); r_vid=np.interp(G.t,tc,sm(rv,3)); sg=np.sign(np.corrcoef(r_acc[v>5],r_vid[v>5])[0,1]); r_vid*=sg
m=v>5; ksl=np.polyfit(r_vid[m],r_acc[m],1); print('ay/v = %.3f * yaw_video %+.4f, r=%.3f'%(ksl[0],ksl[1],np.corrcoef(r_vid[m],r_acc[m])[0,1]))
for lo,hi in ((5,10),(10,15),(15,25)):
    mm=(v>=lo)&(v<hi)&(np.abs(r_vid)>0.1); print('  speed %d-%d m/s: slope %.3f (n %d)'%(lo,hi,np.polyfit(r_vid[mm],r_acc[mm],1)[0],mm.sum()))
# sideslip-rate proxy
bd=np.degrees(r_acc-r_vid)[m]; print('ay/v - yaw_video: rms %.1f deg/s, p95 |.| %.1f deg/s'%(np.sqrt(np.mean(bd**2)),np.quantile(np.abs(bd),.95)))
# dead-reckoned maps: accel-based vs camera-yaw-based, no per-lap correction
fig,ax=plt.subplots(1,2,figsize=(12.5,5.6))
for a,(r,ttl,col) in zip(ax,((r_acc,'Yaw from lateral g / speed',None),(r_vid,'Yaw from camera (lap-closure calibrated)',None))):
    psi=np.cumsum(r)*0.01; x=np.cumsum(v*np.cos(psi))*0.01; yy=np.cumsum(v*np.sin(psi))*0.01
    i0=np.searchsorted(G.t,tl[0]); i1=np.searchsorted(G.t,tl[-1]); sc=a.scatter(x[i0:i1:4],yy[i0:i1:4],c=(G.t[i0:i1:4]-tl[0]),s=2,cmap='viridis'); a.set_aspect('equal'); a.set_title(ttl); a.set_xlabel('m'); a.set_ylabel('m')
    ends=[np.hypot(x[np.searchsorted(G.t,b)]-x[np.searchsorted(G.t,a_)],yy[np.searchsorted(G.t,b)]-yy[np.searchsorted(G.t,a_)]) for a_,b in zip(tl[:-1],tl[1:])]; print(ttl,'| lap closure error (m):',np.round(ends,0))
cb=plt.colorbar(sc,ax=ax,fraction=.025); cb.set_label('time in run [s]'); cb.outline.set_visible(False)
fig.suptitle('Six laps dead-reckoned without per-lap correction, 8/13',x=.01,ha='left',fontweight='bold',fontsize=11); plt.savefig('out/fig10_camyaw.png',bbox_inches='tight'); plt.close()
pd.DataFrame({'t':np.round(G.t,2),'yaw_rate_cam_dps':np.round(np.degrees(r_vid),2),'yaw_rate_ay_v_dps':np.round(np.degrees(r_acc),2)}).iloc[::4].to_csv('clean/20260813_001309_camyaw.csv.gz',index=False)
# ================= B. ground-flow speed, 7/31 (camera sees road in the top band)
sid='20260731_034542_245924'; LAG=2.02; d,_=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]
y=pd.read_csv('out/yaw_0731_034542.csv'); tv=video_t0(sid)+y.t_ms.values/1000+LAG; vs=np.interp(tv,d.t_wall,sm(d.vehicle_speed_kmh.values,15))
ok=(y.resp>0.03)&(y.dy.abs()<80)
for lo,hi in ((0,10),(10,25),(25,45),(45,95)):
    mm=ok&(vs>=lo)&(vs<hi); print('B ground flow: %d-%d km/h n=%d  corr(dy,speed)=%.2f  median dy %.1f px/frame, valid %.0f%%'%(lo,hi,mm.sum(),np.corrcoef(y.dy[mm],vs[mm])[0,1] if mm.sum()>50 else np.nan,y.dy[mm].median() if mm.sum() else np.nan,100*ok[(vs>=lo)&(vs<hi)].mean()))
# ================= C. video-derived FL as a replacement channel after the failure, 7/31 03:45
g=load_pots(sid); tvp=video_t0(sid)+g.t_ms.values/1000+LAG; tsec=g.t_ms.values/1000
f=lambda c,n=101: np.interp(tvp,d.t_wall,sm(d[c].values,n))
FR=f('FR'); FLlog=f('FL'); ayv=f('y_g'); spd=f('vehicle_speed_kmh'); KMM=0.966
Lv=KMM*sm(g.L_px.values,15); Rv=KMM*sm(g.R_px.values,15)
def slope(yv,m): 
    k,b=np.polyfit(ayv[m],yv[m],1); return round(float(k),1),round(float(np.corrcoef(ayv[m],yv[m])[0,1]),2),int(m.sum())
pre=(tsec<355)&(spd>15); post=(tsec>375)&(spd>15)
hp=lambda x:x-sm(x,151)
kL=np.polyfit(hp(g.L_px.values)[tsec<355],hp(f('FL',15))[tsec<355],1)[0]; kR=np.polyfit(hp(g.R_px.values),hp(f('FR',15)),1)[0]; print('C video scale mm/px: left (pre-failure, vs logged FL) %.3f, right (whole session, vs logged FR) %.3f'%(kL,kR))
Lv=kL*sm(g.L_px.values,15); Rv=kR*sm(g.R_px.values,15)
print('C front roll response [mm/g, r, n frames]')
print('  before failure: logged FL-FR',slope(FLlog-FR,pre),'| video L - logged FR',slope(Lv-FR,pre),'| video L - video R',slope(Lv-Rv,pre))
print('  after failure : logged FL-FR',slope(FLlog-FR,post),'| video L - logged FR',slope(Lv-FR,post),'| video L - video R',slope(Lv-Rv,post))
