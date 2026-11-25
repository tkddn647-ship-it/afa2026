import numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
T=np.load('out/track_0813.npy'); t,x,y,v,psi,sd=T.T
ds=0.5; sg=np.arange(0,sd[-1],ds); tt=np.interp(sg,sd,t); vv=np.interp(sg,sd,v)
r=np.gradient(psi,0.01); kap=np.interp(sg,sd,np.where(v>2.5,r/np.maximum(v,2.5),0.0)); kap=np.clip(kap,-0.5,0.5)
L=898.0; nL=int(L/ds); W=int(250/ds)
def corr(a,b): a=a-a.mean(); b=b-b.mean(); return float(np.dot(a,b)/np.sqrt(np.dot(a,a)*np.dot(b,b)+1e-12))
# reference window: pick s0 where a full lap template repeats best
best=None
for s0 in range(int(100/ds),int(900/ds),int(10/ds)):
    if s0+2*nL>=len(kap): break
    c=corr(kap[s0:s0+nL],kap[s0+nL:s0+2*nL])
    if best is None or c>best[1]: best=(s0,c)
s0=best[0]; print('reference start at s=%.0f m, lap-to-lap curvature corr %.2f'%(s0*ds,best[1]))
starts=[s0]
while True:
    p=starts[-1]+nL
    if p+W>=len(kap): break
    cands=[(corr(kap[starts[0]:starts[0]+W],kap[q:q+W]),q) for q in range(p-int(25/ds),p+int(25/ds)) if q+W<len(kap)]
    if not cands: break
    c,q=max(cands); 
    if c<0.5: print('stop: corr',round(c,2),'at s=',q*ds); break
    starts.append(q)
laps=[]
for a,b in zip(starts[:-1],starts[1:]):
    seg=slice(a,b); laps.append(dict(lap=len(laps)+1,length_m=round((b-a)*ds,1),time_s=round(float(tt[b]-tt[a]),2),v_avg_kmh=round((b-a)*ds/(tt[b]-tt[a])*3.6,1),v_max_kmh=round(float(vv[seg].max()*3.6),1),heading_turns=round(float((np.interp(tt[b],t,psi)-np.interp(tt[a],t,psi))/(2*np.pi)),3)))
np.save('out/lap_starts.npy',np.array([[a*ds,tt[a]] for a in starts])); LP=pd.DataFrame(laps); print(LP.to_string(index=False)); LP.to_csv('out/laps_0813.csv',index=False)
# closed per-lap maps: scale yaw so heading closes to one full turn, spread position residual linearly
fig,ax=plt.subplots(1,2,figsize=(13,5.4),gridspec_kw=dict(width_ratios=[1.25,1]))
fast=LP.time_s.idxmin(); XY=[]
for i,(a,b) in enumerate(zip(starts[:-1],starts[1:])):
    ia,ib=np.searchsorted(t,tt[a]),np.searchsorted(t,tt[b]); dpsi=psi[ia:ib]-psi[ia]; k=(-2*np.pi if dpsi[-1]<0 else 2*np.pi)/dpsi[-1]
    ph=dpsi*k; xx=np.cumsum(v[ia:ib]*np.cos(ph))*0.01; yy=np.cumsum(v[ia:ib]*np.sin(ph))*0.01; w=np.linspace(0,1,len(xx)); xx=xx-w*xx[-1]; yy=yy-w*yy[-1]
    XY.append((xx,yy,v[ia:ib],k))
    if i!=fast: ax[0].plot(xx,yy,color='#c9c9c9',lw=1)
xx,yy,vf,_=XY[fast]; sc=ax[0].scatter(xx[::3],yy[::3],c=vf[::3]*3.6,s=5,cmap='viridis',zorder=3); ax[0].set_aspect('equal'); cb=plt.colorbar(sc,ax=ax[0],fraction=.04); cb.set_label('km/h'); cb.outline.set_visible(False)
ax[0].set_title('Track map without GPS: fastest lap coloured'); ax[0].set_xlabel('m'); ax[0].set_ylabel('m')
ax[1].bar(LP.lap,LP.time_s,color=['#2a6fdb' if i==fast else '#9a9a9a' for i in range(len(LP))]); 
for _,rw in LP.iterrows(): ax[1].text(rw.lap,rw.time_s+0.5,f'{rw.time_s:.1f}',ha='center',fontsize=9)
ax[1].set_ylim(LP.time_s.min()-8,LP.time_s.max()+4); ax[1].set_xlabel('lap'); ax[1].set_ylabel('lap time [s]'); ax[1].set_title('Lap times, 8/13 session'); ax[1].grid(axis='x',visible=False)
plt.tight_layout(); plt.savefig('out/fig8_track.png'); plt.close()
print('yaw scale factors per lap',[round(q[3],3) for q in XY])
