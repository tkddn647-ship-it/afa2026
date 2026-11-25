import numpy as np, pandas as pd, glob, os, json, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
BLUE='#2a6fdb'; GREY='#8a8a8a'; C=['#2a6fdb','#e07b39','#3a9e6b','#8a5fbf']
SUS=['20260811_022758','20260811_025402','20260811_071828','20260811_073314','20260811_082807','20260811_084517','20260813_001309']
def load(s):
    o=pd.read_csv(f'clean/{s}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01); G=pd.DataFrame({'t':tg})
    gapt=np.interp(tg,o.t.values[1:],np.diff(o.t.values)); G['ok']=gapt<0.045
    for c in o.columns:
        if c in('t','stm_ms','gap'): continue
        m=o[c].notna().values
        if m.sum()>10: G[c]=np.interp(tg,o.t[m],o[c][m])
    return o,G
def sm(x,n): return pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
def welch(x,ok,n=512):
    w=np.hanning(n); P=[]; 
    for a in range(0,len(x)-n,n//2):
        if ok[a:a+n].mean()<0.9: continue
        seg=x[a:a+n]; seg=seg-seg.mean(); P.append(np.abs(np.fft.rfft(seg*w))**2)
    f=np.fft.rfftfreq(n,0.01); return f,(np.mean(P,axis=0)/(w**2).sum()/100*2 if P else None),len(P)
# ---------------- suspension / vehicle dynamics
rows=[]; allv={k:[] for k in ('logged','restored')}; gg=[]; psd={}
for s in SUS:
    o,G=load(s); mv=(G.vehicle_speed_kmh>15)&G.ok
    ax=sm(G.x_g,20); ay=sm(G.y_g,20)
    r=dict(session=s,moving_s=round(mv.sum()/100),ay_p99=round(np.quantile(np.abs(ay[mv]),.99),2),ax_brake_p99=round(-np.quantile(ax[mv],.01),2),ax_accel_p99=round(np.quantile(ax[mv],.99),2),g_total_p99=round(np.quantile(np.hypot(ax,ay)[mv],.99),2),v_max=round(G.vehicle_speed_kmh[mv].quantile(.999),1))
    for c in ('FL','FR','RL','RR'):
        vl=np.gradient(sm(G['sus_'+c],5),0.01)[mv]; vr=np.gradient(sm(G['sus_'+c+'_inv'],7),0.01)[mv]
        allv['logged'].append(vl); allv['restored'].append(vr)
        r[c+'_v95']=round(np.quantile(np.abs(vr),.95)); r[c+'_v95_logged']=round(np.quantile(np.abs(vl),.95))
    rows.append(r)
    if s=='20260813_001309':
        gg=(-ax[mv][::10],ay[mv][::10])
        modes={'heave':(G.sus_FL_inv+G.sus_FR_inv+G.sus_RL_inv+G.sus_RR_inv)/4,'roll (front L-R)':G.sus_FL_inv-G.sus_FR_inv,'pitch (front-rear)':(G.sus_FL_inv+G.sus_FR_inv-G.sus_RL_inv-G.sus_RR_inv)/2,'heave, as logged':(G.sus_FL+G.sus_FR+G.sus_RL+G.sus_RR)/4}
        okm=mv.values
        for k,x in modes.items(): psd[k]=welch(x.values,okm)
        psd['vertical accel z']=welch(G.z_g.values,okm)
T=pd.DataFrame(rows); pd.set_option('display.width',250); pd.set_option('display.max_columns',40); print(T.to_string(index=False)); T.to_csv('out/vehicle_dynamics.csv',index=False)
vl=np.concatenate(allv['logged']); vr=np.concatenate(allv['restored'])
for nm,v in (('logged',vl),('restored',vr)): print(nm,'damper speed share <25: %.0f%%, 25-100: %.0f%%, >100 mm/s: %.0f%%, p95 %.0f, p99 %.0f'%(100*(np.abs(v)<25).mean(),100*((np.abs(v)>=25)&(np.abs(v)<100)).mean(),100*(np.abs(v)>=100).mean(),np.quantile(np.abs(v),.95),np.quantile(np.abs(v),.99)))
for k,(f,P,n) in psd.items():
    b=(f>=1)&(f<=12); i=np.argmax((P*f**2)[b]) if 'accel' not in k else np.argmax(P[b]); print('PSD',k,'segments',n,'peak (velocity-weighted) at %.1f Hz'%f[b][i], '| top3',[round(x,1) for x in f[b][np.argsort((P*f**2)[b])[-3:]]])
fig,ax_=plt.subplots(1,3,figsize=(14,4.4))
a=ax_[0]; a.scatter(gg[1],gg[0],s=3,color=BLUE,alpha=.25,lw=0); a.set_aspect('equal'); a.set_xlabel('lateral [g]'); a.set_ylabel('longitudinal [g]  (+ accelerating, x sign corrected)'); a.set_title('g-g diagram, 8/13 session'); a.set_xlim(-2,2); a.set_ylim(-2,2)
a=ax_[1]; bins=np.arange(-300,301,10); a.hist(vl,bins=bins,density=True,color=GREY,alpha=.8,label='as logged (firmware EMA)'); a.hist(vr,bins=bins,density=True,histtype='step',color=BLUE,lw=1.6,label='EMA inverted'); a.set_yscale('log'); a.set_xlabel('pot velocity [mm/s]'); a.set_ylabel('density'); a.set_title('Pot velocity distribution, 7 sessions'); a.legend(frameon=False,fontsize=9)
a=ax_[2]
for (k,(f,P,n)),c in zip(psd.items(),C+['#444']):
    if 'accel' in k: continue
    a.loglog(f[1:],P[1:],color=GREY if 'logged' in k else c,ls='--' if 'logged' in k else '-',label=k)
a.set_xlim(0.3,30); a.set_xlabel('frequency [Hz]'); a.set_ylabel('PSD [mm$^2$/Hz]'); a.set_title('Suspension mode spectra, 8/13'); a.legend(frameon=False,fontsize=9)
plt.tight_layout(); plt.savefig('out/fig6_dynamics.png'); plt.close()
