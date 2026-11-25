"""Course-level analysis for the two closed courses found by course_all.py.
Left/right: the camera (not mirrored after the .hflipped fix) shows the accelerometer's lateral sign is
+left on 7/31 and -left from 8/11 on (sensor turned 180 deg about vertical), so DAY_SIGN converts y_g to +left."""
import numpy as np, pandas as pd, json, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
COURSES={'A (7/31, ~490 m)':dict(sessions=['20260731_031858','20260731_034542'],main='20260731_034542',day=+1),
         'B (8/11-8/13, ~898 m)':dict(sessions=['20260811_084517','20260813_001309'],main='20260813_001309',day=-1)}
def load(s,day):
    o=pd.read_csv(f'clean/{s}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01); g=lambda c,n=20: sm(np.interp(tg,o.t,md(o[c].values)),n)
    v=g('vehicle_speed_kmh').clip(min=0)/3.6; ay=g('y_g')*9.81; ay-=np.median(ay[v<0.5]) if (v<0.5).sum()>200 else 0; ay*=day
    ax=g('x_g',30)*9.81; ax*=np.sign(np.corrcoef(ax[v>3],np.gradient(sm(v,100),0.01)[v>3])[0,1])
    sd=np.cumsum(v)*0.01; J=json.load(open(f'out/laps_{s}.json')); DS=J['DS']; sg=np.arange(0,sd[-1],DS)
    ti=np.interp(sg,sd,tg); f=lambda x: np.interp(ti,tg,x)
    gap=np.interp(ti,o.t.values[1:],np.diff(o.t.values))
    P=dict(gap=gap,t=ti,v=f(v),ay=f(ay),ax=f(ax),tq=f(g('INV_torque_commanded',10)),P=f(g('bms_voltage')*g('bms_current')),Tb=f(g('bms_temp_maxvalue',100)),Tm=f(g('inv_temp_motor',100)),laps=[[a,b] for a,b in J['laps'] if gap[a:b].max()<=2.5],laps_dropped=sum(1 for a,b in J['laps'] if gap[a:b].max()>2.5),DS=DS)
    return P
out={}; corner_rows=[]; lap_rows=[]
fig_s,axs_s=plt.subplots(2,1,figsize=(12,7.5)); fig_m,axs_m=plt.subplots(1,2,figsize=(13,6))
for ci,(cname,C) in enumerate(COURSES.items()):
    Ps={s:load(s,C['day']) for s in C['sessions']}; M=Ps[C['main']]; N=1000; u=np.linspace(0,1,N,endpoint=False)
    def lapres(P,a,b,key): return np.interp(a+u*(b-a),np.arange(len(P[key])),P[key])
    Lm=np.median([(b-a)*M['DS'] for a,b in M['laps']]); dist=u*Lm
    V=np.array([lapres(M,a,b,'v') for a,b in M['laps']])*3.6; AY=np.array([lapres(M,a,b,'ay') for a,b in M['laps']]); AX=np.array([lapres(M,a,b,'ax') for a,b in M['laps']]); TT=np.array([lapres(M,a,b,'t')-M['t'][a] for a,b in M['laps']])
    lt=TT[:,-1]+(TT[:,-1]-TT[:,-2]); best=int(np.argmin([M['t'][b]-M['t'][a] for a,b in M['laps']]))
    vm=sm(V.mean(axis=0),9); W=int(20/Lm*N)
    ap=[i for i in range(W,N-W) if vm[i]==vm[i-W:i+W+1].min() and (vm[max(0,i-4*W):i].max()-vm[i]>5 and vm[i:i+4*W].max()-vm[i]>5)]
    bounds=[0]+[(ap[k]+ap[k+1])//2 for k in range(len(ap)-1)]+[N]
    seg_t=np.array([[TT[L,min(bounds[k+1],N-1)]-TT[L,bounds[k]] for k in range(len(ap))] for L in range(len(V))])
    seg_t[:,-1]+=np.array([M['t'][b]-M['t'][a] for a,b in M['laps']])-TT[:,-1]   # close the last segment to the timing line
    print('laps dropped for data gaps > 2.5 s:',{s_:P_['laps_dropped'] for s_,P_ in Ps.items()})
    z_=(seg_t-np.median(seg_t,axis=0))/np.maximum(1.4826*np.median(np.abs(seg_t-np.median(seg_t,axis=0)),axis=0),0.05)
    print('segment outliers (|robust z|>3), lap x corner:',[(int(i)+1,int(j)+1,round(float(seg_t[i,j]),2)) for i,j in zip(*np.where(np.abs(z_)>3))])
    if len(V)<10: z_=np.zeros_like(seg_t)
    seg_ok=np.where(np.abs(z_)>3,np.nan,seg_t)
    for k,i in enumerate(ap):
        a_,b_=bounds[k],bounds[k+1]; w=slice(max(0,i-3),i+4)
        R=np.median((V[:,w]/3.6)**2/np.maximum(np.abs(AY[:,w]),0.5)); d='left' if np.mean(AY[:,w])>0 else 'right'
        corner_rows.append(dict(course=cname[0],corner=k+1,apex_m=round(dist[i]),direction=d,radius_m=round(float(R),1),v_entry=round(float(np.median(V[:,a_:i+1].max(axis=1))),1),v_min=round(float(np.median(V[:,i])),1),v_min_best_lap=round(float(V[best,i]),1),v_exit=round(float(np.median(V[:,min(b_,N-1)])),1),ay_peak_g=round(float(np.median(np.abs(AY[:,a_:b_]).max(axis=1)))/9.81,2),brake_peak_g=round(float(np.median(-AX[:,a_:i+1].min(axis=1)))/9.81,2),seg_time_mean=round(float(seg_t[:,k].mean()),2),seg_time_best=round(float(np.nanmin(seg_ok[:,k])),2),seg_time_sd=round(float(seg_t[:,k].std()),2)))
    th=np.nanmin(seg_ok,axis=0).sum(); lap_times=[M['t'][b]-M['t'][a] for a,b in M['laps']]
    print(f'== course {cname}: lap {Lm:.0f} m, {len(M["laps"])} timed laps in {C["main"]}, best {min(lap_times):.2f} s, theoretical best from {len(ap)} corner segments {th:.2f} s')
    # per-lap rows for every session of the course
    for s,P in Ps.items():
        for n,(a,b) in enumerate(P['laps']):
            lap_rows.append(dict(course=cname[0],session=s,lap=n+1,time_s=round(float(P['t'][b]-P['t'][a]),2),v_avg=round(float((b-a)*P['DS']/(P['t'][b]-P['t'][a])*3.6),1),v_max=round(float(P['v'][a:b].max()*3.6),1),energy_Wh=round(float((P['P'][a:b]*np.gradient(P['t'][a:b])).sum()/3600),1),batt_temp_end=round(float(P['Tb'][b]),1),motor_temp_end=round(float(P['Tm'][b]),1),ay_max_g=round(float(np.quantile(np.abs(P['ay'][a:b]),.99)/9.81),2)))
    # ---- map from lateral g / speed (left positive), closed per lap, best lap
    a,b=M['laps'][best]; tt=np.linspace(M['t'][a],M['t'][b],4000); vv=np.interp(tt,M['t'],M['v']); r=np.where(vv>2.5,np.interp(tt,M['t'],M['ay'])/np.maximum(vv,2.5),0)
    psi=np.cumsum(r)*np.diff(tt).mean(); k=2*np.pi*np.sign(psi[-1])/psi[-1]; psi*=k; dt=np.diff(tt).mean()
    x=np.cumsum(vv*np.cos(psi))*dt; y=np.cumsum(vv*np.sin(psi))*dt; w_=np.linspace(0,1,len(x)); x-=w_*x[-1]; y-=w_*y[-1]
    sdist=np.cumsum(vv)*dt; sdist*=Lm/sdist[-1]
    a_=axs_m[ci]; sc=a_.scatter(x,y,c=vv*3.6,s=3,cmap='viridis'); a_.set_aspect('equal'); a_.set_xticks([]); a_.set_yticks([])
    a_.plot(x[0],y[0],'s',color='#d62728',ms=9); a_.annotate('timing line',(x[0],y[0]),(12,-14),textcoords='offset points',color='#d62728',fontsize=9,fontweight='bold')
    for k_,i in enumerate(ap):
        j=int(np.searchsorted(sdist,dist[i])); j=min(j,len(x)-1); a_.annotate(f'C{k_+1}',(x[j],y[j]),(0,0),textcoords='offset points',ha='center',va='center',fontsize=9,fontweight='bold',color='white',bbox=dict(boxstyle='circle,pad=0.25',fc='#333',lw=0))
    a_.set_title(f'Course {cname}: best lap {min(lap_times):.1f} s, {"counter-clockwise" if k>0 else "clockwise"}',fontsize=10)
    cb=plt.colorbar(sc,ax=a_,fraction=.035,pad=.02); cb.set_label('km/h'); cb.outline.set_visible(False)
    out[cname]=dict(lap_m=float(Lm),best=float(min(lap_times)),theoretical=float(th),direction='CCW' if k>0 else 'CW',n=len(lap_times))
    # ---- speed overlay
    a_=axs_s[ci]
    for L in range(len(V)): a_.plot(dist,V[L],color='#2a6fdb' if L==best else '#c4c4c4',lw=1.8 if L==best else .8,zorder=3 if L==best else 1)
    lo_=V.min()-9; a_.set_ylim(lo_,V.max()+4)
    for k_,i in enumerate(ap): a_.text(dist[i],max(V[:,i].min()-5,lo_+2),f'C{k_+1}',ha='center',fontsize=8,fontweight='bold')
    a_.set_ylabel('km/h'); a_.set_title(f'Course {cname}: all {len(V)} laps ({C["main"][4:8]}), best lap in blue'); a_.set_xlim(0,Lm)
axs_s[1].set_xlabel('distance from timing line [m]'); fig_s.tight_layout(); fig_s.savefig('out/fig14_speed_overlay.png'); fig_m.tight_layout(); fig_m.savefig('out/fig13_course_maps.png')
CR=pd.DataFrame(corner_rows); LR=pd.DataFrame(lap_rows); CR.to_csv('out/corners_courses.csv',index=False); LR.to_csv('out/laps_courses.csv',index=False); json.dump(out,open('out/courses.json','w'),indent=1)
pd.set_option('display.width',250); pd.set_option('display.max_rows',200); print(CR.to_string(index=False)); print(out)
# ---- lap time progression
fig,ax=plt.subplots(1,2,figsize=(13,4.3))
for a_,(c,col) in zip(ax,(('A','#2a6fdb'),('B','#2a6fdb'))):
    q=LR[LR.course==c].reset_index(drop=True); x0=0
    for s,qs in q.groupby('session',sort=True):
        xs=np.arange(x0,x0+len(qs)); a_.plot(xs,qs.time_s,'o-',color='#9a9a9a' if s!=COURSES[[k for k in COURSES if k[0]==c][0]]['main'] else col,ms=4,lw=1)
        a_.text(xs.mean(),qs.time_s.min()-1.6,f'{s[4:6]}/{s[6:8]} {s[9:11]}:{s[11:13]}\n{len(qs)} laps',ha='center',va='top',fontsize=8.5,color='#444'); x0+=len(qs)+2
    bi=q.time_s.idxmin(); a_.annotate(f'best {q.time_s.min():.2f} s',(q.index.get_loc(bi) if False else None,None) if False else (0,0),(0,0)) if False else None
    a_.axhline(q.time_s.min(),color='#d62728',lw=.8,ls='--'); a_.text(x0-2,q.time_s.min()+0.15,f'best {q.time_s.min():.2f} s',color='#d62728',fontsize=9,ha='right'); a_.set_ylim(q.time_s.min()-4,q.time_s.max()+1)
    a_.set_xticks([]); a_.set_ylabel('lap time [s]'); a_.set_title(f'Course {c}: lap times in run order')
plt.tight_layout(); plt.savefig('out/fig15_laptimes.png'); plt.close('all')
print(LR.groupby(['course','session']).agg(n=('lap','size'),best=('time_s','min'),median=('time_s','median'),sd=('time_s','std'),Wh=('energy_Wh','median'),Tb_end=('batt_temp_end','max')).round(2).to_string())
