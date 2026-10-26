"""Independent historical paper replay of Bot-bin: 20 USDT-M pairs, 10x, 10% margin.

Data: Binance Vision official USDT-M 1-minute Kline archives and recorded historical
funding-rate JSON from Binance USDT-M API. No accounts, orders, or API keys.

Engine: original calculate_signals, calculate_position_size, passes_fee_gate,
detect_market_regime imported intact. Signal decisions at 15-minute candle closes;
1-minute OHLC used for price exits, stop-first on same-bar TP/SL ambiguity.
This is NOT an exact 10-second live execution reconstruction.
"""
import csv
import hashlib
import io
import json
import os
import sys
import time
import traceback
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# importing this script through 'python backtests/foo.py' requires repo in PYTHONPATH
import config
from modules.state import state, state_lock
from modules.indicators import calc_atr
from modules.signals import calculate_signals
from modules.regime_detector import detect_market_regime
from modules.position_sizer import calculate_position_size
from modules.trade_manager import round_qty, symbol_rules
from modules.fee_guardian import passes_fee_gate

SYMBOLS = tuple(config.ACTIVE_PAIRS)
assert len(SYMBOLS)==20 and len(set(SYMBOLS))==20 and config.LEVERAGE==10 and abs(config.POSITION_MARGIN_FRACTION-0.10)<1e-12
START = pd.Timestamp('2026-09-26T00:00:00Z')
END = pd.Timestamp('2026-10-26T00:00:00Z')
FIRST_MONTH = '2026-09'
INITIAL = 100.0
OUT = Path('backtests/results/20pairs-10x-20260926-20261025')
OUT.mkdir(parents=True, exist_ok=True)
FUNDING_PATHS = [Path('backtests/funding-20260925-20261025.json'),Path('backtests/funding-extra-20260925-20261025-part1.json'),Path('backtests/funding-extra-20260925-20261025-part2.json')]
RULES_PATH = Path('backtests/binance-exchange-filters-20pairs.json')
REPORT = OUT / 'summary.json'
EVIDENCE = {'source':'Binance Vision official USD-M futures klines', 'funding_source':'Binance USDT-M funding history API', 'checksums':{},'files':[], 'missing':[]}
NOTES = [
 'Historical replay, not an exact recreation of 10-second live scans or tick-by-tick fills.',
 'Signals checked after each completed 15m candle; all four original timeframes use only information available at that point.',
 'Trades enter at next 1-minute open, at documented opening-price execution; execution fees modeled: 0.02% maker entry and 0.04% taker exit.',
 'Stops and targets tested on each subsequent real 1-minute OHLC candle. If both triggered in the same minute, stop-loss has priority.',
 'Uses the configured 10% equity allocated margin and 10x leverage to calculate position notional, with exchange-step quantity floors.',
 'Public exchange minimum lot/notional rules are a present-day snapshot, not historical rules.',
 'No liquidation engine, exchange matching engine, actual maker fill probability, spread, or slippage; results may be optimistic.',
 'One position open at a time across 20 symbols as in the current bot implementation.',
 'Historical maintenance alerts and 10-second intra-minute tick sequences are unavailable, so their effects are not measured.',
 'Risk-circuit pauses approximated using completed-trade equity and original percentage thresholds.',
]

def save_status(status, error=None, extra=None):
    payload={'status':status,'start_utc':START.isoformat(),'end_exclusive_utc':END.isoformat(),
             'initial_usdt':INITIAL,'symbols':SYMBOLS,'error':error,'notes':NOTES,
             'data_evidence':EVIDENCE}
    if extra:payload.update(extra)
    REPORT.write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str))
    print('BACKTEST_REPORT_JSON='+json.dumps(payload,ensure_ascii=False,default=str),flush=True)
    return payload

