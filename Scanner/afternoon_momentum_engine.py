from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, List
import numpy as np
import pandas as pd

@dataclass
class PrefilterResult:
    qualified: bool = False
    passed: bool = False
    reason: str = ""
    latest_price: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    window_move_pct: float = 0.0
    pre_window_move_pct: float = 0.0
    ret_15m_pct: float = 0.0
    ret_30m_pct: float = 0.0
    window_turnover_cr: float = 0.0
    volume_acceleration: float = 0.0
    distance_from_window_high_pct: float = 0.0
    bars_in_window: int = 0
    positive_bar_fraction: float = 0.0
    reasons: List[str] = None
    warnings: List[str] = None
    hard_rejects: List[str] = None
    def __post_init__(self):
        self.reasons = [] if self.reasons is None else self.reasons
        self.warnings = [] if self.warnings is None else self.warnings
        self.hard_rejects = [] if self.hard_rejects is None else self.hard_rejects
    def to_dict(self): return asdict(self)

@dataclass
class DailyFeatures:
    rsi14: float = float('nan'); atr14: float = 0.0; atr_pct: float = 0.0
    ret_2d_pct: float = 0.0; ret_5d_pct: float = 0.0; resistance: float = 0.0
    room_to_resistance_pct: float = 0.0; daily_rvol20: float = 0.0
    above_ema20: bool = False; above_ema50: bool = False; ema20_above_ema50: bool = False
    ema20: float = 0.0; ema50: float = 0.0; latest_close: float = 0.0
    breakout_20d_pct: float = 0.0; prior_2d_return_pct: float = 0.0; prior_5d_return_pct: float = 0.0
    def to_dict(self):
        d=asdict(self); d['daily_rsi']=d['rsi14']; return d

@dataclass
class ContinuationStats:
    sample_size: int = 0; hit_5_pct: float = float('nan'); hit_8_pct: float = float('nan')
    hit_10_pct: float = float('nan'); hit_15_pct: float = float('nan'); hit_20_pct: float = float('nan'); p75_mfe_pct: float = float('nan')
    def to_dict(self): return asdict(self)

def clean_ohlcv(df):
    if df is None or len(df)==0: return pd.DataFrame()
    out=df.copy()
    if isinstance(out.columns,pd.MultiIndex):
        cols=[]
        for c in out.columns:
            parts=[str(x).strip() for x in c if str(x).strip()]
            known={'open','high','low','close','adj_close','adjclose','volume','date','datetime','timestamp'}
            pick=next((x.lower().replace(' ','_') for x in parts if x.lower().replace(' ','_') in known), parts[-1].lower() if parts else '')
            cols.append(pick)
        out.columns=cols
    else: out.columns=[str(c).strip().lower().replace(' ','_') for c in out.columns]
    out=out.rename(columns={'adj_close':'close','adjclose':'close','datetime':'date','timestamp':'date'})
    for col in ['open','high','low','close','volume']:
        matches=[i for i,c in enumerate(out.columns) if c==col]
        if not matches: out[col]=np.nan
        elif len(matches)>1:
            s=out.iloc[:,matches[0]]
            if isinstance(s,pd.DataFrame): s=s.iloc[:,0]
            out[col]=s
            out=out.iloc[:,[i for i in range(len(out.columns)) if i not in matches[1:]]]
        s=out[col]
        if isinstance(s,pd.DataFrame): s=s.iloc[:,0]
        out[col]=pd.to_numeric(s,errors='coerce')
    if 'date' in out.columns:
        idx=pd.to_datetime(out['date'],errors='coerce'); out=out.drop(columns=['date']); out.index=idx
    elif not isinstance(out.index,pd.DatetimeIndex): out.index=pd.to_datetime(out.index,errors='coerce')
    if isinstance(out.index,pd.DatetimeIndex) and out.index.tz is not None: out.index=out.index.tz_convert('Asia/Kolkata')
    return out[~out.index.isna()].sort_index().dropna(subset=['close'])

def _mask(index,start='14:00',end='15:10'):
    st=pd.Timestamp(start).time(); et=pd.Timestamp(end).time(); return np.array([(x.time()>=st and x.time()<=et) for x in index])

