"""fig17: UART line errors - loss by inverter state, the single-bit signature, and old vs new parser in simulation."""
import ast, numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'axes.axisbelow':True,'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':10.5})
C1,C2,GR='#2a6fb0','#d9822b','#9a9a9a'
S=pd.read_csv('out/uart_loss_by_state.csv'); I=pd.read_csv('out/uart_integrity.csv'); P=pd.read_csv('out/uart_pbit.csv'); M=pd.read_csv('out/uart_sim.csv')
S['period']=np.where(S.session.str[4:6]=='07','7/28-7/31','8/11-8/13')
order=['contactor open','enabled, standstill','rolling, <5 A','5-40 A','>40 A']
fig,ax=plt.subplots(1,3,figsize=(15,4.6),gridspec_kw=dict(width_ratios=[1.25,0.9,1.1]))
a=ax[0]; w=0.38; x=np.arange(len(order))
for k,(per,col) in enumerate((('7/28-7/31',C1),('8/11-8/13',C2))):
    g=S[S.period==per].groupby('state').apply(lambda G: np.average(G.loss_pct,weights=G.windows)).reindex(order)
    b=a.bar(x+(k-0.5)*w,g.values,w-0.04,color=col,label=f'{per} ({S[S.period==per].session.nunique()} sessions)')
    for xi,v in zip(x+(k-0.5)*w,g.values):
        if np.isfinite(v): a.text(xi,v+1,f'{v:.0f}',ha='center',fontsize=8,color='#333')
a.axhline(5,color=GR,lw=1,ls='--',label='5 % server-filter loss (fixed earlier)')
a.set_xticks(x); a.set_xticklabels(['contactor\nopen','enabled,\nstandstill','rolling,\n<5 A','5-40 A','>40 A'],fontsize=8.5); a.set_ylabel('samples lost, % (2 s windows)'); a.legend(frameon=False,fontsize=8.5,loc='upper left')
a.set_title('Loss grows with inverter current')
# single-bit signature
cnt={}
for f in I.flips:
    for k,v in ast.literal_eval(f).items():
        if k!='other': cnt[int(k)]=cnt.get(int(k),0)+v
a=ax[1]; dg=list(range(1,10)); v=[cnt.get(d,0) for d in dg]; col=[C1 if d in (1,2,4,8) else GR for d in dg]
a.bar(dg,v,color=col,width=0.7)
for d,vv in zip(dg,v): a.text(d,vv+8,str(vv),ha='center',fontsize=8)
a.set_xticks(dg); a.set_xlabel("digit that replaced '0' in a field that is always 0"); a.set_ylabel('rows')
a.set_title("'0' becomes 1, 2, 4, 8: one flipped bit")
a.text(0.97,0.95,"'0'=0x30: bit0->'1', bit1->'2',\nbit2->'4', bit3->'8'",transform=a.transAxes,ha='right',va='top',fontsize=8,color='#444')
# simulation
a=ax[2]
for par,col,ls in (('old (no checksum)',C1,'-'),('new (CRC + field check)',C2,'-')):
    G=M[M.parser==par]; a.plot(G.p*1e4,G.loss_pct,color=col,lw=2,ls=ls,marker='o',ms=4,label=f'{par}: lost')
    if par.startswith('old'): a.plot(G.p*1e4,G.wrong_of_kept_pct,color=col,lw=2,ls='--',marker='s',ms=4,label=f'{par}: kept but wrong')
    else: a.plot(G.p*1e4,G.wrong_of_kept_pct,color=col,lw=2,ls='--',marker='s',ms=4,label=f'{par}: kept but wrong (0)')
q=P[P.session.str[4:6]=='07'].p_bit*1e4; a.axvspan(q.quantile(.1),q.quantile(.9),color='#eee',zorder=0); a.set_ylim(-1.5,46); a.text(q.median(),44.5,'range measured\n7/28-7/31',ha='center',va='top',fontsize=8,color='#666')
a.set_xlabel('bit error rate (per 10 000 bits)'); a.set_ylabel('% of lines'); a.legend(frameon=False,fontsize=7.8,loc='upper left',bbox_to_anchor=(0.0,-0.16),ncol=1); a.set_title("Simulated through the Pi's real parser")
plt.tight_layout(); plt.savefig('out/fig17_uart_errors.png',bbox_inches='tight'); print('ok', cnt)
