"""Decision-terminal helpers: technical opportunity, semantic states, portfolio risk."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import math
import pandas as pd

@dataclass(frozen=True)
class TechnicalOpportunity:
    ticker: str
    price: float | None
    trend_state: str
    momentum_state: str
    relative_strength_state: str
    volume_state: str
    rsi14: float | None
    adx14: float | None
    rvol20: float | None
    return_5d: float | None
    return_20d: float | None
    rs_spy_20d: float | None
    rs_qqq_20d: float | None
    momentum_acceleration: float | None
    ema20: float | None
    ema50: float | None
    sma200: float | None


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df['High'], df['Low'], df['Close']
    up = high.diff(); down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([(high-low), (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/n, adjust=False).mean().replace(0, float('nan'))
    plus_di = 100 * plus_dm.ewm(alpha=1/n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False).mean() / atr
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, float('nan'))
    return dx.ewm(alpha=1/n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff(); gain=d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean(); loss=(-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain/loss.replace(0, float('nan'))
    return 100-(100/(1+rs))


def technical_opportunity(ticker: str) -> TechnicalOpportunity:
    import yfinance as yf
    tickers = list(dict.fromkeys([ticker.upper(), 'SPY', 'QQQ']))
    raw = yf.download(tickers, period='1y', interval='1d', auto_adjust=True, progress=False, threads=True, group_by='ticker')
    def frame(sym: str) -> pd.DataFrame:
        if isinstance(raw.columns, pd.MultiIndex):
            try: return raw[sym].dropna(how='all').copy()
            except Exception: return pd.DataFrame()
        return raw.copy() if sym == ticker.upper() else pd.DataFrame()
    df=frame(ticker.upper()); spy=frame('SPY'); qqq=frame('QQQ')
    if df.empty or len(df)<30:
        return TechnicalOpportunity(ticker.upper(),None,'UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN',None,None,None,None,None,None,None,None,None,None,None,None)
    c=df['Close'].astype(float); v=df['Volume'].astype(float)
    ema20=c.ewm(span=20,adjust=False).mean(); ema50=c.ewm(span=50,adjust=False).mean(); sma200=c.rolling(200).mean()
    rsi=_rsi(c); adx=_adx(df); rvol=v/v.rolling(20).mean()
    r5=c.pct_change(5)*100; r20=c.pct_change(20)*100
    accel=r5-(r20/4)
    price=float(c.iloc[-1]); e20=float(ema20.iloc[-1]); e50=float(ema50.iloc[-1]); s200=float(sma200.iloc[-1]) if pd.notna(sma200.iloc[-1]) else None
    ax=float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None
    if price>e20>e50 and (s200 is None or e50>s200): trend='STRONG BULL' if (ax or 0)>=25 else 'BULL'
    elif price<e20<e50 and (s200 is None or e50<s200): trend='STRONG BEAR' if (ax or 0)>=25 else 'BEAR'
    else: trend='MIXED'
    a=float(accel.iloc[-1]) if pd.notna(accel.iloc[-1]) else None; m20=float(r20.iloc[-1]) if pd.notna(r20.iloc[-1]) else None
    if a is None: mom='UNKNOWN'
    elif a>1 and (m20 or 0)>0: mom='ACCELERATING BULLISH'
    elif a<-1 and (m20 or 0)<0: mom='ACCELERATING BEARISH'
    elif (m20 or 0)>0: mom='POSITIVE'
    elif (m20 or 0)<0: mom='NEGATIVE'
    else: mom='FLAT'
    def ret20(x): return float(x['Close'].astype(float).pct_change(20).iloc[-1]*100) if len(x)>20 else None
    rs_spy=(m20-ret20(spy)) if m20 is not None and not spy.empty else None
    rs_qqq=(m20-ret20(qqq)) if m20 is not None and not qqq.empty else None
    rsvals=[x for x in [rs_spy,rs_qqq] if x is not None]; rsavg=sum(rsvals)/len(rsvals) if rsvals else None
    rsstate='STRONG' if rsavg is not None and rsavg>=5 else 'POSITIVE' if rsavg is not None and rsavg>=1 else 'WEAK' if rsavg is not None and rsavg<=-5 else 'LAGGING' if rsavg is not None and rsavg<=-1 else 'NEUTRAL'
    rv=float(rvol.iloc[-1]) if pd.notna(rvol.iloc[-1]) else None
    volstate='CONFIRMED' if rv is not None and rv>=1.5 else 'ABOVE AVERAGE' if rv is not None and rv>=1.1 else 'WEAK' if rv is not None and rv<0.8 else 'NORMAL'
    return TechnicalOpportunity(ticker.upper(),price,trend,mom,rsstate,volstate,float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,ax,rv,float(r5.iloc[-1]) if pd.notna(r5.iloc[-1]) else None,m20,rs_spy,rs_qqq,a,e20,e50,s200)


def semantic_signal(score: float | None) -> str:
    if score is None: return 'UNKNOWN'
    if score>=.5: return 'STRONG BULL'
    if score>=.2: return 'BULLISH'
    if score<=-.5: return 'STRONG BEAR'
    if score<=-.2: return 'BEARISH'
    return 'NEUTRAL'

def iv_state(percentile: float | None) -> str:
    if percentile is None: return 'UNKNOWN'
    return 'VERY LOW' if percentile<20 else 'LOW' if percentile<40 else 'NORMAL' if percentile<60 else 'HIGH' if percentile<80 else 'VERY HIGH'

def setup_quality(score, coverage, regime_label, tech: TechnicalOpportunity|None, iv_pct, event_days=None) -> dict[str,Any]:
    direction=semantic_signal(score)
    strengths=[]; risks=[]
    if abs(score or 0)>=.4: strengths.append('Strong directional signal')
    if (coverage or 0)>=.7: strengths.append('High signal coverage')
    elif coverage is not None and coverage<.45: risks.append('Low signal coverage')
    if regime_label and 'RISK_ON' in regime_label and 'BULL' in direction: strengths.append('Market regime supports bullish direction')
    if regime_label and 'RISK_OFF' in regime_label and 'BEAR' in direction: strengths.append('Market regime supports bearish direction')
    if tech:
        if 'STRONG' in tech.trend_state: strengths.append(f'{tech.trend_state.title()} trend')
        if 'ACCELERATING' in tech.momentum_state: strengths.append(tech.momentum_state.title())
        if tech.volume_state=='CONFIRMED': strengths.append('Volume confirms move')
        if tech.relative_strength_state=='STRONG': strengths.append('Strong relative strength')
    if iv_pct is not None and iv_pct<40: strengths.append('Relatively inexpensive IV')
    if iv_pct is not None and iv_pct>80: risks.append('Very high IV / rich premium')
    if event_days is not None and event_days<=7: risks.append(f'Event/earnings risk in {event_days}d')
    points=min(100,max(0,50 + 35*abs(score or 0) + 10*((coverage or .5)-.5) + 4*len(strengths)-5*len(risks)))
    label='HIGH' if points>=75 else 'MODERATE' if points>=55 else 'LOW'
    return {'score':round(points), 'label':label, 'strengths':strengths[:4], 'risks':risks[:4]}