def get_archive(sym, datecode, kind):
    if kind=='monthly':
        filename=f'{sym}-1m-{datecode}.zip'
        path=f'data/futures/um/monthly/klines/{sym}/1m/{filename}'
    else:
        filename=f'{sym}-1m-{datecode}.zip'
        path=f'data/futures/um/daily/klines/{sym}/1m/{filename}'
    url=f'https://data.binance.vision/{path}'
    session=requests.Session()
    raw=None
    for attempt in range(3):
        try:
            resp=session.get(url,timeout=(15,95),headers={'User-Agent':'Mozilla/5.0 BotBin-PaperHistoricalAudit/1.0'})
            resp.raise_for_status()
            raw=resp.content
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                files=[p for p in z.namelist() if p.lower().endswith('.csv')]
                if len(files)!=1: raise ValueError(f'Archive expected 1 CSV, got {files}')
                content=z.read(files[0])
            checksum=hashlib.sha256(raw).hexdigest()
            return sym,datecode,kind,url,checksum,content
        except Exception as e:
            if attempt==2:
                return sym,datecode,kind,url,None,str(e)
            time.sleep(1.5*(attempt+1))

def read_csv_bytes(buf):
    # Official futures kline schema: openTime,open,high,low,close,volume,...
    f=pd.read_csv(io.BytesIO(buf),header=None,low_memory=False,usecols=[0,1,2,3,4,5])
    first=pd.to_numeric(f[0],errors='coerce')
    # Discard official CSV header if present, never synthesize any bars.
    f=f.loc[first.notna()].copy()
    cols=['timestamp','open','high','low','close','volume']
    f.columns=cols
    f[cols]=f[cols].apply(pd.to_numeric,errors='raise')
    v=f.timestamp.astype('int64')
    if v.median()>1e14: v=v//1000
    f['open_time']=pd.to_datetime(v,unit='ms',utc=True)
    return f[['open_time','open','high','low','close','volume']].set_index('open_time',drop=False)

def download_data():
    dates=pd.date_range('2026-10-01','2026-10-25',freq='D')
    targets=[(s,FIRST_MONTH,'monthly') for s in SYMBOLS]+[(s,d.strftime('%Y-%m-%d'),'daily') for s in SYMBOLS for d in dates]
    grouped=defaultdict(list)
    print(f'DOWNLOAD_START files={len(targets)} pairs={SYMBOLS}',flush=True)
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures={pool.submit(get_archive,*args):args for args in targets}
        for n, future in enumerate(as_completed(futures),start=1):
            sym,d,kind,url,sha,payload=future.result()
            if sha is None:
                EVIDENCE['missing'].append({'url':url,'error':payload})
                print(f'MISSING {sym} {d}: {payload}',flush=True)
                continue
            try:
                df=read_csv_bytes(payload)
                grouped[sym].append(df)
                EVIDENCE['files'].append({'symbol':sym,'period':d,'kind':kind,'url':url,'rows':len(df),'sha256_zip':sha})
            except Exception as e:
                EVIDENCE['missing'].append({'url':url,'error':f'CSV error: {e}'})
            if n%20==0: print(f'DOWNLOAD_PROGRESS {n}/{len(targets)}',flush=True)
    if EVIDENCE['missing']:
        raise RuntimeError(f'Missing or unreadable required market archives: {len(EVIDENCE["missing"])}')
    result={}
    for sym in SYMBOLS:
        if not grouped[sym]:raise RuntimeError('No klines for '+sym)
        f=pd.concat(grouped[sym]).sort_index()
        if f.index.duplicated().any():
            raise RuntimeError(f'Duplicate minute timestamps detected: {sym}')
        core=f.loc[(f.index>=START)&(f.index<END)]
        expected=int((END-START).total_seconds()/60)
        if len(core)!=expected: raise RuntimeError(f'Incomplete {sym}: {len(core)} minutes vs {expected}')
        idx=core.index.view('int64')
        missing=int(np.sum(np.diff(idx)!=60*10**9))
        if missing:raise RuntimeError(f'Data gaps in {sym}: {missing}')
        EVIDENCE['checksums'][sym]={'full_1m_rows':len(f),'tested_1m_rows':len(core),'first':str(f.index.min()),'last':str(f.index.max()),'sha256_ohlcv':hashlib.sha256(core[['open','high','low','close','volume']].to_numpy(dtype='float64').tobytes()).hexdigest()}
        result[sym]=f
    return result

