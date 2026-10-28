export type CatalogTone = 'good' | 'bad' | 'warn' | 'info' | 'violet' | 'neutral';

export type CatalogRow = {
  name: string;
  detail: string;
  value?: string;
  tone?: CatalogTone;
};

export type CatalogBlock =
  | {
      kind: 'list';
      title: string;
      description: string;
      fullWidth?: boolean;
      rows: CatalogRow[];
    }
  | {
      kind: 'table';
      title: string;
      description: string;
      fullWidth?: boolean;
      columns: string[];
      rows: string[][];
    };

export type CatalogStat = {
  label: string;
  value: string;
  tone?: CatalogTone;
};

export type CatalogSection = {
  id: 'coin-analysis' | 'grid-strategy' | 'backtesting-validation';
  title: string;
  description: string;
  accent: CatalogTone;
  icon: 'activity' | 'grid' | 'chart';
  stats: CatalogStat[];
  blocks: CatalogBlock[];
};

export const COIN_SETTINGS_PATH = '/admin/strategies/coin-settings';

export const SECTION_PATHS: Record<CatalogSection['id'], string> = {
  'coin-analysis': '/admin/strategies/coin-analysis',
  'grid-strategy': '/admin/strategies/grid-strategy',
  'backtesting-validation': '/admin/strategies/backtesting'
};

export function getSection(id: CatalogSection['id']): CatalogSection {
  const section = STRATEGY_SECTIONS.find((candidate) => candidate.id === id);
  if (!section) {
    throw new Error(`Unknown strategy section: ${id}`);
  }
  return section;
}

