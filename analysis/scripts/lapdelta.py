import numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
BLUE='#2a6fdb'; GREY='#8a8a8a'
T=np.load('out/track_0813.npy'); t,x,y,v,psi,sd=T.T; S=np.load('out/lap_starts.npy'); s0=S[:,0]
N=900; u=np.linspace(0,1,N+1); laps=[]
for a,b in zip(s0[:-1],s0[1:]):
    sg=a+u*(b-a); tl=np.interp(sg,sd,t); vl=np.interp(sg,sd,v)*3.6; laps.append((tl-tl[0],vl))
times=np.array([l[0][-1] for l in laps]); fast=int(times.argmin()); slow=int(times.argmax())
nsec=9; edges=np.linspace(0,N,nsec+1).astype(int); sec=np.array([[l[0][edges[k+1]]-l[0][edges[k]] for k in range(nsec)] for l in laps])
SEC=pd.DataFrame(sec.round(2),columns=[f'S{k+1}' for k in range(nsec)],index=[f'lap{i+1}' for i in range(len(laps))]); SEC['lap_time']=times.round(2); print(SEC.to_string())
best=sec.min(axis=0); print('best sectors',best.round(2),'-> theoretical best lap %.2f s vs actual best %.2f s (gap %.2f)'%(best.sum(),times.min(),times.min()-best.sum()))
print('fastest lap',fast+1,'slowest',slow+1,'| sector deltas slow-fast',(sec[slow]-sec[fast]).round(2))
print('sector time std across laps',sec.std(axis=0).round(2))
# apexes on fastest lap
vf=pd.Series(laps[fast][1]).rolling(15,center=True,min_periods=1).mean().values
ap=[i for i in range(20,N-20) if vf[i]==vf[i-20:i+21].min() and vf[i]<vf.mean()]
AP=pd.DataFrame([dict(dist_m=round(i/N*(s0[fast+1]-s0[fast])),v_fast=round(vf[i],1),v_other_mean=round(np.mean([l[1][max(0,i-10):i+11].min() for j,l in enumerate(laps) if j!=fast]),1),v_spread=round(np.ptp([l[1][max(0,i-10):i+11].min() for l in laps]),1)) for i in ap]); print(AP.to_string(index=False))
SEC.to_csv('out/lap_sectors_0813.csv'); AP.to_csv('out/lap_apex_0813.csv',index=False)
d=u*np.mean(np.diff(s0))
fig,ax=plt.subplots(2,1,figsize=(12,6.6),sharex=True,gridspec_kw=dict(height_ratios=[1.5,1]))
ax[0].plot(d,laps[slow][1],color=GREY,lw=1.4,label=f'lap {slow+1} (slowest, {times[slow]:.1f} s)'); ax[0].plot(d,laps[fast][1],color=BLUE,lw=1.8,label=f'lap {fast+1} (fastest, {times[fast]:.1f} s)'); ax[0].set_ylabel('speed [km/h]'); ax[0].legend(frameon=False,ncol=2,loc='lower right',bbox_to_anchor=(1,1)); ax[0].set_title('Fastest vs slowest lap, 8/13')
dl=laps[slow][0]-laps[fast][0]; ax[1].plot(d,dl,color='#222',lw=1.6); ax[1].axhline(0,color=GREY,lw=.8); ax[1].set_ylabel('time lost by slowest lap [s]'); ax[1].set_xlabel('distance along lap [m]')
for k in range(1,nsec): 
    for a in ax: a.axvline(d[edges[k]],color='#dddddd',lw=.8,zorder=0)
plt.tight_layout(); plt.savefig('out/fig9_lapdelta.png')