def compute_intraday_prefilter(df, now=None, config=None, **kwargs):
    d=clean_ohlcv(df)
    if d.empty:return PrefilterResult(reason='no intraday data')
    start=getattr(config,'window_start','14:00') if config else '14:00'; end=getattr(config,'window_end','15:10') if config else '15:10'
    min_move=float(getattr(config,'min_window_move_pct',2.0) if config else 2.0); max_move=float(getattr(config,'max_window_move_pct',8.5) if config else 8.5)
    min_turn=float(getattr(config,'min_window_turnover_cr',0.60) if config else 0.60); min_bars=int(getattr(config,'min_bars_in_window',2) if config else 2); min_pos=float(getattr(config,'min_positive_bar_fraction',0.55) if config else 0.55); max_pre=float(getattr(config,'max_pre_window_move_pct',2.75) if config else 2.75)
    m=_mask(d.index,start,end); w=d.loc[m]; before=d.loc[~m]
    if now is not None and getattr(now,'tzinfo',None) is not None and getattr(w.index,'tz',None) is not None: w=w[w.index<=now]
    if len(w)<min_bars:return PrefilterResult(reason=f'only {len(w)} bars in 2PM window',bars_in_window=len(w))
    baseline=float(before.iloc[-1].close) if not before.empty else float(w.iloc[0].open); first=float(w.iloc[0].open); latest=float(w.iloc[-1].close)
    move=(latest/first-1)*100 if first else 0; pre=(first/baseline-1)*100 if baseline else 0; turn=float((w.close*w.volume).sum()/1e7); pos=float((w.close>w.open).mean())
    prior=before.volume.tail(10); accel=float(w.volume.tail(min(3,len(w))).mean()/prior.mean()) if len(prior) and prior.mean()>0 else 1.0; high=float(w.high.max()); dist=(high-latest)/high*100 if high else 0
    ret15=float((w.close.iloc[-1]/w.close.iloc[-4]-1)*100) if len(w)>=4 else 0; ret30=float((w.close.iloc[-1]/w.close.iloc[-7]-1)*100) if len(w)>=7 else 0
    reasons=[]
    if move<min_move:reasons.append(f'2PM move {move:.2f}% < {min_move:.2f}%')
    if move>max_move:reasons.append(f'2PM move {move:.2f}% already too extended')
    if pre>max_pre:reasons.append(f'pre-2PM move {pre:.2f}% > {max_pre:.2f}%')
    if turn<min_turn:reasons.append(f'window turnover {turn:.2f}Cr < {min_turn:.2f}Cr')
    if pos<min_pos:reasons.append('weak positive-bar fraction')
    return PrefilterResult(not reasons,not reasons,'; '.join(reasons) if reasons else 'prefilter passed',latest,float(d.high.max()),float(d.low.min()),move,pre,ret15,ret30,turn,accel,dist,len(w),pos,reasons,[],[])

def compute_same_window_rvol(intraday_df,*args,**kwargs):
    d=clean_ohlcv(intraday_df)
    if d.empty:return 0.0
    cfg=kwargs.get('config') or next((x for x in args if hasattr(x,'window_start')),None); start=getattr(cfg,'window_start','14:00') if cfg else '14:00'; end=getattr(cfg,'window_end','15:10') if cfg else '15:10'
    m=_mask(d.index,start,end); w=d.loc[m]; dates=sorted(set(d.index.date)); hist=[]
    for day in dates[:-1][-10:]:
        x=d.loc[d.index.date==day]; xm=_mask(x.index,start,end); v=float(x.loc[xm,'volume'].sum());
        if v>0:hist.append(v)
    return float(w.volume.sum()/np.median(hist)) if not w.empty and hist else 0.0

def compute_intraday_rsi(df,period=14,**kwargs):
    d=clean_ohlcv(df)
    if len(d)<period+1:return 50.0
    delta=d.close.diff(); gain=delta.clip(lower=0).rolling(period).mean(); loss=(-delta.clip(upper=0)).rolling(period).mean(); rs=gain/loss.replace(0,np.nan); v=(100-100/(1+rs)).iloc[-1]
    return float(v) if pd.notna(v) else 50.0