def make_snapshots(one_min):
    resampled={}
    for tf, freq in [('5m','5min'),('15m','15min'),('1h','1h')]:
        allbars=one_min.resample(freq,label='left',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
        allbars['open_time']=allbars.index
        resampled[tf]=allbars
    return resampled

def snapshot(raw, aggregates, tf, now):
    if tf=='1m':
        return raw.loc[:now].iloc[-250:].copy()
    freq={'5m':'5min','15m':'15min','1h':'1h'}[tf]
    bucket=now.floor(freq)
    # Earlier bars are fully completed; never include fully aggregated future of current bucket.
    previous=aggregates[tf].loc[:bucket].iloc[:-1].iloc[-249:]
    curr=raw.loc[bucket:now]
    if curr.empty:return previous.copy()
    partial=pd.DataFrame([{'open_time':bucket,'open':curr['open'].iloc[0],'high':curr['high'].max(),'low':curr['low'].min(),
                          'close':curr['close'].iloc[-1],'volume':curr['volume'].sum()}],index=pd.DatetimeIndex([bucket]))
    return pd.concat([previous,partial])

def load_funding():
    merged={}
    for p in FUNDING_PATHS:
        obj=json.loads(p.read_text())
        for sym,items in obj['symbols'].items():
            if sym in merged:raise RuntimeError('Duplicate funding symbol '+sym)
            merged[sym]=items
    if set(merged)!=set(SYMBOLS):raise RuntimeError('Missing or unexpected historical funding symbols: '+str(set(SYMBOLS)-set(merged)))
    d={}
    for sym in SYMBOLS:
        row=sorted(merged[sym],key=lambda x:x['t'])
        if not row:raise RuntimeError('No real historical funding for '+sym)
        t=np.array([v['t'] for v in row],dtype='int64')
        v=np.array([v['rate'] for v in row],dtype='float64')
        if np.any(np.abs(v)>0.0015):raise RuntimeError('Historical funding extremes require real OI, which is unavailable')
        d[sym]=(t,v)
        EVIDENCE['checksums'][sym+'_funding']={'records':len(row),'first':str(pd.to_datetime(t[0],unit='ms',utc=True)),'last':str(pd.to_datetime(t[-1],unit='ms',utc=True)),
                                                'min_rate':float(np.min(v)),'max_rate':float(np.max(v)),'extreme_count':int(np.sum(np.abs(v)>0.0015))}
    NOTES.append('Actual archived funding rates: none above ±0.15% across the 20 selected pairs; no Funding Fade activation. Historical OI is not synthesized.')
    return d

def calculate_scores(raw, bars, funding, now, weights):
    candles={tf:snapshot(raw,bars,tf,now) for tf in ('1m','5m','15m','1h')}
    if any(len(d)<250 for d in candles.values()):return None,candles
    times, rates=funding
    i=np.searchsorted(times,int(now.value//10**6),side='right')-1
    if i<0:raise RuntimeError('Missing earlier funding rate as of '+str(now))
    rate=float(rates[i])
    return calculate_signals('',candles,rate,float('nan'),weights),candles

def run_backtest(rawdata,funding):
    assert len(SYMBOLS)==20
    bars={sym:make_snapshots(rawdata[sym]) for sym in SYMBOLS}
    equity=INITIAL; peak=equity; total_fee=0.0
    open_trade=None; closed=[]; scanned=0; skipped=Counter(); decisions=Counter(); equity_curve=[]
    daily_fee=defaultdict(float); weights={h:{k:1.0 for k in ['ema','rsi','vwap','volume','macd','funding','oi']} for h in range(24)}
    regimes={sym:'RANGING' for sym in SYMBOLS}
    paused_until=START; checkpoint=START
    expected_minutes=int((END-START).total_seconds()/60)
    times=pd.date_range(START,END,freq='min',inclusive='left')
    with state_lock:
        state['closed_trades']=[]
        state['paper_closed_trades']=[]
        state['paper_balance']=INITIAL
        state['starting_balance']=INITIAL
        state['peak_balance']=INITIAL
    for n,now in enumerate(times):
        if n and n%10080==0:print(f'REPLAY_PROGRESS minute={n}/{expected_minutes} finished={len(closed)} equity={equity:.6f}',flush=True)
        # Check every real 1-minute bar for open position exits; mark from OHLC.
        if open_trade:
            tr=open_trade; minute=rawdata[tr['symbol']].loc[now]
            hi,lo,close=float(minute.high),float(minute.low),float(minute.close)
            entry=tr['entry']; is_long=tr['direction']=='LONG'
            stop=(lo<=tr['sl']) if is_long else (hi>=tr['sl'])
            target=(hi>=tr['tp']) if is_long else (lo<=tr['tp'])
            reason=None
            if stop:exit_price=tr['sl'];reason='stop_loss'
            elif target:exit_price=tr['tp'];reason='take_profit'
            if reason:
                qty=tr['qty']
                pnl=(exit_price-entry)*qty*(1 if is_long else -1)
                fees=qty*entry*0.0002+qty*exit_price*0.0004
                net=pnl-fees
                equity+=net
                total_fee+=fees
                daily_fee[now.date()]+=fees
                tr.update({'exit_time':now.isoformat(),'exit_price':exit_price,'gross_pnl':pnl,'fee_paid':fees,'net_pnl':net,'reason':reason,'equity_after':equity})
                closed.append(tr)
                with state_lock:
                    state['closed_trades'].insert(0,{'pnl':pnl,'exit_time':now.isoformat()})
                    state['closed_trades']=state['closed_trades'][:50]
                    state['paper_balance']=equity
                open_trade=None
                peak=max(peak,equity)
                if len(closed)%20==0:
                    # Original signal_weights algorithm, corrected for historical 'as-of' time
                    # (original code queries datetime('now') which would leak wall-clock time).
                    recent=[x for x in closed if pd.Timestamp(x['exit_time'])>=now-pd.Timedelta(days=30)]
                    for hour in range(24):
                        relevant=[x for x in recent if pd.Timestamp(x['exit_time']).hour==hour]
                        for signal in weights[hour]:
                            candidates=[x for x in relevant if x['signal_states'].get(signal)==1]
                            if len(candidates)>=5:
                                w=sum(x['net_pnl']>=0 for x in candidates)/len(candidates)
                                weights[hour][signal]=max(0.1,min(2.0,(w-0.4)*5.0))
            else:
                # Emulate original paper logic: check close for breakeven and trailing,
                # after executing previously existing stop and target intrabar.
                atr=tr['atr'];profit_atr=((close-entry)*(1 if is_long else -1))/(atr+1e-8)
                tr['peak_price']=max(tr['peak_price'],close) if is_long else min(tr['peak_price'],close)
                if profit_atr>=1.0 and tr['sl']!=entry:
                    tr['sl']=entry
                elif profit_atr>=2.0:
                    target_sl=tr['peak_price']-atr if is_long else tr['peak_price']+atr
                    if (is_long and target_sl>tr['sl']) or (not is_long and target_sl<tr['sl']):tr['sl']=target_sl
        if now.minute==59:
            equity_curve.append({'time':now.isoformat(),'equity_realized':equity})
        if now.minute%15!=14:continue
        if open_trade:
            skipped['already_open']+=1
            continue
        if now<paused_until:
            skipped['circuit_breaker_pause']+=1
            continue
        if equity<=INITIAL*.90:
            paused_until=max(paused_until,now+pd.Timedelta(hours=6))
            skipped['10_percent_drawdown_pause']+=1
            continue
        if (peak-equity)/peak>=0.25:
            paused_until=max(paused_until,now+pd.Timedelta(hours=48))
            skipped['25_percent_peak_drawdown_pause']+=1
            continue
        # Refresh regime from 1h snapshot on each hourly boundary, using no future bars.
        if now.minute==59:
            for sym in SYMBOLS:
                frame=snapshot(rawdata[sym],bars[sym],'1h',now).iloc[-100:]
                with state_lock:state['market_regime']=regimes[sym]
                regimes[sym]=detect_market_regime(frame)
        if now+pd.Timedelta(minutes=1)>=END:continue
        for sym in SYMBOLS:
            with state_lock:state['market_regime']=regimes[sym]
            # Fee throttle as original: >=2% of current account in a single UTC day
            threshold=config.CONFLUENCE_THRESHOLD+(1.0 if equity>0 and daily_fee[now.date()]/equity>=config.FEE_THROTTLE_LIMIT else 0.0)
            scores,candles=calculate_scores(rawdata[sym],bars[sym],funding[sym],now,weights[now.hour])
            scanned+=1
            if scores is None:skipped['insufficient_lookback']+=1;continue
            side=scores.get('confluence_direction')
            if side not in ('LONG','SHORT'):skipped['no_two_strategy_confluence_or_trend_conflict']+=1;continue
            if (side=='LONG' and scores['long_score']<threshold) or (side=='SHORT' and scores['short_score']<threshold):
                skipped['confluence_score_below_threshold']+=1;continue
            atr=float(calc_atr(candles['5m'],14).iloc[-1]); signal_entry=float(candles['5m']['close'].iloc[-1])
            closed_c=len(closed); wins=sum(x['net_pnl']>=0 for x in closed);losses=closed_c-wins
            win_rate=0.58 if not closed_c else wins/(wins+losses+1e-8)
            size=calculate_position_size(equity,atr,signal_entry,win_rate=win_rate)
            if size<5:skipped['notional_below_5_usdt']+=1;continue
            nextbar=rawdata[sym].loc[now+pd.Timedelta(minutes=1)]
            price=float(nextbar.open)
            if not np.isfinite(price) or price<=0:raise RuntimeError('Invalid real entry bar for '+sym)
            qty=round_qty(sym,size/price)
            size=qty*price
            exchange_min=symbol_rules[sym]['min_notional']
            if qty<=0 or size<exchange_min:
                skipped['exchange_minimum_after_rounding']+=1
                continue
            if not passes_fee_gate(atr,signal_entry,size):skipped['fee_gate_blocked']+=1;continue
            agreeing=scores.get('confluence_strategies',[])
            mean='Strategy 2 (Mean Reversion)' in agreeing
            sl_mult=1.0 if mean else 1.5
            tp_mult=1.5 if mean else (4.0 if 'Strategy 3 (Breakout)' in agreeing else 3.0)
            sl=price-atr*sl_mult if side=='LONG' else price+atr*sl_mult
            tp=price+atr*tp_mult if side=='LONG' else price-atr*tp_mult
            open_trade={'symbol':sym,'direction':side,'entry_time':(now+pd.Timedelta(minutes=1)).isoformat(),
                'signal_time':now.isoformat(),'entry':price,'qty':qty,'notional_usdt':size,
                'margin_usdt':size/config.LEVERAGE,'requested_margin_usdt':equity*config.POSITION_MARGIN_FRACTION,
                'leverage':config.LEVERAGE,
                'atr':atr,'sl':sl,'tp':tp,'peak_price':price,'market_regime':regimes[sym],
                'strategy_names':agreeing,'signal_states':scores['states'],'signal_strength':scores['long_score'] if side=='LONG' else scores['short_score']}
            decisions[sym]+=1
            break
        if n%1440==0:print('DAILY',str(now),len(closed),equity,flush=True)
    if open_trade:
        last=rawdata[open_trade['symbol']].loc[END-pd.Timedelta(minutes=1)]
        price=float(last.close)
        qty=open_trade['qty']
        unreal=(price-open_trade['entry'])*qty*(1 if open_trade['direction']=='LONG' else -1)
        pending_fee=qty*open_trade['entry']*0.0002+qty*price*0.0004
        mark_equity=equity+unreal-pending_fee
    else:unreal=0.0;pending_fee=0.0;mark_equity=equity
    gross=sum(x['gross_pnl'] for x in closed)
    fees=sum(x['fee_paid'] for x in closed)
    wins=sum(x['net_pnl']>0 for x in closed)
    losses=sum(x['net_pnl']<0 for x in closed)
    totalwin=sum(x['net_pnl'] for x in closed if x['net_pnl']>0)
    totalloss=sum(x['net_pnl'] for x in closed if x['net_pnl']<0)
    daily_results=defaultdict(lambda:{'trades':0,'net':0.0})
    for tr in closed:
        key=tr['exit_time'][:10]
        daily_results[key]['trades']+=1;daily_results[key]['net']+=tr['net_pnl']
    (OUT/'trades.csv').write_text('')
    if closed:
        df=pd.DataFrame(closed)
        df['signal_states']=df['signal_states'].apply(json.dumps)
        df['strategy_names']=df['strategy_names'].apply(json.dumps)
        df.to_csv(OUT/'trades.csv',index=False)
    else:
        (OUT/'trades.csv').write_text('symbol,direction,entry_time,exit_time,net_pnl\n')
    pd.DataFrame(equity_curve).to_csv(OUT/'equity_curve.csv',index=False)
    return dict(initial_usdt=INITIAL,realized_final_balance_usdt=round(equity,6),
        marked_final_equity_usdt=round(mark_equity,6),net_profit_closed_usdt=round(equity-INITIAL,6),
        return_pct_closed=round((equity-INITIAL)/INITIAL*100,6),
        total_trades=len(closed),wins=wins,losses=losses,win_rate_pct=round(100*wins/len(closed),4) if closed else None,
        gross_pnl_usdt=round(gross,6),total_fees_usdt=round(fees,6),profit_factor=round(totalwin/abs(totalloss),4) if totalloss<0 else None,
        max_drawdown_pct=round(100*max((max(v['equity_realized'] for v in equity_curve[:i+1])-x['equity_realized'])/max(v['equity_realized'] for v in equity_curve[:i+1]) for i,x in enumerate(equity_curve)),4) if equity_curve else None,
        open_trade_at_end=open_trade,open_trade_unrealized_pnl_usdt=round(unreal,6),estimated_exit_fee_usdt=round(pending_fee,6),
        signal_evaluations=scanned,skips=dict(skipped),entries_by_symbol=dict(decisions),daily_results=dict(daily_results),
        sampling='15-minute signal evaluations and 1-minute bar OHLC exits',
        margin_fraction=config.POSITION_MARGIN_FRACTION,leverage=config.LEVERAGE,selected_pairs=len(SYMBOLS),
        original_modules_used=['modules.signals.calculate_signals','modules.position_sizer.calculate_position_size','modules.fee_guardian.passes_fee_gate','modules.regime_detector.detect_market_regime','modules.trade_manager.round_qty'])

def main():
    try:
        if os.getenv('BINANCE_API_KEY') or os.getenv('BINANCE_API_SECRET'):
            raise RuntimeError('Credentials forbidden in historical paper test')
        if len(SYMBOLS)!=20 or config.LEVERAGE!=10 or config.POSITION_MARGIN_FRACTION!=0.10:raise RuntimeError('Requested 20-pair, 10x and 10% margin config not present')
        if not all(p.exists() for p in FUNDING_PATHS):raise RuntimeError('Historical funding source JSON missing')
        if not RULES_PATH.exists():raise RuntimeError('Binance exchange step/min-notional snapshot missing')
        rules=json.loads(RULES_PATH.read_text())['symbols']
        if set(rules)!=set(SYMBOLS):raise RuntimeError('Incomplete Binance trading filters')
        for sym,v in rules.items():
            symbol_rules[sym]={'step_size':float(v['step_size']),'tick_size':float(v['tick_size']),'min_notional':float(v['min_notional'])}
        config.STARTING_BALANCE=INITIAL
        config.TARGET_BALANCE=INITIAL*10
        funding=load_funding()
        raw=download_data()
        result=run_backtest(raw,funding)
        save_status('completed',extra={'results':result})
        print('BACKTEST_COMPLETED',json.dumps({k:v for k,v in result.items() if k not in ('open_trade_at_end','daily_results','skips')},default=str),flush=True)
    except Exception as e:
        traceback.print_exc()
        save_status('failed',error=f'{type(e).__name__}: {e}')
        sys.exit(1)
if __name__=='__main__':main()