export const STRATEGY_SECTIONS: CatalogSection[] = [
  {
    id: 'coin-analysis',
    title: 'Coin Analysis',
    description:
      'How raw Binance USDⓈ-M futures data becomes an eligibility decision and a ranked score. Hard gates run before any ranking; the eight factors only order coins that survived them.',
    accent: 'violet',
    icon: 'activity',
    stats: [
      { label: 'Hard gates', value: '13', tone: 'info' },
      { label: 'Ranking factors', value: '8', tone: 'violet' },
      { label: 'Review flags', value: '11', tone: 'warn' },
      { label: 'Score penalties', value: '12', tone: 'bad' }
    ],
    blocks: [
      {
        kind: 'list',
        title: 'Universe & data window',
        description: 'Every metric below is computed from completed candles only.',
        rows: [
          {
            name: 'Candle interval',
            value: '4h',
            detail:
              'Walk-forward simulation runs on completed 4-hour bars. Paper monitoring ticks every 5 minutes but acts on completed 1-hour candles (PAPER_SIGNAL_INTERVAL).'
          },
          {
            name: 'Analysis lookback',
            value: '520d',
            detail:
              'History pulled per coin (ENGINE_LOOKBACK_DAYS). At least 120 days are required before a coin can be ranked (ENGINE_MIN_DATA_DAYS).'
          },
          {
            name: 'Warmup buffer',
            value: '45d',
            detail:
              'Reserved before the first fold so rolling range, ATR, and funding windows are fully populated (ENGINE_WARMUP_BUFFER_DAYS).'
          },
          {
            name: 'Liquidity floor',
            value: '$5M ADV',
            detail:
              'Median 24-hour quote volume must clear ENGINE_MIN_ADV_USD. This is a hard gate, not a ranking input.'
          },
          {
            name: 'Funding',
            detail:
              'Historical funding rates are charged against long inventory bar by bar, so carry cost is inside every reported return.'
          },
          {
            name: 'Data quality',
            detail:
              'Coins with missing or insufficient candles are excluded before simulation and listed in the run summary as data errors.'
          }
        ]
      },
      {
        kind: 'table',
        title: 'Hard eligibility gates',
        description:
          'A failed gate excludes the coin from ranking and from paper selection. Thresholds are read from the run that produced the results.',
        fullWidth: true,
        columns: ['Gate', 'What it checks', 'Default threshold'],
        rows: [
          ['History', 'Usable candle history per coin', '≥ 120 days'],
          ['Walk-forward folds', 'At least one completed test fold', '≥ 1 fold'],
          ['Median 24h liquidity', 'Median quote volume over 24 hours', '≥ $5M'],
          ['OOS fills', 'Executed fills across untouched test folds', '≥ 12'],
          ['Positive OOS net return', 'Compounded test-window return after costs', '> 0%'],
          ['OOS max drawdown', 'Peak-to-trough drawdown of the stitched curve', '≤ 35%'],
          ['Profitable folds', 'Share of test folds with positive return', '≥ 50%'],
          ['No stop-out', 'No per-position stop or liquidation in any test fold', 'Required'],
          ['ATR% band', '14-day ATR as a share of price', '0% – 12%'],
          ['Bid-ask spread', 'Live book snapshot during analysis', '≤ 10 bps'],
          [
            'Order-book depth',
            'Depth inside ±25 bps versus one grid slot notional',
            '≥ 1.0× a slot'
          ],
          [
            'Profitable under cost stress',
            'Fees and slippage multiplied, test folds rerun',
            '> 0% stressed return'
          ],
          [
            'Profit-factor validation',
            'PF above the validation threshold needs enough fills, no stop-out, and stressed profitability',
            'PF > 30'
          ]
        ]
      },
      {
        kind: 'table',
        title: 'Ranking factors',
        description:
          'Each input is percentile-normalised across the eligible cross-section, then combined 0–100. The final score is clamped to 0–100 after penalties.',
        fullWidth: true,
        columns: ['Factor', 'Weight', 'Inputs'],
        rows: [
          ['Profitability', '18%', 'Profit factor 60% · win rate 40%'],
          ['Risk-adjusted return', '18%', 'OOS net return 45% · return-to-drawdown 55%'],
          [
            'Drawdown control',
            '15%',
            'Max drawdown 40% · recovery days 20% · no stop 20% · max unrealized loss 12% · 1% tail loss 8%'
          ],
          [
            'Fold consistency',
            '14%',
            'Profitable fold share 35% · worst fold 25% · fold return spread 20% · parameter stability 20%'
          ],
          ['Regime robustness', '10%', 'Regime robustness score 75% · time in range 25%'],
          [
            'Structural stability',
            '10%',
            'Mean reversion 35% · range stability 30% · parameter sensitivity 20% · parameter stability 15%'
          ],
          [
            'Execution quality',
            '10%',
            'ADV 22% · cost-to-edge 18% · spread 14% · depth 16% · funding 10% · stressed return 20%'
          ],
          ['Diversification', '5%', 'Average correlation to other eligible coins']
        ]
      },
      {
        kind: 'list',
        title: 'Regime robustness',
        description:
          'Each test fold is labelled using only bars inside that window, then results are aggregated by regime.',
        fullWidth: true,
        rows: [
          {
            name: 'Regime labels',
            value: '5 classes',
            detail:
              'trending, high_volatility, ranging, mixed, or unknown (fewer than 60 bars in the window).'
          },
          {
            name: 'Trending test',
            value: 'ADX ≥ 25',
            detail:
              'Also trending when trend R² ≥ 0.65 and absolute annualised slope ≥ 50%.'
          },
          {
            name: 'High volatility',
            value: '≥ 120% annual',
            detail:
              'Annualised standard deviation of log returns inside the test window.'
          },
          {
            name: 'Ranging test',
            value: '≥ 55% contained',
            detail:
              'Range containment at least 55% with ADX below 20.'
          },
          {
            name: 'Robustness score',
            tone: 'good',
            detail:
              '45 × profitable-regime share + 35 × coverage + 20 × (1 − dominant share) − 15 dependence penalty, clamped 0–100.'
          },
          {
            name: 'Dependence penalty',
            tone: 'bad',
            detail:
              'Applied when one regime supplies 90% or more of all positive fold return. Also penalises the score by 4 points and raises a review flag.'
          },
          {
            name: 'Coverage',
            tone: 'good',
            detail:
              'Share of the three target regimes seen in the test folds: trending, high volatility, ranging. A single-regime result is flagged and penalised.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Walk-forward consistency',
        description: 'The test window is never used to choose a parameter.',
        rows: [
          {
            name: 'Fold structure',
            value: '3 folds',
            detail:
              '45% train / 18% test of available history, stepping forward by one test window. Minimum 90 train days and 30 test days.'
          },
          {
            name: 'Selection',
            value: '96 candidates',
            detail:
              'Each fold picks levels, range lookback, rebalance cadence and mode, stop distance, and take-profit distance on its training window only.'
          },
          {
            name: 'Consistency',
            value: '≥ 50%',
            tone: 'good',
            detail:
              'Share of test folds with positive return. Fewer than half is a gate failure and an 8-point penalty.'
          },
          {
            name: 'Parameter stability',
            detail:
              'Share of folds that selected the same full parameter set. Low stability means the ranking is fitting fold-specific noise.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Cost stress & parameter sensitivity',
        description: 'Two independent overfitting checks on the untouched test folds.',
        rows: [
          {
            name: 'Cost stress',
            value: '2.0×',
            detail:
              'Maker fee, taker fee, and adverse slippage are multiplied by ENGINE_COST_STRESS_MULTIPLIER and every test fold is rerun.'
          },
          {
            name: 'Stress gate',
            value: '> 0%',
            tone: 'good',
            detail:
              'Compounded stressed return must stay positive when ENGINE_ENFORCE_COST_STRESS is enabled. A failure raises a critical flag.'
          },
          {
            name: 'Sensitivity variants',
            value: '4 variants',
            detail:
              'Fewer levels (−5), more levels (+5), shorter range (−7 days), and a spacing/range perturbation around the selected ladder.'
          },
          {
            name: 'Sensitivity score',
            value: 'flag > 0.5',
            tone: 'warn',
            detail:
              'Mean degradation of neighbour returns versus the selected parameters, capped at 2.0. Above 0.5 costs up to 8 score points.'
          },
          {
            name: 'Parameter stability',
            detail:
              'Share of folds that converged on the same configuration; feeds both the consistency and stability factors.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Return stability & recovery',
        description: 'Detects results carried by a handful of trades or a slow recovery profile.',
        rows: [
          {
            name: 'Top trade share',
            value: 'flag > 60%',
            tone: 'warn',
            detail:
              'Share of gross profit produced by the single best trade (ENGINE_MAX_TOP_TRADE_SHARE).'
          },
          {
            name: 'Top-5 trade share',
            detail: 'Same concentration measure across the five best trades.'
          },
          {
            name: 'Fold return spread',
            detail:
              'Standard deviation of test-fold returns. Feeds the fold-consistency factor and later scoring.'
          },
          {
            name: 'Recovery days',
            value: 'flag > 30d',
            tone: 'warn',
            detail:
              'Days from the worst drawdown trough back to the prior equity peak.'
          },
          {
            name: 'Drawdown duration',
            detail: 'Longest peak-to-recovery spell across all test folds.'
          },
          {
            name: '1% tail loss',
            detail:
              'Worst 1% of bar returns across the stitched curve, included in the drawdown-control factor.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Correlation & diversification',
        description: 'Correlations use aligned daily closes and only gate-eligible coins.',
        rows: [
          {
            name: 'Correlation window',
            value: '60 days',
            detail:
              'Rolling daily returns aligned on common dates (ENGINE_CORR_LOOKBACK_DAYS); at least 10 overlapping days are required.'
          },
          {
            name: 'Peer set',
            value: 'eligible only',
            detail:
              'An ineligible coin cannot change the diversification penalty for the rest of the book.'
          },
          {
            name: 'Correlation ceiling',
            value: '0.85',
            tone: 'warn',
            detail:
              'Peak pairwise correlation above ENGINE_MAX_CORRELATION raises a flag and removes 2–14 score points.'
          },
          {
            name: 'Paper selection',
            value: 'greedy',
            tone: 'info',
            detail:
              'Top paper candidates are selected in rank order and skipped when their peak correlation with an already selected coin exceeds the same limit.'
          }
        ]
      },
      {
        kind: 'table',
        title: 'Review flags',
        description:
          'Flags are diagnostic only. Critical flags also imply a gate failure; warnings reduce the score but do not exclude a coin.',
        fullWidth: true,
        columns: ['Code', 'Severity', 'Trigger'],
        rows: [
          ['profit_factor_validation', 'warning', 'Profit factor above 30'],
          ['profit_factor_review', 'warning', 'Profit factor above 10'],
          ['cost_stress_failed', 'critical', 'Stressed test rerun is not profitable'],
          ['single_regime', 'warning', 'Only one market regime appears in test folds'],
          ['regime_dependent', 'warning', 'One regime supplies ≥ 90% of positive return'],
          ['parameter_sensitive', 'warning', 'Neighbour variants lose more than 50% of return'],
          ['concentrated_returns', 'warning', 'Best trade is more than 60% of gross profit'],
          ['high_correlation', 'warning', 'Peak correlation to an eligible peer ≥ 0.85'],
          ['slow_recovery', 'info', 'Worst drawdown needed more than 30 days to recover'],
          ['high_inventory_exposure', 'warning', 'Peak inventory above 1.5× equity'],
          ['low_cycle_completion', 'info', 'Fewer than half of buys completed against a sell rung']
        ]
      },
      {
        kind: 'table',
        title: 'Score penalties',
        description:
          'Applied after the weighted factors. Penalties are additive and the final score is floored at zero.',
        fullWidth: true,
        columns: ['Penalty', 'Points', 'Trigger'],
        rows: [
          ['stop_out', '−15', 'Stop-out or liquidation in any test fold'],
          ['fold_consistency', '−8', 'Fewer than half of test folds profitable'],
          ['low_sample', '−5', 'OOS fills below the minimum'],
          ['cost_stress', '−6', 'Unprofitable under stressed costs'],
          ['single_regime', '−3', 'Only one regime tested'],
          ['regime_dependence', '−4', 'One regime dominates positive return'],
          ['parameter_sensitivity', 'up to −8', 'Neighbour degradation above 0.5'],
          ['trade_concentration', '−4', 'Top trade above the concentration limit'],
          ['correlation', '−2 to −14', 'Peak correlation above the ceiling'],
          ['slow_recovery', 'up to −4', 'Recovery longer than 30 days'],
          ['inventory_exposure', 'up to −4', 'Peak exposure above 1.5× equity'],
          ['cycle_completion', '−2', 'Less than half of buys closed against a sell rung']
        ]
      }
    ]
  },
  {
    id: 'grid-strategy',
    title: 'Grid Strategy',
    description:
      'The live and simulated grid engine: how the ladder is built, how it adapts to volatility and funding, and which caps override every other rule.',
    accent: 'good',
    icon: 'grid',
    stats: [
      { label: 'Default levels', value: '20', tone: 'good' },
      { label: 'Range modes', value: '3', tone: 'info' },
      { label: 'Rebalance modes', value: '2', tone: 'violet' },
      { label: 'Stress scenario', value: '2×', tone: 'warn' }
    ],
    blocks: [
      {
        kind: 'list',
        title: 'Ladder & range construction',
        description: 'Risk caps always override range and ladder adjustments.',
        rows: [
          {
            name: 'Base ladder',
            value: '20 levels',
            detail:
              'Geometric spacing with an ATR multiplier of 1.0 (ENGINE_GRID_LEVELS, ENGINE_ATR_MULTIPLIER). Levels are searched as 10 / 20 / 30 per fold.'
          },
          {
            name: 'Spacing',
            value: 'geometric',
            detail:
              'Geometric gives constant percentage steps; arithmetic gives constant price steps and stays supported for narrow, low-volatility ranges.'
          },
          {
            name: 'Percentile range',
            value: 'p5 – p95',
            detail:
              'Active default. Uses the 5th and 95th percentile of lows/highs over the range lookback, padded by one ATR and clamped so live price sits inside.'
          },
          {
            name: 'Price-centered range',
            value: '±3 ATR',
            detail:
              'Symmetric band around the live price (at least ±1.5%), independent of where the recent percentile band happened to sit.'
          },
          {
            name: 'ATR / support range',
            value: 'support floor',
            detail:
              'Floor at the 30-day structural low plus 0.25 ATR; ceiling at the recent swing high plus one ATR. Range width is capped at 12 ATR.'
          },
          {
            name: 'Range lookback',
            value: '14 / 30d',
            detail:
              'Searched per fold. The ladder is rebuilt from the selected lookback at every rebalance.'
          },
          {
            name: 'Initial inventory',
            value: '40%',
            detail:
              'Share of capital deployed on whichever rungs are eligible at start (ENGINE_INITIAL_INVENTORY_PCT).'
          },
          {
            name: 'Sizing',
            detail:
              'Each rung is sized from equity × leverage × utilization / levels. The inventory cap is applied per order, so a single rung can never exceed capital.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Adaptive overlays',
        description:
          'The engine is long-only and starts flat, so these overlays protect inventory rather than short the market.',
        fullWidth: true,
        rows: [
          {
            name: 'Structural stop',
            value: '1.5 / 2.5 ATR',
            tone: 'warn',
            detail:
              'Close all inventory when price falls the selected ATR distance or invalidates the structural floor below the ladder. A stop-out is a hard risk failure in scoring.'
          },
          {
            name: 'Trailing up',
            value: '1.0 ATR',
            tone: 'good',
            detail:
              'When price closes above the active range high by one ATR, the ladder shifts up and re-arms rungs so the grid keeps making markets.'
          },
          {
            name: 'Trailing take-profit',
            value: '1.5 / 3.0 ATR',
            detail:
              'Armed once net profit clears 2%. Inventory is closed when price retraces the selected ATR distance from the peak. Selection happens on train windows only.'
          },
          {
            name: 'Range-position gate',
            value: 'top 10%',
            tone: 'info',
            detail:
              'New buys pause when price is in the top 10% of its 30-day range, avoiding the worst entries just below resistance.'
          },
          {
            name: 'Funding gate',
            value: 'z ≥ 2.0',
            tone: 'info',
            detail:
              'Buys are blocked when the 90-settlement funding z-score reaches 2.0; inventory can be scaled down by 50% instead of adding.'
          },
          {
            name: 'Overbought guard',
            value: 'RSI 75 / 35',
            tone: 'warn',
            detail:
              'RSI ≥ 75 with fading MACD or a 5% open-interest drop arms a pause. RSI ≤ 35 clears it. Uses a 6% trailing stop, 24-hour arm timeout, and 30-minute recovery cooldown.'
          },
          {
            name: 'Regime gate',
            detail:
              'Paper execution evaluates regime signals on completed 1-hour candles before any fill; the backtest applies the same overlays inside each bar.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Rebalancing',
        description: 'Rebalancing re-centres the range but keeps inventory and realised P&L.',
        rows: [
          {
            name: 'Fixed cadence',
            value: '7 / 14d',
            detail: 'Rebuild the ladder on a fixed schedule. This is the default base configuration.'
          },
          {
            name: 'Adaptive cadence',
            value: '24h review',
            detail:
              'Rebalance on a ≥ 50% volatility shift, a breakout of one ATR, or a regime change. At most once per 12-hour cooldown.'
          },
          {
            name: 'Selection',
            value: 'fixed vs adaptive',
            detail:
              'Both modes are searched per fold. The mode chosen by the fold is carried into the paper session parameters.'
          },
          {
            name: 'Cooldown',
            detail:
              'All adaptive events share a 12-hour cooldown so a volatile bar cannot churn the ladder repeatedly.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Risk caps',
        description: 'These bounds override every grid adjustment and overlay.',
        rows: [
          {
            name: 'Max utilization',
            value: '90%',
            tone: 'warn',
            detail: 'Share of available margin the ladder may use.'
          },
          {
            name: 'Inventory cap',
            value: '1.0× equity',
            tone: 'good',
            detail:
              'Peak base-asset exposure. The tighter of this cap and leverage × utilization applies to every order.'
          },
          {
            name: 'Per-position stop',
            value: '30%',
            tone: 'warn',
            detail:
              'Capital stop in addition to the ATR structural stop (ENGINE_STOP_LOSS_PCT).'
          },
          {
            name: 'Portfolio drawdown stop',
            value: 'disabled',
            tone: 'bad',
            detail:
              'Optional peak-to-trough stop for the whole session. Off by default so walk-forward results stay comparable.'
          },
          {
            name: 'Isolation',
            detail:
              'Each paper session is isolated margin with its own capital. Liquidation is simulated with a 0.5% maintenance margin rate.'
          },
          {
            name: 'Kill switch',
            tone: 'info',
            detail:
              'A global live-trading disable that overrides approvals and scheduled jobs.'
          }
        ]
      },
      {
        kind: 'table',
        title: 'Simulation cost assumptions',
        description:
          'The same assumptions are used in walk-forward tests, paper sessions, and live order sizing.',
        fullWidth: true,
        columns: ['Assumption', 'Default', 'Configuration'],
        rows: [
          ['Capital per session', '$1,000', 'ENGINE_PAPER_CAPITAL'],
          ['Leverage', '2×', 'ENGINE_LEVERAGE'],
          ['Maker fee', '0.02%', 'ENGINE_MAKER_FEE_RATE'],
          ['Taker fee', '0.05%', 'ENGINE_TAKER_FEE_RATE'],
          ['Slippage', '1 bp adverse', 'ENGINE_SLIPPAGE_BPS'],
          ['Funding', 'Historical rate per bar', 'Binance funding history'],
          ['Maintenance margin', '0.5%', 'Fixed simulator assumption'],
          ['Intrabar path', 'Conservative', 'Worst-case ordering when a bar spans both sides'],
          ['Stress scenario', '2× fees and slippage', 'ENGINE_COST_STRESS_MULTIPLIER']
        ]
      },
      {
        kind: 'table',
        title: 'Execution & inventory metrics',
        description: 'Persisted per coin and shown in the per-coin detail view below.',
        fullWidth: true,
        columns: ['Metric', 'Meaning', 'Flag / use'],
        rows: [
          ['max_exposure_ratio', 'Peak base-asset notional as a multiple of equity', 'Flag above 1.5×'],
          ['avg_exposure_ratio', 'Average deployed notional over test bars', 'Context for capital use'],
          [
            'max_unrealized_loss_pct',
            'Worst mark-to-market loss as a share of equity',
            'Drawdown-control factor'
          ],
          [
            'cycle_completion_ratio',
            'Closed lots divided by buy fills',
            'Flag below 50% with enough fills'
          ],
          ['top_trade_share', 'Gross profit from the best trade', 'Flag above 60%'],
          ['time_in_range', 'Share of bars with live price inside the ladder', 'Regime factor input'],
          ['slippage_paid', 'Total slippage charged across test fills', 'Execution-cost audit'],
          ['funding_paid', 'Total funding charged on long inventory', 'Carry-cost audit'],
          ['recovery_days', 'Worst drawdown recovery time', 'Flag above 30 days'],
          ['fold_return_std', 'Dispersion of test-fold returns', 'Consistency factor input']
        ]
      }
    ]
  },
  {
    id: 'backtesting-validation',
    title: 'Backtesting & Validation',
    description:
      'The research pipeline, the search space it explores, and the paper-to-live ladder a strategy must clear before real capital is considered.',
    accent: 'warn',
    icon: 'chart',
    stats: [
      { label: 'Walk-forward folds', value: '3', tone: 'info' },
      { label: 'Ladder candidates', value: '96', tone: 'violet' },
      { label: 'Paper minimum', value: '7d', tone: 'good' },
      { label: 'Validation stages', value: '4', tone: 'warn' }
    ],
    blocks: [
      {
        kind: 'table',
        title: 'Walk-forward design',
        description:
          'Rolling folds use only past data for selection and score on untouched future windows.',
        fullWidth: true,
        columns: ['Stage', 'Rule', 'Default'],
        rows: [
          ['Folds', 'Rolling train/test windows, stepped forward', 'Up to 3'],
          ['Train window', 'Parameter selection only', '45% of history, min 90 days'],
          ['Test window', 'Untouched out-of-sample evaluation', '18% of history, min 30 days'],
          ['Step', 'Advance by one test window', 'Equal to test length'],
          ['Selection', 'Best training objective per fold', '96 candidate ladders'],
          ['Aggregation', 'Compound test returns; stitch equity curves', 'Fees, funding, slippage included'],
          ['Look-ahead', 'No test bar is visible during selection', 'Enforced by window slicing']
        ]
      },
      {
        kind: 'table',
        title: 'Parameter search space',
        description: 'The 96-combination grid searched independently on every fold.',
        fullWidth: true,
        columns: ['Dimension', 'Values', 'Count'],
        rows: [
          ['Grid levels', '10 / 20 / 30', '3'],
          ['Range lookback', '14 / 30 days', '2'],
          ['Rebalance cadence', '7 / 14 days', '2'],
          ['Stop distance', '1.5 / 2.5 × ATR', '2'],
          ['Take-profit distance', '1.5 / 3.0 × ATR', '2'],
          ['Rebalance mode', 'fixed / adaptive', '2'],
          ['Total combinations', '3 × 2 × 2 × 2 × 2 × 2', '96 per fold']
        ]
      },
      {
        kind: 'list',
        title: 'Fold objective & aggregation',
        description: 'A stop-out is treated as a hard failure, not a return trade-off.',
        rows: [
          {
            name: 'Train objective',
            detail:
              'Net return − 1.5 × max drawdown + 0.05 × min(PF, 3) + 0.05 × activity − 1.0 when stopped.'
          },
          {
            name: 'Aggregation',
            detail:
              'Fold returns are compounded; drawdown comes from the stitched equity curve, not from averaging fold drawdowns.'
          },
          {
            name: 'Reported metrics',
            detail:
              'Net and annualised return, max drawdown, Calmar, Sharpe, Sortino, profit factor, win rate, fills, fees, funding, exposure, and time in range.'
          },
          {
            name: 'Baseline comparison',
            detail:
              'The legacy score is retained as baseline_score, so the enhanced score can be compared on the same cross-section (score_delta).'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Validation ladder',
        description: 'Each stage must pass before the next one matters.',
        rows: [
          {
            name: '1 · Hard gates',
            value: 'before ranking',
            detail:
              'Liquidity, sample, return, drawdown, fold consistency, ATR band, spread, depth, cost stress, and PF validation.'
          },
          {
            name: '2 · Ranking',
            value: 'eligible only',
            detail:
              'Eight weighted factors and penalties order the surviving coins. Ineligible coins keep diagnostics but receive no rank.'
          },
          {
            name: '3 · Paper trading',
            value: '≥ 7 days',
            tone: 'info',
            detail:
              'Minimum 7 days, ≥ 1% net return, ≤ 12% max drawdown, ≥ 20 fills, no stopped session. Hard stop at 20% drawdown.'
          },
          {
            name: '4 · Approval',
            tone: 'warn',
            detail:
              'A live approval record, explicit risk acknowledgement, and a released kill switch are all required. Live trading is disabled by default.'
          },
          {
            name: 'Live guardrails',
            value: '$250 / $50',
            tone: 'good',
            detail:
              'Max notional $250, max daily loss $50, max leverage 2×, symbol allowlist, testnet by default, every order audited.'
          }
        ]
      },
      {
        kind: 'list',
        title: 'Paper & risk-guard settings',
        description: 'Paper evaluation cadence and the overbought pause/resume guard.',
        fullWidth: true,
        rows: [
          {
            name: 'Paper evaluation',
            value: 'every 6h',
            detail:
              'Sessions are evaluated against promotion thresholds every 6 hours; history minimum is 7 days and 20 fills.'
          },
          {
            name: 'Tick cadence',
            value: '5 min',
            detail:
              'Risk guard and paper monitoring run every 5 minutes; strategy signals use completed 1-hour candles.'
          },
          {
            name: 'Overbought trigger',
            value: 'RSI 75',
            detail:
              'RSI at or above 75 plus MACD histogram shrinking for 2 bars or open interest down 5% within 6 hours arms a pause.'
          },
          {
            name: 'Recovery',
            value: 'RSI 35',
            detail:
              'Pause clears when RSI falls to 35, followed by a 30-minute cooldown before new buys resume.'
          },
          {
            name: 'Arming timeout',
            value: '24h',
            detail:
              'An armed pause expires if the recovery condition is not met, preventing a permanently stuck session.'
          },
          {
            name: 'Risk trailing stop',
            value: '6%',
            detail:
              'Independent of the grid ATR stop; checked on the 5-minute risk cadence.'
          }
        ]
      },
      {
        kind: 'table',
        title: 'Metric glossary',
        description: 'How the headline walk-forward numbers are formed.',
        fullWidth: true,
        columns: ['Metric', 'Definition'],
        rows: [
          ['Net return', 'Compounded return across untouched test folds after fees, funding, and slippage'],
          ['Annualised return', 'Net return scaled to a yearly rate over the combined test span'],
          ['Max drawdown', 'Largest peak-to-trough decline of the stitched equity curve'],
          ['Profit factor', 'Mean fold gross profit divided by gross loss'],
          ['Win rate', 'Mean share of closed lots with positive realised P&L'],
          ['Fold consistency', 'Share of test folds with positive net return'],
          ['Calmar', 'Annualised return divided by max drawdown'],
          ['Sharpe / Sortino', 'Bar-return risk-adjusted ratios, annualised'],
          ['Return-to-drawdown', 'Combined net return divided by max drawdown'],
          ['Time in range', 'Share of bars with live price inside the active ladder'],
          ['Parameter stability', 'Share of folds selecting the same full configuration'],
          ['Cost-stress delta', 'Stressed combined return minus base combined return'],
          ['Parameter sensitivity', 'Mean neighbour-return degradation versus the selected ladder'],
          ['Regime robustness', 'Coverage and profitability across trending, high-volatility, and ranging folds']
        ]
      }
    ]
  }
];

export const THRESHOLD_ROWS: Array<{
  key: string;
  label: string;
  format: 'usd' | 'percent' | 'number' | 'multiplier';
  kind: 'gate' | 'review' | 'stress';
}> = [
  { key: 'min_adv_usd', label: 'Median 24h liquidity', format: 'usd', kind: 'gate' },
  { key: 'min_oos_trades', label: 'Minimum OOS fills', format: 'number', kind: 'gate' },
  { key: 'max_drawdown', label: 'Max OOS drawdown', format: 'percent', kind: 'gate' },
  { key: 'min_data_days', label: 'Minimum history', format: 'number', kind: 'gate' },
  { key: 'min_atr_pct', label: 'ATR% floor', format: 'percent', kind: 'gate' },
  { key: 'max_atr_pct', label: 'ATR% ceiling', format: 'percent', kind: 'gate' },
  { key: 'pf_review', label: 'Profit-factor review', format: 'number', kind: 'review' },
  { key: 'pf_validate', label: 'Profit-factor validation', format: 'number', kind: 'review' },
  {
    key: 'cost_stress_multiplier',
    label: 'Cost-stress multiplier',
    format: 'multiplier',
    kind: 'stress'
  },
  { key: 'max_spread_bps', label: 'Max bid-ask spread', format: 'number', kind: 'gate' },
  {
    key: 'min_depth_cover',
    label: 'Min order-book depth cover',
    format: 'multiplier',
    kind: 'gate'
  },
  { key: 'max_correlation', label: 'Max peer correlation', format: 'number', kind: 'review' },
  {
    key: 'max_top_trade_share',
    label: 'Max top-trade share',
    format: 'percent',
    kind: 'review'
  }
];

export const PARAM_LABELS: Record<string, string> = {
  grid_levels: 'Grid levels',
  range_lookback_days: 'Range lookback (days)',
  rebalance_days: 'Rebalance cadence (days)',
  spacing_mode: 'Spacing mode',
  atr_multiplier: 'ATR multiplier',
  initial_inventory_pct: 'Initial inventory',
  stop_loss_pct: 'Capital stop',
  max_utilization: 'Max margin utilization',
  stop_atr_multiple: 'Structural stop (ATR)',
  support_lookback_days: 'Support lookback (days)',
  support_buffer_atr: 'Support buffer (ATR)',
  max_range_atr: 'Max range width (ATR)',
  trail_up_atr_multiple: 'Trail-up trigger (ATR)',
  tp_activate_net_return: 'Trailing TP arm (net)',
  tp_atr_multiple: 'Trailing TP distance (ATR)',
  range_position_days: 'Range-position window (days)',
  range_pos_high: 'Range-position high',
  range_pos_low: 'Range-position low',
  funding_z_threshold: 'Funding z-score trigger',
  funding_z_lookback: 'Funding lookback (settlements)',
  funding_cut_scale: 'Funding inventory scale',
  max_inventory_pct: 'Inventory cap (× equity)',
  max_total_drawdown: 'Portfolio drawdown stop',
  range_mode: 'Range mode',
  rebalance_mode: 'Rebalance mode',
  range_review_hours: 'Adaptive review (hours)',
  rebalance_cooldown_hours: 'Rebalance cooldown (hours)',
  volatility_shift_ratio: 'Volatility-shift trigger',
  breakout_atr_multiple: 'Breakout trigger (ATR)'
};

export const PARAM_PERCENT_KEYS = new Set([
  'initial_inventory_pct',
  'stop_loss_pct',
  'max_utilization',
  'tp_activate_net_return',
  'range_pos_high',
  'range_pos_low',
  'funding_cut_scale',
  'max_inventory_pct',
  'max_total_drawdown',
  'volatility_shift_ratio'
]);