def compute_daily_features(daily_df,current_price=None,current_day_high=None,current_day_low=None,current_date=None,config=None,*args,**kwargs):
    d=clean_ohlcv(daily_df)
    if d.empty:return DailyFeatures()
    c,h,l,v=d.close,d.high,d.low,d.volume; ema20=c.ewm(span=20,adjust=False).mean(); ema50=c.ewm(span=50,adjust=False).mean(); tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1); atr=tr.rolling(14).mean(); latest=float(current_price or c.iloc[-1]); atrv=float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0
    ret2=float(c.iloc[-1]/c.iloc[-3]-1)*100 if len(c)>=3 else 0; ret5=float(c.iloc[-1]/c.iloc[-6]-1)*100 if len(c)>=6 else 0; prior20=h.iloc[-21:-1] if len(h)>=21 else h.iloc[:-1]; resistance=float(prior20.max()) if len(prior20) else latest; room=(resistance/latest-1)*100 if latest else 0; vol20=float(v.iloc[-21:-1].mean()) if len(v)>20 else float(v.iloc[:-1].mean()) if len(v)>1 else 0; rv=float(v.iloc[-1]/vol20) if vol20 else 0; breakout=(latest/resistance-1)*100 if resistance else 0
    return DailyFeatures(compute_intraday_rsi(d,14),atrv,atrv/latest*100 if latest else 0,ret2,ret5,resistance,room,rv,latest>float(ema20.iloc[-1]),latest>float(ema50.iloc[-1]),float(ema20.iloc[-1])>float(ema50.iloc[-1]),float(ema20.iloc[-1]),float(ema50.iloc[-1]),latest,breakout,ret2,ret5)

def build_risk_levels(latest_price,*args,**kwargs):
    cfg=kwargs.get('config'); daily=next((x for x in args if isinstance(x,DailyFeatures)),None); price=float(latest_price); atr=float(getattr(daily,'atr14',0) if daily else 0); mult=float(getattr(cfg,'stop_atr_multiple',1.15) if cfg else 1.15); buf=float(getattr(cfg,'breakout_stop_buffer_pct',1.25) if cfg else 1.25); stop=min(price-mult*atr if atr>0 else price*(1-buf/100),price*(1-buf/100)); risk=max(price-stop,price*.005); return {'entry':price,'stop_loss':stop,'risk_per_share':risk,'risk_pct':risk/price*100,'target1':price+2*risk,'target2':price+3*risk,'rr_target1':2.0,'rr_target2':3.0}

def _v(o,n,d=0):
    for nme in n:
        v=o.get(nme) if isinstance(o,dict) else getattr(o,nme,None)
        if v is not None:return v
    return d

