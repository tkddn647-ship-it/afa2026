import numpy as np, pandas as pd, glob, os, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from common import *
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','grid.linewidth':.8,'axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold'})
BLUE='#2a6fdb'; GREY='#6b6b6b'; LIGHT='#b5b5b5'
def prep(sid):
    d,info=load_sensor(sid); g=load_pots(sid); t0=video_t0(sid); tv=t0+g.t_ms.values/1000
    d=d[np.r_[True,np.diff(d.t_wall.values)>0]]; tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01)
    S={c:np.convolve(np.interp(tg,d.t_wall,d[c]),np.ones(7)/7,'same') for c in SUS}
    return d,g,tv,tg,S,t0
# ---------- Fig 1: sync
sid='20260731_034542_245924'; LAG=2.02
d,g,tv,tg,S,t0=prep(sid); tt=g.t_ms.values/1000
VR=hp(g.R_px.values,75); VL=hp(g.L_px.values,75)
lags=np.arange(-6,6.001,0.02); cc=[np.corrcoef(hp(np.interp(tv+l,tg,S['FR']),75),VR)[0,1] for l in lags]
x=hp(np.interp(tv+LAG,tg,S['FR']),75); k=np.dot(x,VR)/np.dot(VR,VR)
m=(tt>=130)&(tt<170)
fig,ax=plt.subplots(3,1,figsize=(10,8.6))
ax[0].plot(lags,cc,color=BLUE,lw=2); ax[0].axvline(0,color=LIGHT,ls=':'); ax[0].annotate(f'peak r = {max(cc):.2f} at +{LAG:.2f} s',(LAG,max(cc)),(LAG+.5,max(cc)-.12),color='#222')
ax[0].annotate(f'r = {cc[len(lags)//2]:.2f} with timestamps as logged',(0,cc[len(lags)//2]),(-5.6,.35),color=GREY,arrowprops=dict(arrowstyle='-',color=LIGHT))
ax[0].set_title('Video and sensor log line up only after shifting the log by 2.0 s'); ax[0].set_xlabel('shift applied to sensor log [s]'); ax[0].set_ylabel('correlation')
for a,(lab,lg) in zip(ax[1:],(('As logged',0),(f'Log shifted by {LAG:.2f} s',LAG))):
    a.plot(tt[m],hp(np.interp(tv+lg,tg,S['FR']),75)[m],color=GREY,lw=1.6,label='logged FR channel'); a.plot(tt[m],k*VR[m],color=BLUE,lw=1.6,label='front-right pot tracked in video')
    a.set_title(lab); a.set_ylabel('displacement [mm, detrended]'); a.legend(loc='lower right',bbox_to_anchor=(1,1.0),frameon=False,ncol=2)
ax[2].set_xlabel('video time [s]'); plt.tight_layout(); plt.savefig('out/fig1_sync.png'); plt.close()
# ---------- Fig 2: FL failure
d['tv']=d.t_wall-t0-LAG; w=d[(d.tv>300)&(d.tv<430)]; mm=(tt>300)&(tt<430)
kL=k; base=np.median(d[(d.tv>300)&(d.tv<360)].FL)
vl=g.L_px.values; vbase=np.median(vl[(tt>300)&(tt<360)])
fig,ax=plt.subplots(figsize=(10,4.2))
ax.plot(w.tv,w.FL-base,color=GREY,lw=1.1,label='logged FL channel'); ax.plot(tt[mm],kL*(vl[mm]-vbase),color=BLUE,lw=1.6,label='front-left pot tracked in video')
ax.axvline(367,color='#222',ls='--',lw=.9); ax.text(371,-34,'after this point the logged FL sits 30-50 mm higher;\nthe sensor seen in the video keeps moving normally',color='#222')
ax.set_title('FL channel fails about 6 min 7 s into the 7/31 03:45 session'); ax.set_xlabel('video time [s]'); ax.set_ylabel('displacement [mm, vs. 300-360 s median]'); ax.legend(loc='upper left',frameon=False)
plt.tight_layout(); plt.savefig('out/fig2_fl_failure.png'); plt.close()
print('FL jump: pre mean',base,'post median',np.median(d[(d.tv>372)&(d.tv<430)].FL))
# ---------- Fig 3: channel timeline heat table
rows=[]
for f in sorted(glob.glob(ROOT+'/udp_logger/logs/sensor_log_202607[23]*.csv.gz')+glob.glob(ROOT+'/udp_logger/logs/sensor_log_202608*.csv.gz')):
    s=os.path.basename(f)[11:-7]
    try: dd,_=load_sensor(s)
    except Exception: continue
    sm=dd[SUS+['y_g','inv_motor_speed']].rolling(50,center=True,min_periods=10).mean(); mv=sm[sm.inv_motor_speed.abs()>500]
    if len(mv)<3000: continue
    rows.append([s[4:8]+' '+s[9:11]+':'+s[11:13]]+[mv[c].corr(mv.y_g) for c in SUS]+[dd[c].std() for c in SUS])
T=pd.DataFrame(rows,columns=['s']+SUS+[c+'_sd' for c in SUS]); T.to_csv('out/channel_timeline.csv',index=False)
M=T[SUS].values
fig,ax=plt.subplots(figsize=(6.4,9.6)); ax.grid(False)
im=ax.imshow(M,cmap='RdBu_r',vmin=-1,vmax=1,aspect='auto')
for i in range(M.shape[0]):
    for j in range(4):
        dead=T.iloc[i,5+j]<1.0
        ax.text(j,i,'dead' if dead else f'{M[i,j]:+.2f}',ha='center',va='center',fontsize=9,color='#222' if abs(M[i,j])<.6 else 'white',style='italic' if dead else 'normal')
ax.set_xticks(range(4)); ax.set_xticklabels(SUS); ax.xaxis.tick_top(); ax.set_yticks(range(len(T))); ax.set_yticklabels(T.s,fontsize=9)
for sp in ax.spines.values(): sp.set_visible(False)
for y0 in [i-.5 for i in range(1,len(T)) if T.s[i][:4]!=T.s[i-1][:4]]: ax.axhline(y0,color='white',lw=4)
fig.suptitle('Channel vs lateral acceleration\ncorrelation by session (UTC)',x=.02,ha='left',fontsize=11,fontweight='bold'); cb=plt.colorbar(im,ax=ax,fraction=.04,pad=.03); cb.outline.set_visible(False)
plt.tight_layout(rect=(0,0,1,.97)); plt.savefig('out/fig3_channel_timeline.png'); plt.close()
# ---------- Fig 4: roll response 8/13 as labelled vs corrected
sid='20260813_001309_427966'; dd,_=load_sensor(sid); dd=dd[np.r_[True,np.diff(dd.t_wall.values)>0]]
tg2=np.arange(dd.t_wall.iloc[0],dd.t_wall.iloc[-1],0.01)
U=pd.DataFrame({c:np.interp(tg2,dd.t_wall,dd[c]) for c in SUS+['y_g','vehicle_speed_kmh']}).rolling(100,center=True,min_periods=50).mean()
U=U[(U.vehicle_speed_kmh>15)].iloc[::25].dropna()
fig,ax=plt.subplots(1,2,figsize=(10,4.4),sharey=True)
for a,(y,ttl,col) in zip(ax,((U.FL-U.FR,'As labelled: FL - FR',GREY),(U.RR-U.FR,'Corrected: (RR label) - FR',BLUE))):
    y=y-y.median(); kk,b=np.polyfit(U.y_g,y,1); r=np.corrcoef(U.y_g,y)[0,1]
    a.scatter(U.y_g,y,s=5,color=col,alpha=.35,lw=0); xs=np.array([-1.4,1.4]); a.plot(xs,kk*xs+b,color='#222',lw=1.2)
    a.set_title(ttl); a.set_xlabel('lateral acceleration [g]'); a.text(.04,.06,f'slope {kk:+.1f} mm/g, r = {r:+.2f}',transform=a.transAxes,color='#222')
ax[0].set_ylabel('front left-right pot difference [mm]'); fig.suptitle('8/13 session: front roll response appears only after swapping the FL and RR labels',x=.01,ha='left',fontweight='bold',fontsize=11)
plt.tight_layout(); plt.savefig('out/fig4_roll.png'); plt.close()
print(T.round(2).to_string())
