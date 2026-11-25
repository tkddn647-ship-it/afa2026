"""Corner-by-corner driving analysis, 8/13 (6 laps). Distance axis from lap detection; signals from the clean file.
x_g sign is inverted in this session (checked against d(speed)/dt), so longitudinal g is negated here."""
import numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
o=pd.read_csv('clean/20260813_001309.csv.gz'); T=np.load('out/track_0813.npy'); t,x,y,v,psi,sd=T.T; S=np.load('out/lap_starts.npy'); s0,ts=S[:,0],S[:,1]
g=lambda c,n=20: sm(np.interp(t,o.t,md(o[c].values)),n)
ax=-g('x_g'); ay=g('y_g'); tq=g('INV_torque_commanded',10); st=g('steering_angle',10)
ax-=np.median(ax[v<0.3]); ay-=np.median(ay[v<0.3])
N=1800; u=np.linspace(0,1,N+1); Lm=np.mean(np.diff(s0)); dgrid=u*Lm
laps=[]
for a,b in zip(s0[:-1],s0[1:]):
    sg=a+u*(b-a); ti=np.interp(sg,sd,t)
    laps.append(dict(t=ti-ti[0],v=np.interp(ti,t,v)*3.6,ax=np.interp(ti,t,ax),ay=np.interp(ti,t,ay),tq=np.interp(ti,t,tq)))
SH=int(np.argmax(sm(np.mean([l['v'] for l in laps],axis=0),31)))
for l in laps:
    for k_ in ('v','ax','ay','tq'): l[k_]=np.roll(l[k_][:-1],-SH)
    tt_=l['t'][:-1]; tt_=np.r_[tt_[SH:]-tt_[SH],tt_[:SH]+(l['t'][-1]-tt_[SH])]; l['t']=tt_
N=N; u=u[:-1]; dgrid=dgrid[:-1]; N=len(u)
print('lap reference moved to the fastest point: +%.0f m'%(SH/1800*Lm))
nL=len(laps); V=np.array([l['v'] for l in laps]); AY=np.array([l['ay'] for l in laps]); AX=np.array([l['ax'] for l in laps]); TQ=np.array([l['tq'] for l in laps]); TT=np.array([l['t'] for l in laps])
gmax=np.quantile(np.hypot(AX,AY),0.99); print('combined g, 99th pct over all laps: %.2f g'%gmax)
# corners: local minima of mean speed profile with clear dips
vm=sm(V.mean(axis=0),9); W=int(25/Lm*N)
apex=[i for i in range(W,N-W) if vm[i]==vm[i-W:i+W+1].min() and (vm[max(0,i-3*W):i].max()-vm[i]>6 or vm[i:i+3*W].max()-vm[i]>6)]
print('apex candidates (m):',[round(dgrid[i]) for i in apex])
rows=[]; fast=int(TT[:,-1].argmin())
bounds=[0]+[ (apex[k]+apex[k+1])//2 for k in range(len(apex)-1)]+[N]
for k,i in enumerate(apex):
    a,b=bounds[k],bounds[k+1]
    for L in range(nL):
        vv=V[L]; seg=slice(a,b); j=a+int(np.argmin(vv[seg]))
        # braking point: last index before apex where long. decel crosses -0.25 g going backwards from apex
        jb=j
        while jb>a and AX[L,jb]<-0.15: jb-=1
        while jb>a and AX[L,jb]>-0.15 and j-jb<int(80/Lm*N): jb-=1
        brk=jb if AX[L,jb]<=-0.15 else None
        # throttle-on: first index after apex where torque command > 20 Nm
        tmax=TQ[L,j:b].max(); on=next((q for q in range(j,b) if TQ[L,q]>0.6*tmax),None) if tmax>40 else None
        use=np.hypot(AX[L,seg],AY[L,seg]); 
        rows.append(dict(corner=k+1,lap=L+1,apex_m=round(dgrid[j]),v_min=round(vv[j],1),v_entry=round(vv[a],1),v_exit=round(vv[b-1],1),throttle_on_after_apex_m=None if on is None else round(dgrid[on]-dgrid[j]),ay_peak=round(float(np.max(np.abs(AY[L,seg]))),2),friction_use_mean=round(float(np.mean(use[vv[seg]>15])/gmax),2) if (vv[seg]>15).sum()>5 else None,time_s=round(float(TT[L,b-1]-TT[L,a]),2),v_entry_peak=round(float(vv[a:j+1].max()),1)))
C=pd.DataFrame(rows); C.to_csv('out/corners_0813.csv',index=False)
summ=C.groupby('corner').agg(apex_m=('apex_m','median'),v_min_mean=('v_min','mean'),v_min_sd=('v_min','std'),time_mean=('time_s','mean'),time_sd=('time_s','std'),time_best=('time_s','min'),thr_on_m=('throttle_on_after_apex_m','median'),thr_on_sd=('throttle_on_after_apex_m','std'),ay_peak=('ay_peak','median'),use=('friction_use_mean','median'))
summ['gain_if_best_s']=summ.time_mean-summ.time_best
fl=C[C.lap==fast+1].set_index('corner'); summ['fast_lap_time']=fl.time_s; summ['fast_minus_mean']=summ.fast_lap_time-summ.time_mean
pd.set_option('display.width',240); print(summ.round(2).to_string()); summ.to_csv('out/corner_summary_0813.csv')
print('sum of (mean - best) per corner: %.2f s'%summ.gain_if_best_s.sum())
# correlation between min speed and corner time across laps, per corner
for k,q in C.groupby('corner'): print('corner',k,'corr(v_min, time) %.2f'%np.corrcoef(q.v_min,q.time_s)[0,1],' corr(throttle_on_dist, time) %s'%(round(np.corrcoef(q.throttle_on_after_apex_m.astype(float),q.time_s)[0,1],2) if q.throttle_on_after_apex_m.notna().sum()>=5 else 'n/a'))
# figure: friction circle use and per-corner time spread
fig,axs=plt.subplots(1,2,figsize=(13,4.8),gridspec_kw=dict(width_ratios=[1.35,1]))
a=axs[0]; idx=np.arange(len(summ)); a.bar(idx,summ.gain_if_best_s,color='#2a6fdb'); a.set_xticks(idx); a.set_xticklabels([f'C{k}\n{int(m)} m' for k,m in zip(summ.index,summ.apex_m)],fontsize=8)
for i,val in zip(idx,summ.gain_if_best_s): a.text(i,val+0.01,f'{val:.2f}',ha='center',fontsize=8)
a.set_ylabel('mean corner time - best [s]'); a.set_title('Where lap time varies most, 8/13 (6 laps)'); a.grid(axis='x',visible=False)
a=axs[1]; th=np.linspace(0,2*np.pi,200)
a.scatter(AY[fast][::4],AX[fast][::4],s=3,color='#2a6fdb',alpha=.5,label=f'fastest lap {fast+1}'); a.plot(gmax*np.cos(th),gmax*np.sin(th),color='#999',lw=1,label=f'99th pct combined g ({gmax:.2f} g)')
a.set_aspect('equal'); a.set_xlim(-1.7,1.7); a.set_ylim(-1.3,1.3); a.set_xlabel('lateral [g]'); a.set_ylabel('longitudinal [g] (+ accel)'); a.set_title('Friction circle, fastest lap'); a.legend(frameon=False,fontsize=8,loc='lower right')
plt.tight_layout(); plt.savefig('out/fig12_corners.png'); plt.close()