def score_candidate(candidate=None,*args,**kwargs):
    """Return transparent Momentum, Swing and Final scores.

    Momentum = immediate 2PM acceleration/volume quality.
    Swing = 1-3 session continuation structure/probability/risk.
    Catalyst is included in the final score but is kept separate in the report.
    This is a ranking score, not a probability forecast.
    """
    pre=candidate
    rvol=float(args[0] if len(args)>0 else _v(pre,['same_window_rvol'],0) or 0)
    irsi=float(args[1] if len(args)>1 else _v(pre,['intraday_rsi'],50) or 50)
    daily=args[2] if len(args)>2 else None
    cont=args[3] if len(args)>3 and isinstance(args[3],ContinuationStats) else None
    move=float(_v(pre,['window_move_pct'],0) or 0); premove=float(_v(pre,['pre_window_move_pct'],0) or 0)
    accel=float(_v(pre,['volume_acceleration'],0) or 0); pos=float(_v(pre,['positive_bar_fraction'],0) or 0)
    dist=float(_v(pre,['distance_from_window_high_pct'],99) or 99); turn=float(_v(pre,['window_turnover_cr'],0) or 0)
    room=float(_v(daily,['room_to_resistance_pct'],0) or 0); atrp=float(_v(daily,['atr_pct'],0) or 0)
    ret2=float(_v(daily,['ret_2d_pct'],0) or 0); ret5=float(_v(daily,['ret_5d_pct'],0) or 0)
    ema20=bool(_v(daily,['above_ema20'],False)); ema50=bool(_v(daily,['above_ema50'],False)); trend=bool(_v(daily,['ema20_above_ema50'],False))
    cat=float(kwargs.get('catalyst_score',0) or 0)

    # Momentum: 40 points.
    m_move=min(12.0,max(0.0,move/5.0*12.0))
    m_rvol=min(12.0,max(0.0,(rvol-1.0)/3.0*12.0))
    m_accel=min(8.0,max(0.0,(accel-1.0)/4.0*8.0))
    m_structure=(4.0 if pos>=0.65 else 2.0 if pos>=0.55 else 0.0)+(4.0 if dist<=0.8 else 2.0 if dist<=1.4 else 0.0)
    momentum_score=min(40.0,m_move+m_rvol+m_accel+m_structure)

    # Swing: 45 points. Historical continuation is explicitly represented.
    s_trend=(3.0 if ema20 else 0)+(3.0 if ema50 else 0)+(2.0 if trend else 0)
    s_room=7.0 if room>=8 else 6.0 if room>=6 else 4.0 if room>=4 else 2.0 if room>=2.25 else 0.0
    s_rsi=5.0 if 50<=irsi<=72 else 3.0 if 45<=irsi<50 or 72<irsi<=80 else 1.0 if irsi<85 else 0.0
    s_prior=5.0 if ret2<=5 and ret5<=10 else 3.0 if ret2<=8 and ret5<=15 else 0.0
    h8=float(getattr(cont,'hit_8_pct',float('nan'))) if cont else float('nan')
    h15=float(getattr(cont,'hit_15_pct',float('nan'))) if cont else float('nan')
    mfe=float(getattr(cont,'p75_mfe_pct',float('nan'))) if cont else float('nan')
    s_hist=(5.0 if np.isfinite(h8) and h8>=70 else 3.5 if np.isfinite(h8) and h8>=50 else 1.5 if np.isfinite(h8) and h8>=30 else 0.0)
    s_hist+=4.0 if np.isfinite(h15) and h15>=35 else 2.5 if np.isfinite(h15) and h15>=20 else 0.0
    s_hist+=4.0 if np.isfinite(mfe) and mfe>=10 else 2.0 if np.isfinite(mfe) and mfe>=6 else 0.0
    s_risk=4.0 if atrp<=5 and atrp>=1.25 else 2.0 if atrp<=7 else 0.0
    swing_score=min(45.0,s_trend+s_room+s_rsi+s_prior+s_hist+s_risk)

    catalyst_component=max(-15.0,min(15.0,cat))
    final_score=max(0.0,min(100.0, momentum_score + swing_score + catalyst_component))
    hard=[]
    if premove>5: hard.append('move was materially underway before 2PM')
    if move<=0: hard.append('no positive afternoon move')
    if ret2>10: hard.append('prior 2-day move already extended')
    if ret5>15: hard.append('prior 5-day move already extended')
    if atrp>9: hard.append('ATR too high')
    breakdown=(f'Momentum {momentum_score:.1f}/40: move={m_move:.1f}, rvol={m_rvol:.1f}, accel={m_accel:.1f}, structure={m_structure:.1f}; '
               f'Swing {swing_score:.1f}/45: trend={s_trend:.1f}, room={s_room:.1f}, RSI={s_rsi:.1f}, prior={s_prior:.1f}, history={s_hist:.1f}, risk={s_risk:.1f}; '
               f'Catalyst {catalyst_component:+.1f}/15')
    return {'score':float(final_score),'momentum_score':float(momentum_score),'swing_score':float(swing_score),
            'score_breakdown':breakdown,'rejected':bool(hard),'reasons':[],'warnings':[],'hard_rejects':hard}

def calibrate_historical_continuation(daily_df,setup_date=None,config=None,**kwargs):
    d=clean_ohlcv(daily_df); cfg=config; fwd=int(getattr(cfg,'calibration_forward_days',3) if cfg else 3); ming=float(getattr(cfg,'calibration_event_min_gain_pct',2) if cfg else 2); maxg=float(getattr(cfg,'calibration_event_max_gain_pct',9) if cfg else 9); minrv=float(getattr(cfg,'calibration_min_volume_ratio',1.4) if cfg else 1.4)
    if len(d)<40:return ContinuationStats()
    c=d.close; rv=d.volume/d.volume.rolling(20).mean(); setups=[i for i in range(20,len(d)-fwd) if ming<=float((c.iloc[i]/c.iloc[i-1]-1)*100)<=maxg and pd.notna(rv.iloc[i]) and rv.iloc[i]>=minrv]; outcomes={k:[] for k in (5,8,10,15,20)}; mfe=[]
    for i in setups:
        fut=c.iloc[i+1:i+1+fwd]
        if fut.empty:continue
        m=float((fut.max()/c.iloc[i]-1)*100); mfe.append(m)
        for k in outcomes:outcomes[k].append(m>=k)
    def rate(k):return float(np.mean(outcomes[k])*100) if outcomes[k] else float('nan')
    return ContinuationStats(len(outcomes[5]),rate(5),rate(8),rate(10),rate(15),rate(20),float(np.percentile(mfe,75)) if mfe else float('nan'))
